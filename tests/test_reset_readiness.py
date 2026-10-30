import base64
import hashlib
import io
import json
import unittest
import zipfile
from datetime import datetime, timedelta
from unittest.mock import patch
from sqlalchemy import text, event
from app import db
from app.models import (ApplicationMemberAddition, ApplicationMemberCountReview, CampaignRecipient,
    ClientApplication, ClientStoredFile, CommunicationCampaign, ContactCommunicationPreference,
    ContactSuppression, LapsedPolicy, PolicyProduct, SystemSetting, WhatsAppTemplate)
from app.services.communication_service import contact_hash, normalize_phone, normalize_email
from app.services.data_reset import reset_operational_data
from app.services.reset_backup import build_reset_backup
from tests import test_analytics_security as fixtures


class ResetReadinessTests(unittest.TestCase):
    login=fixtures.AnalyticsSecurityTests.login

    def setUp(self):
        fixtures.AnalyticsSecurityTests.setUp(self)
        db.session.execute(text('PRAGMA foreign_keys=ON'))
        product=PolicyProduct(product_name='Kept product',plan_name='Test',active=True)
        db.session.add(product);db.session.flush()
        lead=LapsedPolicy.query.filter_by(branch='A').one();lead.email_address='client@example.test'
        self.phone=contact_hash(normalize_phone(lead.cell_number or '0821234567'))
        lead.cell_number='0821234567'
        self.phone=contact_hash(normalize_phone(lead.cell_number))
        campaign=CommunicationCampaign(name='Kept campaign',message_body='Example',created_by_id=self.user_ids['admin'],
            archived_at=datetime.utcnow(),deleted_at=datetime.utcnow(),qr_scan_count=7,scheduled_at=datetime.utcnow(),queue_status='scheduled')
        db.session.add(campaign);db.session.flush();self.cid=campaign.id
        recipient=CampaignRecipient(campaign_id=campaign.id,lapsed_policy_id=lead.id,secure_token='old-recipient-link')
        db.session.add(recipient);db.session.flush()
        a=ClientApplication(application_ref='RESET-TEST',product_id=product.id,lapsed_policy_id=lead.id,source_campaign_recipient_id=recipient.id)
        db.session.add(a);db.session.flush();self.aid=a.id
        self.contents=b'Original private document bytes\x00\xff'
        db.session.add_all([ClientStoredFile(application_id=a.id,relative_path='private.pdf',content=self.contents),
            ApplicationMemberAddition(application_id=a.id,token_hash='a'*64,snapshot='b'*64,expires_at=datetime.utcnow()+timedelta(days=7)),
            ApplicationMemberCountReview(application_id=a.id,expected_count=2),
            ContactCommunicationPreference(lapsed_policy_id=lead.id,opted_out_all=True,email_allowed=False,whatsapp_allowed=False),
            ContactSuppression(phone_hash=self.phone),
            SystemSetting(category='Email',key='secret',value='NOT-IN-EXPORT'),
            WhatsAppTemplate(campaign_id=campaign.id,name='kept_template',language='en',body_text='Kept message',status='Approved')])
        db.session.commit();self.login('admin')

    def tearDown(self):
        db.session.rollback();db.session.execute(text('PRAGMA foreign_keys=OFF'))
        fixtures.AnalyticsSecurityTests.tearDown(self)

    def backup(self):
        archive,fingerprint=build_reset_backup()
        with zipfile.ZipFile(archive) as z:
            raw=z.read('records.json');manifest=json.loads(z.read('manifest.json'));data=json.loads(raw)
        return fingerprint,raw,manifest,data

    def test_backup_is_read_only_and_preserves_document_bytes_without_configuration_secrets(self):
        before=ClientApplication.query.count()
        fingerprint,raw,manifest,data=self.backup()
        self.assertEqual(before,ClientApplication.query.count())
        self.assertEqual(hashlib.sha256(raw).hexdigest(),manifest['sha256_records'])
        self.assertEqual(base64.b64decode(data['tables']['client_stored_files'][0]['content']['base64']),self.contents)
        self.assertNotIn('users',data['tables']);self.assertNotIn('system_settings',data['tables'])
        self.assertNotIn(b'NOT-IN-EXPORT',raw)
        self.assertEqual(fingerprint,self.backup()[0])

    def test_reset_with_foreign_keys_clears_new_member_features_and_retains_configuration(self):
        fingerprint,*_=self.backup()
        result=reset_operational_data(expected_fingerprint=fingerprint)
        self.assertEqual(result['applications'],1)
        for model in [LapsedPolicy,ClientApplication,ClientStoredFile,CampaignRecipient,ApplicationMemberAddition,ApplicationMemberCountReview]:
            self.assertEqual(model.query.count(),0)
        self.assertEqual(PolicyProduct.query.count(),1)
        self.assertEqual(WhatsAppTemplate.query.count(),1)
        self.assertEqual(SystemSetting.query.filter_by(key='secret').one().value,'NOT-IN-EXPORT')
        campaign=db.session.get(CommunicationCampaign,self.cid)
        self.assertIsNotNone(campaign.archived_at);self.assertIsNotNone(campaign.deleted_at)
        self.assertEqual(campaign.qr_scan_count,0);self.assertEqual(campaign.queue_status,'idle');self.assertIsNone(campaign.scheduled_at)
        self.assertIsNotNone(ContactSuppression.query.filter_by(phone_hash=self.phone).first())
        self.assertIsNotNone(ContactSuppression.query.filter_by(email_hash=contact_hash(normalize_email('client@example.test'))).first())

    def test_changes_after_backup_block_reset_without_deleting_anything(self):
        fingerprint,*_=self.backup()
        db.session.add(LapsedPolicy(policy_number='NEW-SINCE-BACKUP'));db.session.commit()
        with self.assertRaisesRegex(ValueError,'changed after the backup'):reset_operational_data(expected_fingerprint=fingerprint)
        db.session.rollback()
        self.assertEqual(ClientApplication.query.count(),1);self.assertEqual(LapsedPolicy.query.count(),3)

    def test_failed_database_reset_rolls_back_all_deletions(self):
        def reject_delete(conn,cursor,statement,parameters,context,executemany):
            if statement.startswith('DELETE FROM lapsed_policies'):raise RuntimeError('Simulated failed delete')
        event.listen(db.engine,'before_cursor_execute',reject_delete)
        try:
            with self.assertRaisesRegex(RuntimeError,'Simulated'):reset_operational_data()
            db.session.rollback()
        finally:event.remove(db.engine,'before_cursor_execute',reject_delete)
        self.assertEqual(ClientApplication.query.count(),1)
        self.assertEqual(ApplicationMemberAddition.query.count(),1)
        self.assertEqual(CampaignRecipient.query.count(),1)
        self.assertEqual(ClientStoredFile.query.one().content,self.contents)

    def test_channel_only_opt_out_is_not_lost(self):
        lead=LapsedPolicy.query.filter_by(branch='B').one();lead.cell_number='0821234568'
        db.session.add(ContactCommunicationPreference(lapsed_policy_id=lead.id,whatsapp_allowed=False,opted_out_all=False))
        db.session.commit();reset_operational_data()
        self.assertIsNotNone(ContactSuppression.query.filter_by(phone_hash=contact_hash(normalize_phone('0821234568'))).first())

    def test_backup_and_clear_routes_require_super_admin_backup_csrf_and_exact_confirmation(self):
        page=self.client.get('/settings/data-reset');self.assertEqual(page.status_code,200)
        with self.client.session_transaction() as s:csrf=s['data_reset_csrf']
        self.assertEqual(self.client.post('/settings/data-reset',data={'confirmation':'CLEAR ALL TEST DATA'}).status_code,400)
        data={'data_reset_csrf':csrf,'confirmation':'CLEAR ALL TEST DATA','backup_saved':'yes'}
        self.client.post('/settings/data-reset',data=data)
        self.assertEqual(ClientApplication.query.count(),1)
        backup=self.client.get('/settings/data-reset/backup')
        self.assertEqual(backup.status_code,200);self.assertIn('no-store',backup.headers['Cache-Control'])
        self.assertTrue(zipfile.is_zipfile(io.BytesIO(backup.data)))
        self.client.post('/settings/data-reset',data={**data,'confirmation':'WRONG'})
        self.assertEqual(ClientApplication.query.count(),1)
        self.client.post('/settings/data-reset',data=data)
        self.assertEqual(ClientApplication.query.count(),0)
        self.login('manager')
        self.assertEqual(self.client.get('/settings/data-reset').status_code,403)
        self.assertEqual(self.client.get('/settings/data-reset/backup').status_code,403)


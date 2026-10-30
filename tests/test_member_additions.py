import base64
import io
import json
import re
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageDraw
from reportlab.pdfgen import canvas
from tests import test_analytics_security as fixtures
from app import db
from app.models import (ApplicationMemberAddition, ApplicationJourney, ClientApplication, ClientFicaDocument,
                        ClientStoredFile, DocumentSignature, PolicyProduct, PolicyProductRule)
from app.services.client_storage import application_folder, store_document
from app.services.member_additions import append_member, available_kinds, token_hash
from app.services.signature_fields import application_fields, signed_documents


class MemberAdditionTests(unittest.TestCase):
    baseSetUp = fixtures.AnalyticsSecurityTests.setUp
    tearDown = fixtures.AnalyticsSecurityTests.tearDown
    login = fixtures.AnalyticsSecurityTests.login

    def setUp(self):
        self.baseSetUp()
        self.app.config['BASE_URL'] = 'https://example.test'
        product = PolicyProduct(product_name='Member 1 + 5 R20 000', plan_name='R20k', cover_amount=20000,
                                monthly_premium=330, waiting_period_months=6, active=True)
        db.session.add(product);db.session.flush()
        db.session.add(PolicyProductRule(product_id=product.id, plan_type='family'))
        a = ClientApplication(application_ref='ADD-MEMBER', product=product, form_template='member_product',
            first_names='Example', surname='Principal', id_number='8001015009087', date_of_birth='01/01/1980',
            email='client@example.test', document_email='client@example.test', cell_number='0821234567',
            residential_address='Saved address', bank_name='Saved bank', status='Signed', payment_method='Cash',
            agent_id=self.user_ids['admin'], branch='A', cover_amount=20000, monthly_premium=330,
            signed_at=datetime.utcnow(), sign_token='previous-signing-token', sign_token_revoked=True,
            sign_token_used_at=datetime.utcnow(), product_dependents_json=json.dumps([
                {'full_name':'Existing Child','relationship':'Child','id_or_dob':'2010-01-01','cover':'20000','kind':'productdep'}]))
        # Cash applications must not accidentally be treated as debit orders by the signature layout.
        a.bank_name = ''
        db.session.add(a);db.session.flush();self.aid=a.id
        db.session.add(ApplicationJourney(application_id=a.id, ready=True, signed_bundle_at=datetime.utcnow()))
        folder=Path(application_folder(a))
        sig=folder/'previous-signature.png';image=Image.new('RGB',(140,50),'white');ImageDraw.Draw(image).line((10,10,120,40),fill='black',width=3);image.save(sig)
        self.signature_data='data:image/png;base64,'+base64.b64encode(sig.read_bytes()).decode()
        for field in application_fields(a):
            db.session.add(DocumentSignature(application_id=a.id, document_type=field['key'], typed_name='Example',signature_image_path=str(sig)))
        for key in ['popia','disclosure','welcome','cdd']:
            db.session.add(DocumentSignature(application_id=a.id, document_type=key, typed_name='Example',signature_image_path=str(sig)))
        for kind in ['signed_application','welcome_pack','popia_consent','policy_disclosure','annexure_j1']:
            path=folder/f'{kind}_{a.id}.pdf';c=canvas.Canvas(str(path));c.drawString(40,800,'Original signed '+kind);c.save();store_document(a,path)
        a.signed_pdf_path=str(folder/f'signed_application_{a.id}.pdf')
        self.original=(folder/f'signed_application_{a.id}.pdf').read_bytes()
        db.session.add(ClientFicaDocument(application_id=a.id, document_type='id_copy', original_filename='existing.png',file_path=str(sig),status='Approved'))
        db.session.commit();self.login('admin');self.public=self.app.test_client()

    @property
    def record(self): return db.session.get(ClientApplication,self.aid)

    def issue(self, success=True):
        with self.client.session_transaction() as s: s['member_addition_csrf']='staff-nonce'
        with patch('app.services.email_service.send_email',return_value=success) as mail:
            response=self.client.post(f'/member-additions/request/{self.aid}',data={'member_addition_csrf':'staff-nonce','channel':'email'})
        self.assertEqual(response.status_code,302)
        if not mail.called: return None
        self.assertEqual(mail.call_args.args[0],'client@example.test')
        return re.search(r'/member-additions/([A-Za-z0-9_-]+)',mail.call_args.args[2]).group(1)

    def unlock(self, token):
        link='/member-additions/'+token
        page=self.public.get(link);self.assertEqual(page.status_code,200)
        self.assertNotIn(b'Example Principal',page.data)
        addition=ApplicationMemberAddition.query.filter_by(token_hash=token_hash(token)).one()
        with self.public.session_transaction() as s: nonce=s[f'add_member_nonce_{addition.id}']
        self.assertEqual(self.public.post(link,data={'action':'unlock','nonce':nonce,'id_number':'8001015009087'}).status_code,302)
        return link,nonce

    def add(self, link, nonce, **changes):
        data={'action':'add','nonce':nonce,'member_kind':'productdep','full_name':'Missing Spouse',
              'relationship':'Spouse','id_or_dob':'1985-01-01'};data.update(changes)
        return self.public.post(link,data=data)

    def test_staff_permissions_csrf_and_delivery_failure(self):
        self.assertEqual(self.client.post(f'/member-additions/request/{self.aid}').status_code,400)
        self.login('other')
        with self.client.session_transaction() as s:s['member_addition_csrf']='staff-nonce'
        self.assertEqual(self.client.post(f'/member-additions/request/{self.aid}',data={'member_addition_csrf':'staff-nonce'}).status_code,403)
        self.login('admin');self.issue(False)
        self.assertEqual(ApplicationMemberAddition.query.one().delivery_status,'Delivery failed')

    def test_staff_can_send_the_focused_link_by_whatsapp(self):
        from types import SimpleNamespace
        with self.client.session_transaction() as s:s['member_addition_csrf']='staff-nonce'
        with patch('app.services.whatsapp_service.send_application_link',return_value=SimpleNamespace(ok=True)) as send:
            response=self.client.post(f'/member-additions/request/{self.aid}',data={'member_addition_csrf':'staff-nonce','channel':'whatsapp'})
        self.assertEqual(response.status_code,302)
        self.assertIn('/member-additions/',send.call_args.args[1])
        self.assertEqual(ApplicationMemberAddition.query.one().delivery_status,'Accepted by provider')

    def test_expired_replaced_and_stale_links_cannot_change_members(self):
        old=self.issue();fresh=self.issue()
        self.assertEqual(self.public.get('/member-additions/'+old).status_code,410)
        request_row=ApplicationMemberAddition.query.filter_by(token_hash=token_hash(fresh)).one()
        request_row.expires_at=datetime.utcnow()-timedelta(seconds=1);db.session.commit()
        self.assertEqual(self.public.get('/member-additions/'+fresh).status_code,410)
        fresh=self.issue();link,nonce=self.unlock(fresh)
        self.record.spouse_surname='Changed by staff';db.session.commit()
        self.assertEqual(self.add(link,nonce).status_code,410)
        self.assertEqual(len(json.loads(self.record.product_dependents_json)),1)

    def test_identity_unlock_and_csrf_required(self):
        token=self.issue();link='/member-additions/'+token
        page=self.public.post(link,data={'action':'add'});self.assertEqual(page.status_code,400)
        self.public.get(link)
        row=ApplicationMemberAddition.query.one()
        with self.public.session_transaction() as s:nonce=s[f'add_member_nonce_{row.id}']
        response=self.public.post(link,data={'action':'unlock','nonce':nonce,'id_number':'0000000000000'})
        self.assertIn(b'does not match',response.data)
        self.assertIn(b'Confirm your identity',self.add(link,nonce).data)
        self.assertEqual(len(json.loads(self.record.product_dependents_json)),1)

    def test_add_preserves_existing_details_documents_and_other_signatures(self):
        link,nonce=self.unlock(self.issue());response=self.add(link,nonce)
        self.assertEqual(response.status_code,302)
        a=self.record
        self.assertEqual(a.residential_address,'Saved address');self.assertEqual(a.email,'client@example.test')
        self.assertEqual(float(a.monthly_premium),330)
        members=json.loads(a.product_dependents_json)
        self.assertEqual([r['full_name'] for r in members],['Existing Child','Missing Spouse'])
        self.assertEqual(members[1]['cover'],'20000.00');self.assertEqual(a.total_members,3)
        self.assertIsNone(a.signed_at);self.assertTrue(a.whatsapp_journey.ready)
        self.assertNotEqual(a.sign_token,'previous-signing-token')
        self.assertEqual(signed_documents(a),{'popia','disclosure','welcome','cdd'})
        self.assertEqual(ClientFicaDocument.query.filter_by(application_id=self.aid,status='Approved').count(),1)
        archive=ClientStoredFile.query.filter(ClientStoredFile.relative_path.like('revisions/%')).one()
        self.assertEqual(archive.content,self.original)
        # A retry/replayed request cannot append the same member twice.
        self.add(link,nonce);self.assertEqual(len(json.loads(self.record.product_dependents_json)),2)
        page=self.public.get(link);self.assertIn(b'Review and sign updated application',page.data)
        self.assertNotIn(b'name="bank_name"',page.data)
        with patch('app.routes.signing.finish_application') as finish:
            response=self.public.post(link,data={'action':'submit','nonce':nonce})
            self.assertIn(b'sign the updated application first',response.data);finish.assert_not_called()

    def test_product_capacity_age_duplicate_and_active_policy_guards(self):
        link,nonce=self.unlock(self.issue())
        response=self.add(link,nonce,id_or_dob='1900-01-01');self.assertIn(b'age range',response.data)
        response=self.add(link,nonce,full_name='Existing Child',id_or_dob='2010-01-01');self.assertIn(b'already captured',response.data)
        response=self.add(link,nonce,member_kind='spouse');self.assertIn(b'no available space',response.data)
        response=self.add(link,nonce,id_or_dob='bad');self.assertIn(b'valid South African ID',response.data)
        self.record.status='Active';db.session.commit();self.assertEqual(self.add(link,nonce).status_code,410)

    def test_full_product_cannot_issue_an_addition_link(self):
        members=json.loads(self.record.product_dependents_json)
        members.extend({'full_name':f'Member {i}','id_or_dob':'1980-01-01','relationship':'Other'} for i in range(4))
        self.record.product_dependents_json=json.dumps(members);db.session.commit()
        self.assertEqual(available_kinds(self.record),[])
        self.assertIsNone(self.issue())
        self.assertEqual(ApplicationMemberAddition.query.count(),0)

    def test_resend_after_adding_resumes_signing_without_a_second_addition(self):
        old=self.issue();link,nonce=self.unlock(old);self.add(link,nonce)
        renewed=self.issue();self.assertIsNotNone(renewed)
        self.assertEqual(self.public.get(link).status_code,410)
        link,nonce=self.unlock(renewed)
        self.assertIn(b'Review and sign updated application',self.public.get(link).data)
        self.add(link,nonce)
        self.assertEqual(len(json.loads(self.record.product_dependents_json)),2)

    def test_family_addition_keeps_child_and_spouse_in_combined_list(self):
        a=self.record;a.product.product_name='Family';a.product.plan_name='Family';a.form_template='single_family'
        a.product.rules.plan_type='family';a.product_dependents_json='[]'
        a.dependents_json=json.dumps([{'full_name':'Existing Child','id_or_dob':'2010-01-01','relationship':'Child'}])
        db.session.commit()
        name=append_member(a,{'member_kind':'spouse','full_name':'Missing Spouse','relationship':'Spouse','id_or_dob':'1985-01-01'})
        self.assertEqual(name,'Missing Spouse');self.assertEqual(a.spouse_first_names,'Missing')
        self.assertEqual([r['full_name'] for r in json.loads(a.product_dependents_json)],['Existing Child','Missing Spouse'])

    def test_focused_resigning_returns_to_addition_and_completes(self):
        link,nonce=self.unlock(self.issue());self.add(link,nonce)
        a=self.record;sign_token=a.sign_token
        from app.services.pdf_service import _signature_for_app
        self.assertIsNone(_signature_for_app(a,document_type='application'))
        for field in application_fields(a):
            response=self.public.get(f'/sign/{sign_token}/review/application');self.assertEqual(response.status_code,200)
            with self.public.session_transaction() as s:review_nonce=s[f'document_review_{self.aid}_application']
            response=self.public.post(f'/sign/{sign_token}',data={'action':'sign_document','document_type':'application',
                'signature_field':field['key'],'review_nonce':review_nonce,'typed_name':'Example Principal','signature_data':self.signature_data})
            self.assertEqual(response.status_code,302)
        self.assertTrue(response.location.endswith(link))
        self.assertIn(b'Submit updated application',self.public.get(link).data)
        with patch('app.services.company_documents.active_company_documents',return_value=[type('Doc',(),{'category':'bank_confirmation'})()]), \
             patch('app.routes.signing.send_supporting_upload_email',return_value=True), \
             patch('app.routes.signing.send_email',return_value=True):
            response=self.public.post(link,data={'action':'submit','nonce':nonce})
        self.assertEqual(response.status_code,302)
        self.assertIsNotNone(ApplicationMemberAddition.query.one().completed_at)
        self.assertIsNotNone(self.record.signed_at);self.assertTrue(self.record.sign_token_revoked)
        page=self.public.get(link);self.assertIn(b'Upload the new member',page.data)
        uploads=self.public.get('/sign/'+self.record.sign_token+'/supporting-documents')
        self.assertIn(b'Missing Spouse',uploads.data)
        self.assertIn(b'Received for staff review',uploads.data)
        self.assertEqual(len(json.loads(self.record.product_dependents_json)),2)


if __name__=='__main__':unittest.main()

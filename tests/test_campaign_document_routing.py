import os
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from tests import test_analytics_security as fixtures
from app import db
from app.models import (BranchFilingOffice, CampaignRecipient, ClientApplication,
                        CommunicationCampaign, LapsedPolicy, PolicyProduct, WhatsAppTemplate)
from app.services.email_service import submission_notification_recipients
from app.services.branch_filing import filing_office, send_branch_pack
from app.services.client_storage import application_folder


class CampaignDocumentRoutingTests(unittest.TestCase):
    setUpBase = fixtures.AnalyticsSecurityTests.setUp
    tearDown = fixtures.AnalyticsSecurityTests.tearDown
    login = fixtures.AnalyticsSecurityTests.login

    def setUp(self):
        self.setUpBase()
        db.session.add_all([
            BranchFilingOffice(branch_key='wjm', branch_name='Wjm', email='wjm@martinsdirect.com', is_default=True),
            BranchFilingOffice(branch_key='lowhann', branch_name='Lowhann', email='lowhann@martinsdirect.com'),
            BranchFilingOffice(branch_key='sanel', branch_name='Sanel', email='reception@martinsdirect.com'),
            BranchFilingOffice(branch_key='inactive', branch_name='Inactive', email='inactive@example.com', active=False),
        ])
        self.campaign = CommunicationCampaign(name='Routing', message_body='Hello {{1}}', branch='A',
            created_by_id=self.user_ids['admin'], filing_branch='Sanel', public_application_token='public-routing')
        self.product = PolicyProduct(product_name='Routing Family', plan_name='R10k', cover_amount=10000,
                                     monthly_premium=99, active=True)
        db.session.add_all([self.campaign, self.product])
        db.session.flush()
        self.campaign.product_id = self.product.id
        self.lead = LapsedPolicy.query.filter_by(branch='A').one()
        self.lead.cell_number = '0821234567'
        self.lead.email_address = 'client@example.com'
        self.recipient = CampaignRecipient(campaign_id=self.campaign.id, lapsed_policy_id=self.lead.id,
                                          secure_token='routing-token')
        db.session.add(self.recipient)
        db.session.commit()
        self.cid, self.pid, self.rid = self.campaign.id, self.product.id, self.recipient.id

    def save(self, branch, csrf=True):
        self.login('admin')
        with self.client.session_transaction() as s:
            s['document_routing_csrf'] = 'test-routing'
        data = {'filing_branch': branch}
        if csrf:
            data['document_routing_csrf'] = 'test-routing'
        return self.client.post(f'/communications/{self.cid}/document-recipient', data=data)

    def test_choice_saved_only_with_csrf_and_active_office(self):
        self.assertEqual(self.save('lowhann', csrf=False).status_code, 400)
        self.assertEqual(self.save('inactive').status_code, 404)
        self.assertEqual(self.save('forged@example.com').status_code, 404)
        self.assertEqual(db.session.get(CommunicationCampaign, self.cid).filing_branch, 'Sanel')
        self.assertEqual(self.save('lowhann').status_code, 302)
        self.assertEqual(db.session.get(CommunicationCampaign, self.cid).filing_branch, 'Lowhann')
        page = self.client.get(f'/communications/{self.cid}')
        self.assertEqual(page.status_code, 200)
        self.assertIn(b'Application documents go to', page.data)
        self.assertIn(b'reception@martinsdirect.com', page.data)

    def test_other_branch_cannot_change_routing(self):
        self.login('other')
        with self.client.session_transaction() as s:
            s['document_routing_csrf'] = 'test-routing'
        response = self.client.post(f'/communications/{self.cid}/document-recipient',
            data={'filing_branch': 'wjm', 'document_routing_csrf': 'test-routing'})
        self.assertEqual(response.status_code, 403)

    def test_create_from_approved_template_saves_selected_recipient(self):
        template = WhatsAppTemplate(campaign_id=self.cid, name='routing_approved', status='Approved', body_text='Hello {{1}}', language='en_US')
        db.session.add(template)
        db.session.commit()
        tid = template.id
        self.login('admin')
        page = self.client.get(f'/communications/new?template_id={tid}')
        self.assertIn(b'wjm@martinsdirect.com', page.data)
        response = self.client.post('/communications/new', data={'name':'Selected office',
            'template_id':tid, 'template_mode':'existing', 'filing_branch':'lowhann'})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(CommunicationCampaign.query.filter_by(name='Selected office').one().filing_branch, 'Lowhann')

    def test_personal_link_copies_office_and_keeps_client_email(self):
        self.client.post('/join/routing-token', data={'id_number':'8001015009087', 'total_members':'4', 'cover_amount':'10000'})
        response = self.client.get(f'/join/routing-token/application/{self.pid}')
        self.assertEqual(response.status_code, 302, response.data.decode())
        self.assertIn('/online-application/', response.location)
        application = ClientApplication.query.filter_by(source_campaign_recipient_id=self.rid).one()
        self.assertEqual(application.filing_branch, 'Sanel')
        self.assertEqual(application.document_email, 'client@example.com')
        aid = application.id
        self.assertEqual(self.save('lowhann').status_code, 302)
        application = db.session.get(ClientApplication, aid)
        self.assertEqual(application.filing_branch, 'Sanel')
        self.assertEqual(filing_office(application).email, 'reception@martinsdirect.com')

    def test_public_qr_copies_default_when_old_campaign_has_no_choice(self):
        self.campaign.filing_branch = None
        db.session.commit()
        response = self.client.post('/join/campaign/public-routing', data={'first_names':'Example', 'surname':'Client',
            'id_number':'8001015009087', 'cell_number':'0821234567', 'email':'client@example.com', 'total_members':'1'})
        self.assertIn('/online-application/', response.location)
        application = ClientApplication.query.one()
        self.assertEqual(application.filing_branch, 'Wjm')
        self.assertEqual(application.email, 'client@example.com')

    def test_notification_goes_only_to_selected_office(self):
        with patch.dict(os.environ, {'MAIL_DOCUMENTS_TO':'wjm@martinsdirect.com',
                                     'APPLICATION_SUBMISSION_NOTIFY':'wjm@martinsdirect.com'}):
            for branch, expected in [('Wjm','wjm@martinsdirect.com'), ('Lowhann','lowhann@martinsdirect.com'),
                                     ('Sanel','reception@martinsdirect.com')]:
                application = ClientApplication(filing_branch=branch)
                self.assertEqual(submission_notification_recipients('client@example.com', application), [expected])
            self.assertEqual(submission_notification_recipients('client@example.com', ClientApplication(filing_branch='Inactive')), [])

    def test_approved_pack_sends_five_pdfs_to_selected_recipient_once(self):
        application = ClientApplication(application_ref='ROUTING-PACK', status='Active', signed_at=datetime.utcnow(),
                                        filing_branch='Sanel')
        db.session.add(application)
        db.session.commit()
        aid = application.id
        folder = Path(application_folder(application))
        for kind in ['signed_application', 'welcome_pack', 'popia_consent', 'policy_disclosure', 'annexure_j1']:
            (folder / f'{kind}_{aid}.pdf').write_bytes(b'%PDF-1.4 test document')
        with patch('app.services.branch_filing.send_email', return_value=True) as mail:
            first = send_branch_pack(aid)
            self.assertEqual(first.recipient_email, 'reception@martinsdirect.com')
            self.assertEqual(mail.call_args.args[0], 'reception@martinsdirect.com')
            self.assertEqual(len(mail.call_args.args[3]), 5)
            self.assertEqual(send_branch_pack(aid).id, first.id)
            self.assertEqual(mail.call_count, 1)


if __name__ == '__main__':
    unittest.main()

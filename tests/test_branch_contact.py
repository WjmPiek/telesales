import io
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from pypdf import PdfReader
import test_analytics_security as fixtures
from app import db
from app.models import LapsedPolicy, CommunicationCampaign, CampaignRecipient, ClientApplication, PolicyProduct, PolicyProductRule, WhatsAppMessage
from app.services.branch_contact import branch_contact


class BranchContactTests(unittest.TestCase):
    setUp=fixtures.AnalyticsSecurityTests.setUp
    tearDown=fixtures.AnalyticsSecurityTests.tearDown

    def record(self,branch):
        product=PolicyProduct(product_name='Contact test',plan_name='Family',monthly_premium=100,cover_amount=10000)
        db.session.add(product);db.session.flush()
        db.session.add(PolicyProductRule(product_id=product.id,plan_type='family'))
        application=ClientApplication(application_ref='CONTACT',branch=branch,first_names='Example',surname='Client',
            id_number='8001015009087',date_of_birth='01/01/1980',product=product,monthly_premium=100,cover_amount=10000,
            application_type='New Policy',payment_method='Cash')
        db.session.add(application);db.session.commit()
        return application

    def recipient(self,branch):
        policy=LapsedPolicy(policy_number='PHONE-TEST',branch=branch,cell_number='0821234567')
        campaign=CommunicationCampaign(name='Contact test',branch=branch,message_body='Test',created_by_id=self.user_ids['admin'])
        db.session.add_all([policy,campaign]);db.session.flush()
        recipient=CampaignRecipient(campaign_id=campaign.id,lapsed_policy_id=policy.id,secure_token='contact-test')
        db.session.add(recipient);db.session.commit()
        return recipient

    def test_application_branch_wins_after_transfer_to_brokers(self):
        self.assertEqual(branch_contact(SimpleNamespace(branch='NORTHCLIFF'))['phone'],'010 448 0921')
        self.assertEqual(branch_contact(SimpleNamespace(branch='Brokers',campaign=SimpleNamespace(branch='NORTHCLIFF')))['phone'],'011 412 4180')

    def test_northcliff_client_pages_and_email_show_tap_to_call_number(self):
        from app.services.email_service import client_email_content
        self.recipient('NORTHCLIFF')
        for url in ['/communications/client/contact-test','/communications/respond/contact-test/callback']:
            page=self.client.get(url)
            self.assertEqual(page.status_code,200)
            self.assertIn(b'tel:+27104480921',page.data)
            self.assertIn(b'010 448 0921',page.data)
        app=self.record('Brokers')
        subject,body=client_email_content('activation',app)
        self.assertIn('011 412 4180',body)
        self.assertNotIn('010 448 0921',body)

    def confirmation(self,branch,phone):
        from app.routes.whatsapp import _process_payload
        self.recipient(branch)
        item={'id':'inbound-callback','from':'27821234567','type':'button',
              'button':{'text':'CALL ME BACK','payload':'callback:contact-test'}}
        payload={'messages':[item]}
        result=SimpleNamespace(ok=True,message_id='office-reply',error=None,response_json={})
        with patch.dict('os.environ',{'WHATSAPP_PROVIDER':'meta','META_PHONE_NUMBER_ID':'','WHATSAPP_PHONE_NUMBER_ID':''}), \
             patch('app.routes.whatsapp.send_whatsapp_text',return_value=result) as send:
            _process_payload(payload);db.session.commit()
            self.assertIn(phone,send.call_args.args[1])
            _process_payload(payload);db.session.commit()
            item['id']='second-callback-click'
            _process_payload(payload);db.session.commit()
            self.assertEqual(send.call_count,1)
        self.assertEqual(WhatsAppMessage.query.filter_by(provider_message_id='office-reply').count(),1)

    def test_northcliff_whatsapp_callback_confirmation_uses_office_number_once(self):
        self.confirmation('NORTHCLIFF','010 448 0921')

    def test_brokers_whatsapp_callback_confirmation_uses_office_number_once(self):
        self.confirmation('Brokers','011 412 4180')

    def test_new_policy_documents_use_the_application_branch_phone(self):
        from app.services.pdf_service import generate_welcome_pack,generate_popia_pdf
        from app.services.client_storage import application_folder
        app=self.record('NORTHCLIFF')
        for branch,expected in [('NORTHCLIFF','010 448 0921'),('Brokers','011 412 4180')]:
            app.branch=branch
            for i,generator in enumerate([generate_welcome_pack,generate_popia_pdf]):
                path=str(Path(application_folder(app))/f'{branch}-{i}.pdf')
                generator(app,path)
                content='\n'.join(page.extract_text() for page in PdfReader(path).pages)
                self.assertIn(expected,content)

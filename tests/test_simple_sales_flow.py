import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch
import test_laps_report as fixtures
from app import db
from app.models import (CompanyGroupState, BranchFilingOffice, CommunicationCampaign, WhatsAppTemplate,
    PolicyProduct, ClientImportBatch, ClientImportRow, ClientImportCampaign, CampaignRecipient, LapsedPolicy)


class SimpleSalesFlowTests(unittest.TestCase):
    setUp=fixtures.LapsReportTests.setUp
    tearDown=fixtures.LapsReportTests.tearDown
    login=fixtures.LapsReportTests.login
    workbook=staticmethod(fixtures.LapsReportTests.workbook)

    def seed(self):
        self.login('admin')
        companies=[CompanyGroupState(company_name="Martin's Brokers",branch='Brokers',status='Active'),
                   CompanyGroupState(company_name="Martin's Northcliff",branch='NORTHCLIFF',status='Active')]
        offices=[BranchFilingOffice(branch_name="Martin's Brokers",branch_key="martin's brokers",email='brokers@example.test'),
                 BranchFilingOffice(branch_name="Martin's Northcliff",branch_key="martin's northcliff",email='northcliff@example.test')]
        source=CommunicationCampaign(name='Approved template',message_body='Hello {{1}}',created_by_id=self.user_ids['admin'],
            image_url='https://example.test/image.png',template_status='Approved')
        product=PolicyProduct(product_name='Single test',plan_name='Single',monthly_premium=100,cover_amount=10000,active=True)
        db.session.add_all(companies+offices+[source,product]);db.session.flush()
        template=WhatsAppTemplate(campaign_id=source.id,name='approved_test',body_text='Hello {{1}}',language='en',status='Approved')
        db.session.add(template);db.session.commit()
        self.company_ids=[c.id for c in companies];self.template_id=template.id;self.product_id=product.id
        self.assertEqual(self.client.get('/sales/imports').status_code,200)
        with self.client.session_transaction() as session:self.token=session['sales_csrf']

    def upload(self, company=0):
        response=self.client.post('/sales/imports',data={'sales_csrf':self.token,'company_id':str(self.company_ids[company]),
            'description':'November list','file':(self.workbook(),'Northcliff Laps Report.xlsx')})
        self.assertEqual(response.status_code,302,response.data[:100])
        batch=ClientImportBatch.query.order_by(ClientImportBatch.id.desc()).first()
        return batch.id,response.location

    def create_campaign(self,batch_id):
        response=self.client.post(f'/sales/imports/{batch_id}/campaign/{self.template_id}',data={
            'sales_csrf':self.token,'audience':'group','product_id':self.product_id,
            'company_id':self.company_ids[1],'filing_branch':'Tampered branch'})
        self.assertEqual(response.status_code,200)
        self.assertIn(b'Preview your message',response.data)
        with self.client.session_transaction() as session:nonce=session['sales_preview']['nonce']
        response=self.client.post(f'/sales/imports/{batch_id}/submit',data={'sales_csrf':self.token,'nonce':nonce})
        self.assertEqual(response.status_code,302)
        campaign_id=ClientImportCampaign.query.filter_by(batch_id=batch_id).one().campaign_id
        return campaign_id,response.location

    def test_selected_branch_overrides_filename_and_retains_original_download(self):
        self.seed();bid,path=self.upload(0)
        batch=db.session.get(ClientImportBatch,bid)
        self.assertEqual(batch.company_id,self.company_ids[0])
        self.assertEqual(batch.filing_branch,"Martin's Brokers")
        rows=LapsedPolicy.query.join(ClientImportRow,ClientImportRow.policy_id==LapsedPolicy.id).filter(ClientImportRow.batch_id==bid).all()
        self.assertEqual(len(rows),2)
        self.assertTrue(all(p.branch=='Brokers' and p.company_id==self.company_ids[0] for p in rows))
        page=self.client.get(path)
        self.assertEqual(page.status_code,200)
        self.assertIn(b'1 Fictional Road',page.data)
        self.assertIn(b'WhatsApp</a>',page.data);self.assertIn(b'Call</a>',page.data)
        original=self.client.get(f'/sales/imports/{bid}/download')
        self.assertEqual(original.status_code,200);self.assertTrue(original.data.startswith(b'PK'))

    def test_preview_submit_and_schedule_keep_file_company_documents_and_product(self):
        self.seed();bid,_=self.upload();cid,select_path=self.create_campaign(bid)
        c=db.session.get(CommunicationCampaign,cid)
        self.assertEqual((c.company_id,c.branch,c.filing_branch,c.product_id),
            (self.company_ids[0],'Brokers',"Martin's Brokers",self.product_id))
        policy_id=ClientImportRow.query.filter_by(batch_id=bid).first().policy_id
        with patch('app.routes.communications._refresh_template_status',return_value=SimpleNamespace(ok=True)):
            response=self.client.post(select_path,data={'sales_csrf':self.token,'policy_ids':[policy_id],
                'action':'schedule','scheduled_at':'2030-11-16T10:30'})
        self.assertEqual(response.status_code,200)
        self.assertIn(b'window.alert',response.data)
        self.assertIn(b'window.location.replace',response.data)
        c=db.session.get(CommunicationCampaign,cid)
        self.assertEqual(c.scheduled_at,datetime(2030,11,16,8,30))
        self.assertEqual(CampaignRecipient.query.filter_by(campaign_id=cid).count(),1)

    def test_send_now_records_only_selected_file_clients_without_provider_calls_in_request(self):
        self.seed();bid,_=self.upload();cid,path=self.create_campaign(bid)
        policy_id=ClientImportRow.query.filter_by(batch_id=bid).first().policy_id
        with patch('app.routes.communications._refresh_template_status',return_value=SimpleNamespace(ok=True)), \
             patch('app.routes.communications.send_whatsapp_template_image') as provider:
            response=self.client.post(path,data={'sales_csrf':self.token,'policy_ids':[policy_id],'action':'send'})
            self.assertEqual(response.status_code,200);provider.assert_not_called()
        self.assertIn(b'Sending messages',response.data)
        campaign=db.session.get(CommunicationCampaign,cid)
        self.assertEqual(campaign.queue_status,'queued')
        progress=self.client.get(f'/sales/campaign/{cid}/progress').json
        self.assertFalse(progress['done']);self.assertEqual(progress['total'],1)
        campaign=db.session.get(CommunicationCampaign,cid)
        recipient=CampaignRecipient.query.filter_by(campaign_id=cid).one();recipient.whatsapp_status='Sent'
        campaign.queue_status='completed';db.session.commit()
        progress=self.client.get(f'/sales/campaign/{cid}/progress').json
        self.assertTrue(progress['done']);self.assertEqual(progress['sent'],1)

    def test_foreign_file_client_and_csrf_are_rejected(self):
        self.seed();bid,_=self.upload();cid,path=self.create_campaign(bid)
        foreign=LapsedPolicy(initials='Foreign',surname='Client',cell_number='0821112222',branch='Brokers',company_id=self.company_ids[0])
        db.session.add(foreign);db.session.commit();fid=foreign.id
        self.assertEqual(self.client.post(path,data={'policy_ids':[fid],'action':'send'}).status_code,400)
        with patch('app.routes.communications._refresh_template_status') as refresh:
            response=self.client.post(path,data={'sales_csrf':self.token,'policy_ids':[fid],'action':'send'})
            self.assertIn(b'Select clients from this imported file',response.data);refresh.assert_not_called()
        self.assertEqual(CampaignRecipient.query.filter_by(campaign_id=cid).count(),0)

    def test_batch_results_show_unsent_file_clients_and_preserve_scope(self):
        self.seed();bid,_=self.upload()
        response=self.client.get(f'/communications/clients?batch_id={bid}')
        self.assertEqual(response.status_code,200)
        self.assertIn(b'November list',response.data);self.assertIn(b'Resend failed messages',response.data)
        self.assertIn(b'Not sent',response.data)
        self.login('other')
        self.assertEqual(self.client.get(f'/sales/imports/{bid}').status_code,403)

    def test_failed_retry_is_limited_to_file_and_branch(self):
        self.seed();bid,_=self.upload();cid,_=self.create_campaign(bid)
        pid=ClientImportRow.query.filter_by(batch_id=bid).first().policy_id
        db.session.add(CampaignRecipient(campaign_id=cid,lapsed_policy_id=pid,secure_token='failed-workflow',whatsapp_status='Failed'))
        db.session.commit()
        with patch('app.routes.communications._refresh_template_status',return_value=SimpleNamespace(ok=True)):
            response=self.client.post(f'/sales/imports/{bid}/retry',data={'sales_csrf':self.token})
        self.assertEqual(response.status_code,302)
        self.assertEqual(db.session.get(CommunicationCampaign,cid).queue_status,'queued')

    def test_bad_workbook_leaves_no_partial_batch(self):
        import io
        self.seed()
        response=self.client.post('/sales/imports',data={'sales_csrf':self.token,'company_id':self.company_ids[0],
            'description':'Broken','file':(io.BytesIO(b'not an excel file'),'broken.xlsx')})
        self.assertEqual(response.status_code,200)
        self.assertEqual(ClientImportBatch.query.count(),0)

    def test_northcliff_import_uses_northcliff_document_destination(self):
        self.seed();bid,_=self.upload(1)
        batch=db.session.get(ClientImportBatch,bid)
        self.assertEqual(batch.company_id,self.company_ids[1])
        self.assertEqual(batch.filing_branch,"Martin's Northcliff")
        self.assertTrue(all(p.branch=='NORTHCLIFF' for p in LapsedPolicy.query.join(
            ClientImportRow,ClientImportRow.policy_id==LapsedPolicy.id).filter(ClientImportRow.batch_id==bid)))

    def test_message_date_filter_uses_south_african_day(self):
        from app.models import WhatsAppContact,WhatsAppConversation,WhatsAppMessage
        self.seed();bid,_=self.upload();cid,_=self.create_campaign(bid)
        pid=ClientImportRow.query.filter_by(batch_id=bid).first().policy_id
        recipient=CampaignRecipient(campaign_id=cid,lapsed_policy_id=pid,secure_token='sast-history',whatsapp_status='Delivered')
        contact=WhatsAppContact(wa_id='27821234567',phone_number='27821234567',branch='Brokers')
        db.session.add_all([recipient,contact]);db.session.flush()
        conversation=WhatsAppConversation(contact_id=contact.id)
        db.session.add(conversation);db.session.flush()
        db.session.add(WhatsAppMessage(conversation_id=conversation.id,campaign_recipient_id=recipient.id,
            direction='outbound',status='delivered',created_at=datetime(2026,11,1,22,30)))
        db.session.commit()
        page=self.client.get(f'/communications/clients?batch_id={bid}&sent_from=2026-11-02&sent_to=2026-11-02')
        self.assertIn(b'1 matching client(s)',page.data)
        page=self.client.get(f'/communications/clients?batch_id={bid}&sent_from=2026-11-01&sent_to=2026-11-01')
        self.assertIn(b'0 matching client(s)',page.data)

    def test_existing_file_opens_without_reimporting_or_sending(self):
        self.seed()
        policy=LapsedPolicy(initials='Saved',surname='Client',cell_number='0829998888',company_id=self.company_ids[0],
            company_name="Martin's Brokers",branch='Brokers',source_filename='Existing.xlsx',franchise='Existing')
        db.session.add(policy);db.session.commit();pid=policy.id
        with patch('app.routes.communications.send_whatsapp_template_image') as provider:
            response=self.client.post('/sales/imports/open-existing',data={'sales_csrf':self.token,
                'company_id':self.company_ids[0],'filename':'Existing.xlsx'})
            self.assertEqual(response.status_code,302);provider.assert_not_called()
        self.assertEqual(ClientImportRow.query.one().policy_id,pid)
        self.assertEqual(LapsedPolicy.query.filter_by(source_filename='Existing.xlsx').count(),1)

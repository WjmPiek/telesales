import unittest
from datetime import date
import test_laps_report as fixtures
from app import db
from app.models import LapsedPolicy, CommunicationCampaign, CampaignRecipient


class ImportDeliveryVisibilityTests(unittest.TestCase):
    setUp = fixtures.LapsReportTests.setUp
    tearDown = fixtures.LapsReportTests.tearDown
    login = fixtures.LapsReportTests.login
    workbook = staticmethod(fixtures.LapsReportTests.workbook)
    import_report = fixtures.LapsReportTests.import_report
    lead = fixtures.LapsReportTests.lead

    def seed(self):
        response = self.import_report(rows=[
            ['SENT-CLIENT','111','A CONTACTED','1 Example Road','0821234567','',date(2024,1,5),1,''],
            ['UNSENT-CLIENT','222','B UNCONTACTED','2 Example Road','0821234568','',date(2024,1,6),1,''],
            ['FAILED-CLIENT','333','C FAILED','3 Example Road','0821234569','',date(2024,1,7),1,''],
        ])
        sent = self.lead('SENT-CLIENT')
        failed = self.lead('FAILED-CLIENT')
        campaign = CommunicationCampaign(name='Visible history',message_body='Test only',branch=sent.branch,
                                         created_by_id=self.user_ids['admin'])
        db.session.add(campaign);db.session.flush()
        db.session.add_all([
            CampaignRecipient(campaign_id=campaign.id,lapsed_policy_id=sent.id,secure_token='visible-sent',whatsapp_status='Read'),
            CampaignRecipient(campaign_id=campaign.id,lapsed_policy_id=failed.id,secure_token='visible-failed',whatsapp_status='Failed'),
        ])
        db.session.commit()
        return response

    def test_successful_import_opens_file_and_lists_saved_details(self):
        response = self.seed()
        self.assertIn('source_file=Northcliff',response.location)
        page = self.client.get(response.location)
        self.assertEqual(page.status_code,200)
        for text in [b'Your imported files',b'Northcliff Laps Report.xlsx',b'1 Example Road',b'111',
                     b'CONTACTED',b'UNCONTACTED',b'Read',b'Not sent',b'Failed']:
            self.assertIn(text,page.data)
        self.assertIn(b'Sent / not sent',page.data)

    def test_never_selected_clients_show_in_whatsapp_list_and_not_sent_filter(self):
        self.seed()
        page=self.client.get('/communications/clients?source_file=Northcliff+Laps+Report.xlsx')
        self.assertEqual(page.status_code,200)
        self.assertIn(b'3 matching client(s)',page.data)
        self.assertIn(b'B UNCONTACTED',page.data)
        self.assertIn(b'2 Example Road',page.data)
        self.assertIn(b'222',page.data)
        page=self.client.get('/communications/clients?status=not_sent&source_file=Northcliff+Laps+Report.xlsx')
        self.assertIn(b'B UNCONTACTED',page.data)
        self.assertIn(b'C FAILED',page.data)
        self.assertNotIn(b'A CONTACTED',page.data)
        sent=self.client.get('/communications/clients?status=sent&source_file=Northcliff+Laps+Report.xlsx')
        self.assertIn(b'A CONTACTED',sent.data)
        self.assertNotIn(b'B UNCONTACTED',sent.data)

    def test_main_directory_file_and_delivery_filters_combine(self):
        self.seed()
        response=self.client.get('/client-files/?source_file=Northcliff+Laps+Report.xlsx&delivery=not_sent')
        self.assertEqual(response.status_code,200)
        self.assertIn(b'2 matching client records',response.data)
        self.assertNotIn(b'A CONTACTED',response.data)
        self.assertIn(b'B UNCONTACTED',response.data)
        response=self.client.get('/client-files/?source_file=Northcliff+Laps+Report.xlsx&delivery=read')
        self.assertIn(b'1 matching client records',response.data)
        self.assertIn(b'A CONTACTED',response.data)
        self.assertNotIn(b'B UNCONTACTED',response.data)
        self.assertEqual(self.client.get('/client-files/?delivery=invalid').status_code,400)

    def test_recorded_send_date_renders_in_directory_and_import_queue(self):
        from datetime import datetime
        from app.models import WhatsAppContact, WhatsAppConversation, WhatsAppMessage
        self.seed()
        recipient=CampaignRecipient.query.filter_by(secure_token='visible-sent').one()
        contact=WhatsAppContact(wa_id='27821234567',phone_number='27821234567',branch='A')
        db.session.add(contact);db.session.flush()
        conversation=WhatsAppConversation(contact_id=contact.id)
        db.session.add(conversation);db.session.flush()
        db.session.add(WhatsAppMessage(conversation_id=conversation.id,campaign_recipient_id=recipient.id,
            direction='outbound',message_type='template',status='read',created_at=datetime(2026,11,1,8,30)))
        db.session.commit()
        for path in ['/client-files/','/recovery/','/communications/clients']:
            response=self.client.get(path+'?source_file=Northcliff+Laps+Report.xlsx')
            self.assertEqual(response.status_code,200)
            self.assertIn(b'01 Nov 2026 10:30',response.data)

    def test_file_filter_chooses_details_from_that_file_for_shared_number(self):
        self.seed()
        duplicate=LapsedPolicy(initials='OTHER',surname='FILE DETAILS',cell_number='0821234568',branch='A',
                                source_filename='Other File.xlsx',franchise='Other franchise',address='Other address')
        db.session.add(duplicate);db.session.commit()
        response=self.client.get('/communications/clients?source_file=Northcliff+Laps+Report.xlsx&q=0821234568')
        self.assertIn(b'B UNCONTACTED',response.data)
        self.assertIn(b'2 Example Road',response.data)
        self.assertNotIn(b'OTHER FILE DETAILS',response.data)

    def test_branch_scope_hides_other_imports_and_history(self):
        self.seed()
        db.session.add(LapsedPolicy(initials='PRIVATE',surname='BRANCH',cell_number='0841112222',branch='B',
                                   source_filename='Private Import.xlsx',franchise='Private franchise'))
        db.session.commit()
        self.login('manager')
        for path in ['/client-files/','/recovery/','/communications/clients']:
            response=self.client.get(path)
            self.assertEqual(response.status_code,200)
            self.assertNotIn(b'PRIVATE BRANCH',response.data)
            self.assertNotIn(b'Private Import.xlsx',response.data)

    def test_status_and_file_are_retained_in_history_pagination(self):
        self.login('admin')
        for i in range(52):
            db.session.add(LapsedPolicy(initials='PAGE',surname=str(i),branch='A',cell_number=f'08299{i:05d}',
                                       source_filename='Pages.xlsx',franchise='Pages'))
        db.session.commit()
        response=self.client.get('/communications/clients?source_file=Pages.xlsx&status=not_sent')
        self.assertEqual(response.status_code,200)
        self.assertIn(b'Page 1 of 2',response.data)
        self.assertIn(b'source_file=Pages.xlsx',response.data)
        self.assertIn(b'status=not_sent',response.data)

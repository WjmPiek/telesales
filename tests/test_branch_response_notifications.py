import os
import unittest
from datetime import datetime
from unittest.mock import patch
import test_analytics_security as fixtures
from app import db
from app.models import (LapsedPolicy,CompanyGroupState,BranchFilingOffice,CommunicationCampaign,CampaignRecipient,
    ClientImportBatch,ClientImportRow,ClientImportCampaign,CallbackEmailNotification,ClientApplication,
    ClientCommunication,WhatsAppContact,WhatsAppConversation,WhatsAppMessage,AuditLog)
from app.services.communication_service import record_callback
from app.services.callback_notifications import process_callback_emails
from app.services.email_service import submission_notification_recipients


class BranchResponseNotificationTests(unittest.TestCase):
    setUp=fixtures.AnalyticsSecurityTests.setUp
    tearDown=fixtures.AnalyticsSecurityTests.tearDown
    login=fixtures.AnalyticsSecurityTests.login

    def seed(self):
        company=CompanyGroupState(company_name="Martin's Brokers",branch='Brokers',status='Active')
        office=BranchFilingOffice(branch_name="Martin's Brokers",branch_key="martin's brokers",email='brokers@example.test',active=True)
        other=BranchFilingOffice(branch_name='Northcliff',branch_key='northcliff',email='northcliff@example.test',active=True,is_default=True)
        db.session.add_all([company,office,other]);db.session.flush()
        policy=LapsedPolicy(initials='Test',surname='Callback',cell_number='0821234567',policy_number='TEST-1',
            branch='Brokers',company_id=company.id,source_filename='Krugersdorp Laps Report.xlsx')
        campaign=CommunicationCampaign(name='Krugersdorp',branch='Brokers',company_id=company.id,
            filing_branch=office.branch_name,message_body='Test',created_by_id=self.user_ids['admin'])
        batch=ClientImportBatch(filename=policy.source_filename,description='Krugersdorp November',company_id=company.id,
            filing_branch=office.branch_name,created_by_id=self.user_ids['admin'])
        db.session.add_all([policy,campaign,batch]);db.session.flush()
        recipient=CampaignRecipient(campaign_id=campaign.id,lapsed_policy_id=policy.id,secure_token='private-callback-token')
        db.session.add_all([recipient,ClientImportRow(batch_id=batch.id,policy_id=policy.id),ClientImportCampaign(batch_id=batch.id,campaign_id=campaign.id)])
        db.session.commit()
        return policy,recipient

    def test_callback_queues_branch_email_once_and_sender_uses_correct_office(self):
        policy,recipient=self.seed()
        with patch('app.services.email_service.send_email') as send:
            record_callback(recipient,'whatsapp');db.session.commit()
            record_callback(recipient,'whatsapp');db.session.commit()
            send.assert_not_called()
        self.assertEqual(CallbackEmailNotification.query.count(),1)
        with patch('app.services.email_service.send_email',return_value=True) as send:
            self.assertEqual(process_callback_emails(),1)
            self.assertEqual(process_callback_emails(),0)
        self.assertEqual(send.call_count,1)
        self.assertEqual(send.call_args.args[0],'brokers@example.test')
        body=send.call_args.args[2]
        for expected in ['Brokers','Krugersdorp November','Test Callback','0821234567','SAST','staff login required']:
            self.assertIn(expected,body)
        self.assertNotIn('private-callback-token',body)
        self.assertEqual(CallbackEmailNotification.query.one().status,'Accepted by mail server')

    def test_failed_callback_email_is_visible_without_losing_callback(self):
        policy,recipient=self.seed();record_callback(recipient,'whatsapp');db.session.commit()
        with patch('app.services.email_service.send_email',return_value=False):process_callback_emails()
        self.assertEqual(policy.recovery_status,'Callback')
        self.assertEqual(CallbackEmailNotification.query.one().status,'Failed')
        self.login('admin')
        page=self.client.get('/recovery/callbacks').data
        self.assertIn(b'brokers@example.test',page);self.assertIn(b'Failed',page)

    def test_missing_branch_office_does_not_send_to_default_other_branch(self):
        _,recipient=self.seed();recipient.campaign.filing_branch='Missing office'
        record_callback(recipient,'whatsapp');db.session.commit()
        with patch('app.services.email_service.send_email') as send:
            process_callback_emails();send.assert_not_called()
        self.assertEqual(CallbackEmailNotification.query.one().status,'Not configured')

    def test_application_office_keeps_additional_requested_copy(self):
        self.seed()
        application=ClientApplication(application_ref='TEST-OFFICE',filing_branch="Martin's Brokers")
        with patch.dict(os.environ,{'MAIL_DOCUMENTS_TO':'wrong-global@example.test','APPLICATION_SUBMISSION_NOTIFY':'wjm@martinsdirect.com'}):
            self.assertEqual(submission_notification_recipients('client@example.test',application),
                             ['brokers@example.test','wjm@martinsdirect.com'])

    def test_application_list_shows_source_dates_submission_and_email_attempt(self):
        policy,recipient=self.seed()
        a=ClientApplication(application_ref='BRANCH-DRAFT',first_names='Draft',surname='Client',branch='Brokers',
            filing_branch="Martin's Brokers",lapsed_policy_id=policy.id,source_campaign_recipient_id=recipient.id,
            status='Draft',created_at=datetime(2026,11,17,10,0))
        signed=ClientApplication(application_ref='BRANCH-SIGNED',first_names='Signed',surname='Client',branch='Brokers',
            filing_branch="Martin's Brokers",lapsed_policy_id=policy.id,source_campaign_recipient_id=recipient.id,
            status='Signed',signed_at=datetime(2026,11,17,11,0))
        contact=WhatsAppContact(wa_id='27821234567',phone_number='27821234567')
        db.session.add_all([a,signed,contact]);db.session.flush()
        conversation=WhatsAppConversation(contact_id=contact.id)
        db.session.add(conversation);db.session.flush()
        db.session.add(WhatsAppMessage(conversation_id=conversation.id,campaign_recipient_id=recipient.id,
            direction='outbound',status='sent',body='Invitation',created_at=datetime(2026,11,17,9,0)))
        db.session.add(ClientCommunication(application_id=signed.id,channel='Email',direction='outbound',
            subject='Signed documents received: BRANCH-SIGNED',body='Staff notification',status='Accepted by mail server',
            occurred_at=datetime(2026,11,17,11,1)))
        import json
        for address,status in [('brokers@example.test','Failed'),('wjm@example.test','Accepted by mail server')]:
            db.session.add(AuditLog(action='APPLICATION_SUBMISSION_EMAIL',entity_type='ClientApplication',
                entity_id=str(signed.id),details=json.dumps({'to':address,'status':status})))
        db.session.commit();self.login('admin')
        page=self.client.get('/applications/?branch=Brokers').get_data(as_text=True)
        for expected in ['Krugersdorp November','Krugersdorp Laps Report.xlsx','17 Nov 2026 11:00',
                         '17 Nov 2026 12:00','17 Nov 2026 13:00','Not submitted','Accepted by mail server','brokers@example.test']:
            self.assertIn(expected,page)
        self.assertNotIn('BRANCH-DRAFT',self.client.get('/applications/?status=signed').get_data(as_text=True))
        from app.services.application_sources import application_sources
        with self.app.test_request_context():
            current=ClientApplication.query.filter_by(application_ref='BRANCH-SIGNED').one()
            self.assertEqual(application_sources([current])[current.id]['email_status'],'Failed')
        self.login('manager')
        self.assertNotIn(b'BRANCH-SIGNED',self.client.get('/applications/?branch=Brokers').data)

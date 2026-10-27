import os
import unittest
from contextlib import ExitStack
from unittest.mock import patch

import test_analytics_security as fixtures
from app import db
from app.models import ClientApplication, DocumentSignature, User
from app.services.email_service import submission_notification_recipients


class RecipientTests(unittest.TestCase):
    def test_owner_is_added_without_replacing_office_recipients(self):
        with patch.dict(os.environ, {'MAIL_DOCUMENTS_TO':'Office <office@example.com>',
                                   'APPLICATION_SUBMISSION_NOTIFY':'wjm@martinsdirect.com'}):
            self.assertEqual(submission_notification_recipients('client@example.com'),
                             ['office@example.com', 'wjm@martinsdirect.com'])

    def test_duplicates_and_client_recipient_are_excluded(self):
        with patch.dict(os.environ, {'MAIL_DOCUMENTS_TO':'Office <office@example.com>, client@example.com',
                                   'APPLICATION_SUBMISSION_NOTIFY':'OFFICE@example.com; wjm@martinsdirect.com;wjm@martinsdirect.com'}):
            self.assertEqual(submission_notification_recipients('Client <CLIENT@example.com>'),
                             ['office@example.com', 'wjm@martinsdirect.com'])

    def test_new_recipient_works_without_an_existing_office_address(self):
        with patch.dict(os.environ, {'MAIL_DOCUMENTS_TO':'', 'APPLICATION_SUBMISSION_NOTIFY':'wjm@martinsdirect.com'}):
            self.assertEqual(submission_notification_recipients(), ['wjm@martinsdirect.com'])

    def test_empty_configuration_keeps_notifications_disabled(self):
        with patch.dict(os.environ, {'MAIL_DOCUMENTS_TO':'', 'APPLICATION_SUBMISSION_NOTIFY':''}):
            self.assertEqual(submission_notification_recipients(), [])


class SubmissionNotificationTests(unittest.TestCase):
    setUp = fixtures.AnalyticsSecurityTests.setUp
    tearDown = fixtures.AnalyticsSecurityTests.tearDown

    def test_final_submission_sends_private_staff_links_to_both_recipients(self):
        from flask_login import login_user
        from app.routes.signing import finish_application
        application = ClientApplication(application_ref='NOTIFY-TEST', first_names='Example', surname='Client',
                                        email='client@example.com', branch='A', agent_id=self.user_ids['admin'],
                                        payment_method='Debit Order', sign_token='test-only-token')
        db.session.add(application)
        db.session.flush()
        app_id = application.id
        for kind in ['application:principal','popia','disclosure','welcome','cdd']:
            db.session.add(DocumentSignature(application_id=app_id, document_type=kind,
                                             typed_name='Example Client', signature_image_path='test.png'))
        db.session.commit()
        with self.app.test_request_context('/sign/test-only-token', method='POST'), ExitStack() as stack:
            login_user(db.session.get(User, self.user_ids['admin']))
            stack.enter_context(patch.dict(os.environ, {'MAIL_DOCUMENTS_TO':'office@example.com',
                                                       'APPLICATION_SUBMISSION_NOTIFY':'wjm@martinsdirect.com'}))
            for name, value in [
                ('app.services.delivery_preferences.receipt_address','client@example.com'),
                ('app.services.company_documents.active_company_documents',[]),
                ('app.routes.signing.assert_application_rules',(True,[])),
                ('app.routes.signing._signed_doc_types',{'application','popia','disclosure','welcome','cdd'}),
                ('app.routes.signing._fica_status',([],[],[],[])),
                ('app.routes.signing.application_folder',self.tmp.name),
                ('app.routes.signing._resolve_existing','already-saved.pdf'),
                ('app.routes.signing.send_supporting_upload_email',True),
                ('app.services.document_reminders.schedule_document_reminders',None),
            ]:
                stack.enter_context(patch(name, return_value=value))
            delivery = stack.enter_context(patch('app.routes.signing.send_email', return_value=True))
            finish_application(db.session.get(ClientApplication, app_id), 'test-only-token')
            self.assertEqual([call.args[0] for call in delivery.call_args_list],
                             ['office@example.com','wjm@martinsdirect.com'])
            for call in delivery.call_args_list:
                self.assertIn('Signed documents received: NOTIFY-TEST',call.args[1])
                self.assertIn(f'/client-files/?application_id={app_id}',call.args[2])
                self.assertNotIn('test-only-token',call.args[2])
                self.assertEqual(call.kwargs['application_id'],app_id)
                self.assertNotIn('attachments',call.kwargs)
            self.assertEqual(db.session.get(ClientApplication,app_id).status,'Signed')


if __name__ == '__main__':
    unittest.main()

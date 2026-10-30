import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from contextlib import ExitStack
import test_analytics_security as fixtures
from app import db
from app.models import ClientApplication, ApplicationJourney, CompanyDocument
from app.services.franchise_routing import destination_company


class FranchisePaymentRoutingTests(unittest.TestCase):
    setUp=fixtures.AnalyticsSecurityTests.setUp
    tearDown=fixtures.AnalyticsSecurityTests.tearDown

    def activation(self, northcliff, missing=False):
        from app.services.online_application import notify_activation
        north=destination_company(True); brokers=destination_company()
        for company,content in [(north,b'%PDF-Northcliff-bank'),(brokers,b'%PDF-Brokers-bank')]:
            if missing and company.id == (north.id if northcliff else brokers.id):
                continue
            db.session.add(CompanyDocument(company_id=company.id,category='bank_confirmation',original_filename='bank.pdf',
                file_data=content,file_size=len(content),checksum_sha256='a'*64,uploaded_by_id=self.user_ids['admin']))
        company=north if northcliff else brokers
        application=ClientApplication(application_ref='STOP-TEST',branch=company.branch,company_id=company.id,
            payment_method='Stop Order',email='client@example.test',first_names='Test',surname='Client')
        db.session.add(application); db.session.flush()
        db.session.add(ApplicationJourney(application_id=application.id,activated_at=datetime.utcnow()))
        db.session.commit()
        def pdf(application,path,**kwargs):
            Path(path).write_bytes(b'%PDF-test')
        found=[]
        def mail(to,subject,body,attachments,**kwargs):
            found.extend(Path(path).read_bytes() for path in attachments)
            return True
        with ExitStack() as stack:
            for name in ['generate_application_pdf','generate_welcome_pack','generate_popia_pdf','generate_disclosure_pdf']:
                stack.enter_context(patch('app.services.pdf_service.'+name,side_effect=pdf))
            stack.enter_context(patch('app.services.cdd_service.generate_cdd_pdf',side_effect=pdf))
            stack.enter_context(patch('app.services.online_application.assert_application_rules',return_value=(True,[])))
            stack.enter_context(patch('app.services.cover_eligibility.activation_identity_errors',return_value=[]))
            stack.enter_context(patch('app.services.cover_eligibility.coverage_report',return_value={'blocked':False}))
            sender=stack.enter_context(patch('app.services.email_service.send_email',side_effect=mail))
            generic=stack.enter_context(patch('app.services.email_service.business_bank_confirmation_attachment'))
            result=notify_activation(application)
            generic.assert_not_called()
            if missing:
                sender.assert_not_called();self.assertFalse(result)
                self.assertEqual(application.whatsapp_journey.notice_status,'Failed')
            else:
                self.assertTrue(result);self.assertEqual(sender.call_count,1)
                expected=b'%PDF-Northcliff-bank' if northcliff else b'%PDF-Brokers-bank'
                other=b'%PDF-Brokers-bank' if northcliff else b'%PDF-Northcliff-bank'
                self.assertIn(expected,found);self.assertNotIn(other,found)

    def test_northcliff_stop_order_uses_only_northcliff_bank(self):
        self.activation(True)

    def test_brokers_stop_order_uses_only_brokers_bank(self):
        self.activation(False)

    def test_missing_branch_bank_does_not_send_another_company_letter(self):
        self.activation(True,missing=True)

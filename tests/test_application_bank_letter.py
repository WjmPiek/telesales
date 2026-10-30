import io
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch
from reportlab.pdfgen import canvas
from werkzeug.datastructures import FileStorage
import test_application_workflow as fixture
from app import db
from app.models import BranchFilingOffice, ComplianceReview
from app.services.application_bank_letter import saved_letter, save_letter


class ApplicationBankLetterTests(unittest.TestCase):
    baseSetUp=fixture.ApplicationWorkflowTests.baseSetUp
    setUp=fixture.ApplicationWorkflowTests.setUp
    tearDown=fixture.ApplicationWorkflowTests.tearDown
    login=fixture.ApplicationWorkflowTests.login
    record=fixture.ApplicationWorkflowTests.record
    complete_agent_file=fixture.ApplicationWorkflowTests.complete_agent_file

    @staticmethod
    def pdf():
        stream=io.BytesIO();c=canvas.Canvas(stream);c.drawString(40,800,'Chosen branch bank confirmation');c.save();stream.seek(0)
        return stream

    def data(self):
        from app.routes.qa import QA_CHECKLIST
        self.complete_agent_file()
        db.session.add(BranchFilingOffice(branch_key='test office',branch_name='Test Office',email='office@example.test',is_default=True))
        db.session.commit()
        return {**{key:'on' for key,label in QA_CHECKLIST},'decision':'QA Approved',
                'start_date':date.today().isoformat(),'filing_branch':'Test Office','notes':'All items checked'}

    def test_missing_letter_preserves_review_and_does_not_activate_or_send(self):
        data=self.data()
        with patch('app.services.online_application.notify_activation') as send:
            response=self.client.post(f'/qa/application/{self.aid}',data=data,follow_redirects=True)
            send.assert_not_called()
        self.assertIn(b'Attach the bank confirmation letter',response.data)
        self.assertIn(b'All items checked',response.data)
        self.assertIn(b'id="beneficiary" name="beneficiary" required checked',response.data)
        self.assertIsNone(self.record.whatsapp_journey.activated_at)
        self.assertNotEqual(self.record.status,'Active')
        self.assertEqual(ComplianceReview.query.count(),0)

    def test_uploaded_letter_is_in_activation_email_once_and_not_replaced_by_global(self):
        data=self.data();data['bank_confirmation_letter']=(self.pdf(),'my-branch.pdf')
        attachments=[]
        def mail(to,subject,body,paths,**kwargs):
            attachments.extend((Path(path).name,Path(path).read_bytes()) for path in paths)
            return True
        with patch('app.services.email_service.send_email',side_effect=mail), \
             patch('app.services.email_service.business_bank_confirmation_attachment') as generic, \
             patch('app.services.branch_filing.send_branch_pack') as filing:
            self.client.post(f'/qa/application/{self.aid}',data=data)
            generic.assert_not_called()
        self.assertEqual(self.record.status,'Active')
        self.assertEqual(self.record.whatsapp_journey.notice_status,'Sent')
        row=saved_letter(self.record)
        self.assertIsNotNone(row)
        self.assertEqual([content for name,content in attachments if name=='bank_confirmation_letter.pdf'],[row.content])
        self.assertEqual(len(attachments),6)

    def test_invalid_pdf_is_rejected_and_another_branch_cannot_attach(self):
        data=self.data();data['bank_confirmation_letter']=(io.BytesIO(b'%PDF-invalid'),'bank.pdf')
        response=self.client.post(f'/qa/application/{self.aid}',data=data,follow_redirects=True)
        self.assertIn(b'readable, unencrypted',response.data)
        self.assertIsNone(saved_letter(self.record))
        self.assertIsNone(self.record.whatsapp_journey.activated_at)
        self.login('other')
        self.assertEqual(self.client.post(f'/qa/application/{self.aid}',data={**data,'bank_confirmation_letter':(self.pdf(),'bank.pdf')}).status_code,403)

    def test_letter_is_not_reused_after_branch_or_company_changes(self):
        save_letter(self.record,FileStorage(stream=self.pdf(),filename='bank.pdf'),self.user_ids['admin'])
        db.session.commit()
        self.assertIsNotNone(saved_letter(self.record))
        self.record.branch='Brokers'
        self.assertIsNone(saved_letter(self.record))

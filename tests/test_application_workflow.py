import io
import json
import unittest
from datetime import datetime
from unittest.mock import patch
from werkzeug.datastructures import FileStorage
from PIL import Image
import test_member_additions as fixture
from app import db
from app.models import ClientFicaDocument, AuditLog
from app.services.application_workflow import workflow_state, advance_to_quality


class ApplicationWorkflowTests(unittest.TestCase):
    baseSetUp=fixture.MemberAdditionTests.baseSetUp
    setUp=fixture.MemberAdditionTests.setUp
    tearDown=fixture.MemberAdditionTests.tearDown
    login=fixture.MemberAdditionTests.login
    record=fixture.MemberAdditionTests.record

    def complete_agent_file(self):
        self.record.total_members=2
        rows=json.loads(self.record.product_dependents_json)
        rows[0]['id_or_dob']='0908155312089'
        self.record.product_dependents_json=json.dumps(rows)
        self.record.document_email='documents@example.test'
        for kind,status in [('member_1_id_copy','Needs Review'),('proof_of_address','Approved')]:
            db.session.add(ClientFicaDocument(application_id=self.aid,document_type=kind,status=status,
                original_filename=kind+'.png',file_path=self.record.signed_pdf_path))
        from app.services.screening_service import save_employee_check
        photo=io.BytesIO();Image.new('RGB',(100,100),'blue').save(photo,format='PNG');photo.seek(0)
        save_employee_check(self.record,[FileStorage(stream=photo,filename='fic.png')],'no_match',
            'Completed the identity check; no matches found.',datetime.utcnow(),self.user_ids['admin'])
        db.session.commit()
        return ClientFicaDocument.query.filter_by(application_id=self.aid,document_type='member_1_id_copy').one()

    def request_nonce(self):
        response=self.client.get(f'/documents/application/{self.aid}')
        self.assertEqual(response.status_code,200)
        with self.client.session_transaction() as s:return s['document_request_csrf']

    def test_last_accept_routes_to_quality_without_activating_or_sending(self):
        doc=self.complete_agent_file();doc_id=doc.id
        self.assertFalse(workflow_state(self.record)['ready'])
        with patch('app.routes.documents.send_email') as mail:
            response=self.client.post(f'/documents/fica/{doc.id}/approve')
            mail.assert_not_called()
        self.assertTrue(response.location.endswith(f'/qa/application/{self.aid}'))
        self.assertEqual(self.record.status,'QA Pending')
        self.assertIsNone(self.record.whatsapp_journey.activated_at)
        self.assertTrue(workflow_state(self.record)['ready'])
        self.assertEqual(AuditLog.query.filter_by(action='Ready for quality review').count(),1)
        self.client.post(f'/documents/fica/{doc_id}/approve')
        self.assertEqual(AuditLog.query.filter_by(action='Ready for quality review').count(),1)
        page=self.client.get(f'/qa/application/{self.aid}')
        self.assertIn(b'Approve and activate',page.data)
        self.assertNotIn(b'Compliance Final Approve',page.data)

    def test_id_or_count_gap_keeps_file_at_agent_check(self):
        doc=self.complete_agent_file();self.record.total_members=3;db.session.commit()
        response=self.client.post(f'/documents/fica/{doc.id}/approve')
        self.assertIn('/documents/application/',response.location)
        self.assertNotEqual(self.record.status,'QA Pending')
        self.record.total_members=2
        self.record.product_dependents_json=self.record.product_dependents_json.replace('0908155312089','2009-08-15')
        db.session.commit()
        self.assertFalse(advance_to_quality(self.record))
        self.assertTrue(workflow_state(self.record)['missing_ids'])

    def test_combined_request_requires_csrf_explains_replacement_and_preserves_accepted_files(self):
        doc=self.complete_agent_file();nonce=self.request_nonce();url=f'/documents/application/{self.aid}/request-outstanding'
        original=self.record.sign_token
        with patch('app.routes.documents.send_email',return_value=True) as mail:
            self.assertEqual(self.client.post(url,data={'items':'member_1_id_copy'}).status_code,400)
            self.client.post(url,data={'document_request_csrf':nonce,'items':'member_1_id_copy'})
            mail.assert_not_called()
            response=self.client.post(url,data={'document_request_csrf':nonce,'items':['member_1_id_copy'],
                'notes':'Please replace the blurred ID photo with a clear photograph.'})
            self.assertEqual(response.status_code,302)
        self.assertEqual(mail.call_count,1)
        self.assertEqual(mail.call_args.args[0],'documents@example.test')
        body=mail.call_args.args[2]
        self.assertIn('Existing Child',body)
        self.assertIn('blurred ID',body)
        self.assertNotIn('Employee FIC',body)
        self.assertNotIn('Proof of Address',body)
        self.assertEqual(ClientFicaDocument.query.filter_by(application_id=self.aid,document_type='member_1_id_copy').one().status,'Rejected')
        self.assertEqual(ClientFicaDocument.query.filter_by(application_id=self.aid,document_type='proof_of_address').one().status,'Approved')
        self.assertEqual(self.record.sign_token,original)
        self.assertIsNotNone(self.record.signed_at)
        self.assertEqual(self.record.status,'FICA Outstanding')
        self.assertIn(b'Accepted by mail server',self.client.get(f'/documents/application/{self.aid}').data)

    def test_failed_request_visible_and_can_retry_without_losing_files(self):
        self.complete_agent_file();nonce=self.request_nonce();url=f'/documents/application/{self.aid}/request-outstanding'
        data={'document_request_csrf':nonce,'items':'member_1_id_copy','notes':'Photo blurred; please replace.'}
        with patch('app.routes.documents.send_email',return_value=False):self.client.post(url,data=data)
        self.assertIn(b'Delivery failed',self.client.get(f'/documents/application/{self.aid}').data)
        with patch('app.routes.documents.send_email',return_value=True) as mail:self.client.post(url,data=data)
        self.assertEqual(mail.call_count,1)
        self.assertEqual(ClientFicaDocument.query.filter_by(application_id=self.aid).count(),3)

    def test_no_rejection_of_approved_or_staff_items_and_no_cross_branch_access(self):
        self.complete_agent_file();nonce=self.request_nonce();url=f'/documents/application/{self.aid}/request-outstanding'
        with patch('app.routes.documents.send_email') as mail:
            for key in ['proof_of_address','employee_fic','application','unknown']:
                self.assertEqual(self.client.post(url,data={'document_request_csrf':nonce,'items':key,'notes':'Test request'}).status_code,409)
            mail.assert_not_called()
        self.login('other')
        self.assertEqual(self.client.get(f'/documents/application/{self.aid}').status_code,403)
        self.assertEqual(self.client.post(url,data={'document_request_csrf':nonce,'notes':'No access'}).status_code,403)
        self.record.agent_id=self.user_ids['agent'];db.session.commit();self.login('agent')
        self.assertEqual(self.client.get(f'/documents/application/{self.aid}').status_code,200)
        self.record.agent_id=self.user_ids['admin'];db.session.commit()
        self.assertEqual(self.client.get(f'/documents/application/{self.aid}').status_code,403)

    def test_qa_queue_separates_ready_from_agent_work_and_closed_files_cannot_change(self):
        doc=self.complete_agent_file()
        before=self.client.get('/qa/').data
        self.assertIn(b'Agent check',before)
        self.assertNotIn(b'>Final review</a>',before)
        self.client.post(f'/documents/fica/{doc.id}/approve')
        after=self.client.get('/qa/').data
        self.assertIn(b'>Final review</a>',after)
        nonce=self.request_nonce()
        self.record.status='Active';self.record.whatsapp_journey.activated_at=datetime.utcnow();db.session.commit()
        self.assertEqual(self.client.post(f'/documents/fica/{doc.id}/reject',data={'reason':'Test'}).status_code,409)
        self.assertEqual(self.client.post(f'/documents/application/{self.aid}/request-outstanding',
            data={'document_request_csrf':nonce,'notes':'Test'}).status_code,409)

    def test_qa_shortcut_accept_uses_same_handoff_and_reject_requires_instructions(self):
        doc=self.complete_agent_file()
        self.client.post(f'/qa/fica/{doc.id}/reject')
        self.assertEqual(doc.status,'Needs Review')
        response=self.client.post(f'/qa/fica/{doc.id}/approve')
        self.assertIn('/qa/application/',response.location)
        self.assertEqual(self.record.status,'QA Pending')

import io
import json
import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import patch
from PIL import Image
import test_application_workflow as fixture
from app import db
from app.models import BranchFilingOffice, ClientFicaDocument
from app.services.application_workflow import workflow_state
from app.services.document_status_service import document_summary
from app.services.cover_eligibility import activation_identity_errors


class DobActivationTests(unittest.TestCase):
    baseSetUp=fixture.ApplicationWorkflowTests.baseSetUp
    setUp=fixture.ApplicationWorkflowTests.setUp
    tearDown=fixture.ApplicationWorkflowTests.tearDown
    login=fixture.ApplicationWorkflowTests.login
    record=fixture.ApplicationWorkflowTests.record
    complete_agent_file=fixture.ApplicationWorkflowTests.complete_agent_file

    def dob_only(self):
        doc=self.complete_agent_file();db.session.delete(doc)
        rows=json.loads(self.record.product_dependents_json);rows[0]['id_or_dob']='2010-01-01'
        self.record.product_dependents_json=json.dumps(rows);db.session.commit()

    def test_dob_only_and_missing_member_upload_does_not_block_activation_or_policy_email(self):
        from app.routes.qa import QA_CHECKLIST
        self.dob_only()
        summary=document_summary(self.record)
        self.assertFalse(summary['complete']);self.assertTrue(summary['approval_complete'])
        self.assertTrue(workflow_state(self.record)['ready'])
        self.assertEqual(activation_identity_errors(self.record),[])
        db.session.add(BranchFilingOffice(branch_key='test office',branch_name='Test Office',email='office@example.test',is_default=True));db.session.commit()
        data={key:'on' for key,label in QA_CHECKLIST};data.update(decision='QA Approved',policy_number='DOB-TEST',start_date=date.today().isoformat(),filing_branch='Test Office')
        delivery=SimpleNamespace(branch_name='Test Office',recipient_email='office@example.test',status='Accepted by mail server')
        with patch('app.services.company_documents.active_company_documents',return_value=[SimpleNamespace(category='bank_confirmation')]), \
             patch('app.services.company_documents.materialise_company_documents',return_value=([],None)), \
             patch('app.services.branch_filing.send_branch_pack',return_value=delivery), \
             patch('app.services.email_service.send_email',return_value=True) as mail:
            response=self.client.post(f'/qa/application/{self.aid}',data=data)
        self.assertEqual(response.status_code,302)
        self.assertEqual(self.record.status,'Active')
        self.assertEqual(self.record.policy_number,self.record.id_number)
        self.assertNotEqual(self.record.policy_number,'DOB-TEST')
        self.assertEqual(self.record.whatsapp_journey.notice_status,'Sent')
        self.assertIn('now active',mail.call_args.args[2]);self.assertEqual(len(mail.call_args.args[3]),5)

    def test_additional_upload_review_or_rejection_is_followup_only(self):
        doc=self.complete_agent_file()
        for status in ['Needs Review','Rejected','Received']:
            doc.status=status;db.session.flush()
            self.assertTrue(workflow_state(self.record)['ready'])
            self.assertTrue(document_summary(self.record)['approval_complete'])
            row=next(r for r in document_summary(self.record)['rows'] if r['key']=='member_1_id_copy')
            self.assertFalse(row['approval_required'])
        self.assertTrue(workflow_state(self.record)['followups'])

    def test_principal_documents_and_main_fic_check_still_block(self):
        self.dob_only()
        doc=ClientFicaDocument.query.filter_by(application_id=self.aid,document_type='id_copy').one()
        doc.status='Rejected';db.session.flush()
        self.assertFalse(workflow_state(self.record)['ready'])
        self.assertFalse(document_summary(self.record)['approval_complete'])
        doc.status='Approved';db.session.flush()
        with patch('app.services.screening_service.ensure_screened',return_value=(False,['Missing evidence'])):
            self.assertFalse(workflow_state(self.record)['ready'])
        self.record.id_number='invalid'
        self.assertTrue(activation_identity_errors(self.record))

    def test_invalid_or_missing_member_dob_still_blocks(self):
        self.dob_only()
        for value in ['', 'not-a-date','2099-01-01']:
            rows=json.loads(self.record.product_dependents_json);rows[0]['id_or_dob']=value
            self.record.product_dependents_json=json.dumps(rows);db.session.flush()
            self.assertTrue(activation_identity_errors(self.record))
            self.assertFalse(workflow_state(self.record)['ready'])

    def test_client_can_submit_main_documents_without_additional_upload(self):
        self.dob_only();public=self.app.test_client();link='/sign/previous-signing-token/supporting-documents'
        public.post(link,data={'action':'unlock','id_number':'8001015009087'})
        page=public.get(link)
        self.assertIn(b'does not block activation',page.data)
        self.assertIn(b'Submit available documents',page.data)
        result=public.post(link,data={'action':'complete'})
        self.assertIn(b'Supporting documents received',result.data)

    def test_optional_upload_does_not_revert_an_active_policy(self):
        self.dob_only();self.record.status='Active';db.session.commit()
        public=self.app.test_client();link='/sign/previous-signing-token/supporting-documents'
        public.post(link,data={'action':'unlock','id_number':'8001015009087'})
        photo=io.BytesIO();Image.new('RGB',(120,120),'blue').save(photo,format='PNG');photo.seek(0)
        self.assertEqual(public.post(link,data={'action':'upload','document_type':'member_1_id_copy','file':(photo,'member.png')},content_type='multipart/form-data').status_code,302)
        self.assertEqual(self.record.status,'Active')
        public.post(link,data={'action':'complete'})
        self.assertEqual(self.record.status,'Active')

    def test_approval_page_fills_policy_number_from_principal_id_and_locks_field(self):
        from bs4 import BeautifulSoup
        self.dob_only()
        self.record.policy_number='old-draft-number';db.session.commit()
        page=self.client.get(f'/qa/application/{self.aid}')
        field=BeautifulSoup(page.data,'html.parser').find('input',id='policy-number')
        self.assertEqual(field['value'],self.record.id_number)
        self.assertTrue(field.has_attr('readonly'))

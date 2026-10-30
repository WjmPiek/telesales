import io
import json
import re
import unittest
from datetime import datetime, timedelta, date
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image
from bs4 import BeautifulSoup
import test_member_additions as fixture
from app import db
from app.models import ApplicationMemberAddition, ApplicationMemberCountReview, BranchFilingOffice, ClientFicaDocument
from app.services.member_count_review import count_summary, confirm_current_members, process_member_count_reminders


class MemberCountReviewTests(unittest.TestCase):
    baseSetUp=fixture.MemberAdditionTests.baseSetUp
    setUp=fixture.MemberAdditionTests.setUp
    tearDown=fixture.MemberAdditionTests.tearDown
    login=fixture.MemberAdditionTests.login
    record=fixture.MemberAdditionTests.record
    issue=fixture.MemberAdditionTests.issue
    unlock=fixture.MemberAdditionTests.unlock
    add=fixture.MemberAdditionTests.add

    def gap(self):
        self.record.total_members=4;db.session.commit()

    def test_counts_requested_people_including_principal_not_unused_product_slots(self):
        self.record.total_members=2;db.session.commit()
        self.assertEqual(count_summary(self.record),{'expected':2,'captured':2,'missing':0})
        with patch('app.services.email_service.send_email',return_value=True) as mail:
            self.assertEqual(process_member_count_reminders()['sent'],0);mail.assert_not_called()
        self.gap();self.assertEqual(count_summary(self.record)['missing'],2)
        page=self.client.get(f'/applications/{self.aid}')
        self.assertIn(b'2 requested member(s) still need confirmation',page.data)
        link,nonce=self.unlock(self.issue());self.add(link,nonce)
        self.assertEqual(self.record.total_members,4)
        self.assertEqual(count_summary(self.record),{'expected':4,'captured':3,'missing':1})

    def test_automatic_email_has_two_safe_buttons_and_one_final_reminder(self):
        self.gap();now=datetime.utcnow()
        with patch('app.services.email_service.send_email',return_value=True) as mail:
            self.assertEqual(process_member_count_reminders(now=now)['sent'],1)
            self.assertEqual(mail.call_args.args[0],'client@example.test')
            buttons=BeautifulSoup(mail.call_args.kwargs['html_body'],'html.parser').find_all('a')
            self.assertEqual(len(buttons),2)
            self.assertIn('?choice=ignore',buttons[1]['href'])
            self.assertIn('requested 4',mail.call_args.args[2])
            self.assertEqual(process_member_count_reminders(now=now+timedelta(hours=23))['sent'],0)
            self.assertEqual(process_member_count_reminders(now=now+timedelta(hours=24))['sent'],1)
            self.assertEqual(process_member_count_reminders(now=now+timedelta(hours=72))['sent'],0)
            self.assertEqual(mail.call_count,2)
        self.assertEqual(ApplicationMemberAddition.query.filter_by(revoked_at=None).count(),1)

    def test_ignore_get_does_not_change_members_and_post_requires_identity_csrf_confirmation(self):
        self.gap();token=self.issue();link='/member-additions/'+token
        original=self.record.product_dependents_json;signature=self.record.sign_token
        self.assertIn(b'Confirm your identity',self.public.get(link+'?choice=ignore').data)
        self.assertEqual(self.record.total_members,4)
        link,nonce=self.unlock(token)
        page=self.public.get(link+'?choice=ignore')
        self.assertIn(b'Confirm your covered members',page.data)
        self.assertEqual(self.record.total_members,4)
        self.assertEqual(self.public.post(link+'?choice=ignore',data={'action':'ignore','confirm_current':'1'}).status_code,400)
        response=self.public.post(link+'?choice=ignore',data={'action':'ignore','nonce':nonce})
        self.assertIn(b'Confirm the current member list',response.data)
        response=self.public.post(link+'?choice=ignore',data={'action':'ignore','nonce':nonce,'confirm_current':'1'})
        self.assertEqual(response.status_code,302)
        self.assertIn(b'Current member list confirmed',self.public.get(link).data)
        self.assertEqual(self.record.total_members,2)
        self.assertEqual(self.record.product_dependents_json,original)
        self.assertEqual(self.record.sign_token,signature)
        self.assertEqual(count_summary(self.record)['missing'],0)
        self.assertIsNotNone(db.session.get(ApplicationMemberCountReview,self.aid).confirmed_at)
        with patch('app.services.email_service.send_email') as mail:
            self.assertEqual(process_member_count_reminders(now=datetime.utcnow()+timedelta(days=3))['sent'],0)
            mail.assert_not_called()

    def test_failed_automatic_delivery_keeps_previous_working_link(self):
        self.gap();token=self.issue();old=ApplicationMemberAddition.query.filter_by(revoked_at=None).one();oid=old.id
        due=datetime.utcnow()+timedelta(hours=25)
        with patch('app.services.email_service.send_email',return_value=False):
            self.assertEqual(process_member_count_reminders(now=due)['failed'],1)
        self.assertIsNone(db.session.get(ApplicationMemberAddition,oid).revoked_at)
        self.assertEqual(self.public.get('/member-additions/'+token).status_code,200)

    def test_camera_fields_and_actual_camera_file_upload(self):
        link='/sign/previous-signing-token/supporting-documents'
        self.public.post(link,data={'action':'unlock','id_number':'8001015009087'})
        page=self.public.get(link)
        soup=BeautifulSoup(page.data,'html.parser')
        camera=soup.find('input',attrs={'name':'camera_file','capture':'environment'})
        self.assertIsNotNone(camera);self.assertEqual(camera['accept'],'image/*')
        photo=io.BytesIO();Image.new('RGB',(150,150),'blue').save(photo,format='PNG');photo.seek(0)
        response=self.public.post(link,data={'action':'upload','document_type':'member_1_id_copy',
            'camera_file':(photo,'camera-photo.png')},content_type='multipart/form-data')
        self.assertEqual(response.status_code,302)
        self.assertIsNotNone(ClientFicaDocument.query.filter_by(application_id=self.aid,document_type='member_1_id_copy').first())

    def test_outstanding_id_email_names_only_outstanding_uploads(self):
        from app.routes.signing import send_supporting_upload_email
        with self.app.test_request_context('/'),patch('app.routes.signing.send_email',return_value=True) as mail:
            self.assertTrue(send_supporting_upload_email(self.record))
        body=mail.call_args.args[2]
        outstanding=body.split('Outstanding document uploads:')[1].split('You can take')[0]
        self.assertIn('Existing Child',outstanding)
        self.assertNotIn('Principal Member South African ID Copy',outstanding)
        self.assertIn('take a clear photo',body)

    def questionnaire(self):
        from app.services.cdd_service import FIELDS
        data={'first_names':'Example','surname':'Principal','cell_number':'0821234567','email':'client@example.test',
            'residential_address':'Saved address','residential_postal_code':'1234','beneficiary_full_names':'Beneficiary Only',
            'beneficiary_relationship':'Spouse','beneficiary_date_of_birth':'1985-01-01','payment_method':'Cash','marketing_choice':'no',
            'productdep_1_full_name':'Existing Child','productdep_1_relationship':'Child','productdep_1_id_or_dob':'2010-01-01'}
        for key,label,options in FIELDS:data['cdd_'+key]=options.split('|')[0] if options else 'Example answer'
        return data

    def test_legacy_spouse_survives_member_plus_questionnaire_save(self):
        from app.services.online_application import save_questionnaire
        self.record.spouse_first_names='Saved';self.record.spouse_surname='Spouse';self.record.spouse_date_of_birth='01/01/1985'
        self.record.signed_at=None;db.session.commit()
        save_questionnaire(self.record,self.questionnaire());db.session.commit()
        members=json.loads(self.record.product_dependents_json)
        self.assertEqual([r['full_name'] for r in members],['Existing Child','Saved Spouse'])
        self.assertNotIn('Beneficiary Only',[r['full_name'] for r in members])

    def test_omitted_saved_member_cannot_be_silently_erased(self):
        from app.services.online_application import save_questionnaire
        original=self.record.product_dependents_json
        data=self.questionnaire()
        for key in list(data):
            if key.startswith('productdep_'):data.pop(key)
        with self.assertRaisesRegex(ValueError,'Saved covered members are missing'):
            save_questionnaire(self.record,data)
        db.session.rollback()
        self.assertEqual(self.record.product_dependents_json,original)

    def test_approval_blocks_gap_then_activation_emails_policy_pack_automatically(self):
        from app.routes.qa import QA_CHECKLIST
        self.record.product_dependents_json='[]';self.record.total_members=2
        db.session.add(BranchFilingOffice(branch_key='test office',branch_name='Test Office',email='office@example.test',is_default=True))
        db.session.commit()
        data={key:'on' for key,label in QA_CHECKLIST}
        data.update(decision='QA Approved',policy_number='TEST-ACTIVE',start_date=date.today().isoformat(),filing_branch='Test Office')
        delivery=SimpleNamespace(branch_name='Test Office',recipient_email='office@example.test',status='Accepted by mail server')
        with patch('app.routes.qa.document_summary',return_value={'complete':True}), \
             patch('app.routes.qa.ensure_screened',return_value=(True,[])), \
             patch('app.services.company_documents.active_company_documents',return_value=[SimpleNamespace(category='bank_confirmation')]), \
             patch('app.services.company_documents.materialise_company_documents',return_value=([],None)), \
             patch('app.services.branch_filing.send_branch_pack',return_value=delivery), \
             patch('app.services.email_service.send_email',return_value=True) as mail:
            response=self.client.post(f'/qa/application/{self.aid}',data=data)
            self.assertEqual(response.status_code,302);mail.assert_not_called()
            self.assertNotEqual(self.record.status,'Active')
            confirm_current_members(self.record);db.session.commit()
            response=self.client.post(f'/qa/application/{self.aid}',data=data)
            self.assertEqual(response.status_code,302)
            self.assertEqual(self.record.status,'Active')
            self.assertEqual(self.record.whatsapp_journey.notice_status,'Sent')
            self.assertIn('now active',mail.call_args.args[2])
            self.assertEqual(len(mail.call_args.args[3]),5)
            self.client.post(f'/qa/application/{self.aid}',data=data)
            self.assertEqual(mail.call_count,1)

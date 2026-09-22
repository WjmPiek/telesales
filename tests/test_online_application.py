import unittest
import io
import base64
from pathlib import Path
from datetime import date
from unittest.mock import patch
import test_application_flow as fixtures
from app import db
from app.models import (CommunicationCampaign, CommunicationEvent, ApplicationJourney, ClientApplication,
                        ClientFicaDocument, DocumentSignature, PolicyProductRule, SystemSetting)
from app.services.client_storage import application_folder, store_document
from app.services.cdd_service import FIELDS as CDD_FIELDS
from app.services.signature_fields import application_fields

class OnlineApplicationTests(unittest.TestCase):
    setUp=fixtures.ApplicationFlowTests.setUp
    tearDown=fixtures.ApplicationFlowTests.tearDown
    record=fixtures.ApplicationFlowTests.record
    upload_screening=fixtures.ApplicationFlowTests.upload_screening

    def prepare(self):
        import json
        if not SystemSetting.query.filter_by(category='Email',key='business_bank_confirmation_pdf').first():
            db.session.add(SystemSetting(category='Email',key='business_bank_confirmation_pdf',active=True,
                value=json.dumps({'filename':'letter.pdf','content':base64.b64encode(b'%PDF-1.4\n%%EOF').decode()})))
        campaign=CommunicationCampaign(name='Isolated test',message_body='Test',created_by_id=self.user_id)
        db.session.add(campaign);db.session.flush()
        db.session.add(ApplicationJourney(application_id=self.record_id,campaign_id=campaign.id))
        self.record.sign_token='fictional-online-test';db.session.commit()
        client=self.app.test_client()
        client.post('/sign/fictional-online-test',data={'action':'unlock','id_number':self.record.id_number})
        page=client.get('/online-application/fictional-online-test')
        self.assertEqual(page.status_code,200)
        with client.session_transaction() as s:nonce=s[f'questionnaire_nonce_{self.record_id}']
        return client,nonce

    def save(self,client,nonce,include_supporting_documents=True):
        from PIL import Image
        if include_supporting_documents:
            for kind in ['id_copy','proof_of_address']:
                path=Path(application_folder(self.record))/(kind+'.png')
                Image.new('RGB',(100,100),'white').save(path)
                store_document(self.record,str(path))
                db.session.add(ClientFicaDocument(application_id=self.record_id,document_type=kind,original_filename=path.name,file_path=str(path),status='Approved'))
            db.session.commit()
        data={'nonce':nonce,'action':'save','first_names':'Fictional','surname':'Test','cell_number':'0821234567',
              'email':'client@example.test','residential_address':'1 Example Road','residential_postal_code':'1234',
              'postal_address':'1 Example Road','postal_code':'1234','beneficiary_full_names':'Example Beneficiary',
              'beneficiary_relationship':'Spouse','beneficiary_date_of_birth':'1985-01-01','payment_method':'Cash','marketing_choice':'no'}
        for key,label,options in CDD_FIELDS:data['cdd_'+key]=options.split('|')[0] if options else 'Fictional answer'
        response=client.post('/online-application/fictional-online-test',data=data)
        self.assertEqual(response.status_code,302,response.data[:400])
        self.assertTrue(self.record.whatsapp_journey.ready)
        return data

    def sign_documents(self, client):
        from PIL import Image
        out=io.BytesIO();Image.new('RGB',(80,30),'black').save(out,format='PNG')
        signature='data:image/png;base64,'+base64.b64encode(out.getvalue()).decode()
        targets={'application':[field['key'] for field in application_fields(self.record)],
                 'popia':['popia'],'disclosure':['disclosure'],'welcome':['welcome'],'cdd':['cdd']}
        for kind,fields in targets.items():
            for field in fields:
                response=client.get(f'/sign/fictional-online-test/review/{kind}')
                self.assertEqual(response.status_code,200,response.data[:400])
                self.assertIn(b'Sign here',response.data)
                with client.session_transaction() as session:
                    review_nonce=session[f'document_review_{self.record_id}_{kind}']
                response=client.post('/sign/fictional-online-test',data={
                    'action':'sign_document','document_type':kind,'signature_field':field,
                    'typed_name':'Fictional Test','signature_data':signature,'review_nonce':review_nonce})
                self.assertEqual(response.status_code,302,response.data[:400])

    def test_postal_address_and_supporting_uploads_are_optional(self):
        client,nonce=self.prepare()
        page=client.get('/online-application/fictional-online-test')
        self.assertNotIn(b'Supporting documents (optional upload)',page.data)
        self.assertNotIn(b'name="id_copy"',page.data)
        self.assertNotIn(b'name="proof_of_address"',page.data)
        data=self.save(client,nonce,include_supporting_documents=False)
        data.update(postal_address='',postal_code='',cdd_funds='Salary',cdd_funds_details='')
        response=client.post('/online-application/fictional-online-test?edit=1',data=data)
        self.assertEqual(response.status_code,302,response.data[:400])
        self.assertEqual(self.record.postal_address,'')
        self.assertFalse(ClientFicaDocument.query.filter_by(application_id=self.record_id).count())

        data['cdd_funds']='Other'
        response=client.post('/online-application/fictional-online-test?edit=1',data=data)
        self.assertIn(b'Complete Explain the source of funds',response.data)

    def test_child_uses_child_age_rules_and_is_not_validated_twice(self):
        self.record.product.min_age=31
        self.record.product.max_age=55
        db.session.add(PolicyProductRule(product_id=self.record.product_id,plan_type='family',
          spouse_slots=1,child_slots=5,extended_slots=5,family_14_21=40000))
        db.session.commit()
        client,nonce=self.prepare()
        data=self.save(client,nonce)
        data.update(child_1_full_name='Example Child',child_1_relationship='Child',
                    child_1_id_or_dob='2009-08-15')
        response=client.post('/online-application/fictional-online-test?edit=1',data=data)
        self.assertEqual(response.status_code,302,response.data[:400])

        data['child_1_id_or_dob']='0908151234088'
        response=client.post('/online-application/fictional-online-test?edit=1',data=data)
        self.assertEqual(response.status_code,302,response.data[:400])

        data['child_1_id_or_dob']='2000-08-15'
        response=client.post('/online-application/fictional-online-test?edit=1',data=data)
        text=response.data.decode()
        self.assertIn('This product allows the child age range 0 to 21 years.',text)
        self.assertNotIn('Minimum age for this policy is 31',text)

    def test_questionnaire_draft_is_restored_after_network_failure_and_cleared_after_save(self):
        client,nonce=self.prepare()
        page=client.get('/online-application/fictional-online-test')
        self.assertIn(b'id="questionnaire-form"',page.data)
        self.assertIn(b'mf-application-draft-',page.data)
        self.assertIn(b'Your answers are saved automatically on this device',page.data)
        self.assertIn(b'Your saved answers were restored',page.data)
        self.save(client,nonce)
        review=client.get('/online-application/fictional-online-test')
        self.assertIn(b"localStorage.removeItem('mf-application-draft-",review.data)

    def test_street_code_and_member_benefits_are_captured_per_person(self):
        self.record.product.waiting_period_months=6
        db.session.add(PolicyProductRule(product_id=self.record.product_id,spouse_cover=50000,
          extended_cover=30000,family_0_11=10000,family_1_5=10000,family_6_13=25000,family_14_21=50000))
        db.session.commit()
        client,nonce=self.prepare()
        page=client.get('/online-application/fictional-online-test')
        self.assertIn(b'Street code',page.data)
        self.assertIn(b'Waiting period',page.data)
        self.assertIn(b'id="child_1_cover"',page.data)
        self.assertIn(b'id="child_1_date_of_birth"',page.data)
        self.assertIn(b'id="child_1_age"',page.data)
        self.assertIn(b'Cover for selected age',page.data)
        self.assertIn(b'Add next child',page.data)
        self.assertIn(b'data-index="2" hidden',page.data)
        data=self.save(client,nonce)
        data.update(spouse_1_full_name='Example Spouse',spouse_1_relationship='Spouse',spouse_1_id_or_dob='1985-01-01',
          child_1_full_name='Example Child',child_1_relationship='Child',child_1_id_or_dob='2018-01-01',
          extended_1_full_name='Example Parent',extended_1_relationship='Parent',extended_1_id_or_dob='1960-01-01')
        response=client.post('/online-application/fictional-online-test?edit=1',data=data)
        self.assertEqual(response.status_code,302,response.data[:400])
        child=__import__('json').loads(self.record.dependents_json)[0]
        extended=__import__('json').loads(self.record.extended_family_json)[0]
        all_members=__import__('json').loads(self.record.product_dependents_json)
        self.assertEqual(child['cover'],'25000.00')
        self.assertEqual(child['waiting_period'],'6 months')
        self.assertEqual(extended['cover'],'30000.00')
        self.assertEqual({row['kind'] for row in all_members},{'spouse','child','extended'})
        self.assertEqual(next(row for row in all_members if row['kind']=='spouse')['cover'],'50000.00')
        self.assertEqual(self.record.spouse_date_of_birth,'01/01/1985')
        review=client.get('/online-application/fictional-online-test')
        self.assertIn(b'People covered by this application',review.data)
        self.assertIn(b'Example Child',review.data)
        self.assertIn(b'R25,000.00',review.data)

        from pypdf import PdfReader
        from app.services.pdf_service import generate_application_pdf
        self.record.form_template='gold_family_fillable';self.record.postal_code='';db.session.commit()
        output=Path(application_folder(self.record))/f'mapping_check_{self.record_id}.pdf'
        generate_application_pdf(self.record,str(output))
        fields=PdfReader(str(output)).get_fields()
        value=lambda key: str(fields[key].get('/V') or '')
        self.assertEqual(value('contact'),'0821234567')
        self.assertEqual(value('email'),'client@example.test')
        self.assertEqual(value('res_address'),'1 Example Road')
        self.assertEqual(value('postal_code'),'1234')
        self.assertEqual(value('spouse_first'),'Example')
        self.assertEqual(value('spouse_surname'),'Spouse')
        self.assertEqual(value('spouse_dob'),'01/01/1985')
        self.assertEqual(value('child_1_name'),'Example Child')
        self.assertEqual(value('ext_1_name'),'Example Parent / Parent')
        self.assertEqual(value('beneficiary_name'),'Example Beneficiary')

    def test_debit_order_with_different_account_holder_is_sent_for_client_authority(self):
        client,nonce=self.prepare()
        data=self.save(client,nonce)
        data.update(payment_method='Debit Order',bank_name='FNB',branch_code='250655',
          account_number='1234567890',account_type='Cheque',account_holder='Different Account Holder',
          debit_day='25',first_deduction_date='2026-10-25')
        response=client.post('/online-application/fictional-online-test?edit=1',data=data)
        self.assertEqual(response.status_code,302,response.data[:400])
        review=client.get('/online-application/fictional-online-test')
        self.assertIn(b'Debit order included',review.data)
        self.assertIn(b'Open document and sign',review.data)
        keys={field['key'] for field in application_fields(self.record)}
        self.assertIn('application:account',keys)
        self.assertIn('application:terms_account',keys)

    def test_standard_branch_code_is_automatic_and_not_manually_required(self):
        client,nonce=self.prepare()
        page=client.get('/online-application/fictional-online-test')
        self.assertIn(b'id="branch_code"',page.data)
        self.assertIn(b'readonly',page.data)
        self.assertIn(b'The standard branch code is added automatically',page.data)
        data=self.save(client,nonce)
        data.update(payment_method='Debit Order',bank_name='Standard Bank',branch_code='',
          account_number='1234567890',account_type='Cheque',account_holder='Example Account Holder',
          debit_day='25',first_deduction_date='2026-10-25')
        response=client.post('/online-application/fictional-online-test?edit=1',data=data)
        self.assertEqual(response.status_code,302,response.data[:400])
        self.assertEqual(self.record.branch_code,'051001')

    def test_staff_new_policy_form_has_street_code_and_member_benefit_fields(self):
        page=self.client.get('/applications/new')
        self.assertIn(b'Residential Street Code',page.data)
        self.assertIn(b'name="spouse_1_waiting_period"',page.data)
        self.assertIn(b'name="child_1_cover"',page.data)
        self.assertIn(b'name="extended_1_waiting_period"',page.data)

    def test_product_editor_saves_family_member_setup_and_covers(self):
        product=self.record.product
        product_id=product.id
        response=self.client.post(f'/policies/{product_id}/edit',data={
          'product_name':product.product_name,'plan_name':product.plan_name,'cover_amount':'50000',
          'monthly_premium':'300','waiting_period_months':'6','min_age':'31','max_age':'55','active':'on',
          'plan_type':'family','spouse_slots':'1','child_slots':'8','extended_slots':'5','extra_member_slots':'0',
          'spouse_min_age':'31','spouse_max_age':'55','child_min_age':'0','child_max_age':'21',
          'extended_min_age':'22','extended_max_age':'75','extra_member_min_age':'0','extra_member_max_age':'70',
          'main_member_cover':'50000','spouse_cover':'50000','family_0_11':'10000','family_1_5':'10000',
          'family_6_13':'25000','family_14_21':'50000','stillborn_cover':'10000','extended_cover':'30000'})
        self.assertEqual(response.status_code,302,response.data[:400])
        rules=PolicyProductRule.query.filter_by(product_id=product_id).one()
        self.assertEqual(rules.plan_type,'family')
        self.assertEqual((rules.spouse_slots,rules.child_slots,rules.extended_slots),(1,8,5))
        self.assertEqual(float(rules.family_6_13),25000)
        self.assertEqual((rules.spouse_min_age,rules.spouse_max_age),(31,55))
        self.assertEqual((rules.extended_min_age,rules.extended_max_age),(22,75))
        page=self.client.get(f'/policies/{product_id}/edit')
        self.assertIn(b'Application member setup',page.data)
        self.assertIn(b'Locked member age rules',page.data)
        self.assertIn(b'Child 14',page.data)

    def test_member_product_uses_configured_extra_member_fields_online(self):
        product=self.record.product
        db.session.add(PolicyProductRule(product_id=product.id,plan_type='member_product',extra_member_slots=3,
          member_0_5_product_only=10000,member_6_70_product_only=20000))
        db.session.commit()
        client,nonce=self.prepare()
        page=client.get('/online-application/fictional-online-test')
        self.assertIn(b'Extra members (add one at a time, up to 3)',page.data)
        self.assertIn(b'id="productdep_3_cover"',page.data)
        self.assertNotIn(b'id="productdep_4_cover"',page.data)
        self.assertNotIn(b'id="child_1_cover"',page.data)
        data=self.save(client,nonce)
        data.update(productdep_1_full_name='Extra Member',productdep_1_relationship='Parent',
                    productdep_1_id_or_dob='1960-01-01')
        response=client.post('/online-application/fictional-online-test?edit=1',data=data)
        self.assertEqual(response.status_code,302,response.data[:400])
        members=__import__('json').loads(self.record.product_dependents_json)
        self.assertEqual(len(members),1)
        self.assertEqual(members[0]['kind'],'productdep')
        self.assertEqual(members[0]['cover'],'20000.00')

    def test_client_can_sign_when_supporting_documents_will_be_emailed(self):
        client,nonce=self.prepare();self.save(client,nonce,include_supporting_documents=False)
        self.sign_documents(client)
        data={'nonce':nonce,'action':'final_submit','document_email':'client@example.test','document_email_confirm':'client@example.test'}
        with patch('app.routes.signing.send_email',return_value=True):
            response=client.post('/online-application/fictional-online-test',data=data)
        self.assertEqual(response.status_code,200)
        self.assertIn(b'Documents Submitted',response.data)
        self.assertEqual(self.record.status,'Signed')

    def test_document_opens_before_pad_and_returns_to_document_after_each_signature(self):
        from PIL import Image
        client,nonce=self.prepare();self.save(client,nonce,include_supporting_documents=False)
        fields=[field['key'] for field in application_fields(self.record)]
        review=client.get('/sign/fictional-online-test/review/application')
        self.assertEqual(review.status_code,200)
        self.assertIn(b'deliberately taps its highlighted space',review.data)
        self.assertNotIn(b'openPad(remaining[0].target)',review.data)

        image=io.BytesIO();Image.new('RGB',(80,30),'black').save(image,format='PNG')
        signature='data:image/png;base64,'+base64.b64encode(image.getvalue()).decode()
        for index,field in enumerate(fields):
            if index:
                review=client.get('/sign/fictional-online-test/review/application')
            with client.session_transaction() as session:
                review_nonce=session[f'document_review_{self.record_id}_application']
            response=client.post('/sign/fictional-online-test',data={
                'action':'sign_document','document_type':'application','signature_field':field,
                'typed_name':'Fictional Test','signature_data':signature,'review_nonce':review_nonce})
            self.assertEqual(response.status_code,302)
            if index < len(fields)-1:
                self.assertIn('/review/application?saved=',response.location)
                saved=client.get(response.location)
                self.assertIn(b'Signature saved on the document',saved.data)
            else:
                self.assertTrue(response.location.endswith('/online-application/fictional-online-test'))

        self.assertEqual(
            {row.document_type for row in DocumentSignature.query.filter_by(application_id=self.record_id).all()},
            set(fields),
        )

    def test_saved_client_contact_details_are_present_in_application_pdf(self):
        from pypdf import PdfReader
        from app.routes.signing import _signable_pdf
        client,nonce=self.prepare();self.save(client,nonce,include_supporting_documents=False)
        path=_signable_pdf(self.record,'application')
        text=' '.join(page.extract_text() or '' for page in PdfReader(path).pages)
        self.assertIn('0821234567',text)
        self.assertIn('client@example.test',text)
        self.assertIn('1 Example Road',text)

    def test_full_whatsapp_questionnaire_signature_and_activation(self):
        client,nonce=self.prepare();self.save(client,nonce)
        data={'nonce':nonce,'action':'final_submit','document_email':'client@example.test','document_email_confirm':'client@example.test'}
        response=client.post('/online-application/fictional-online-test',data=data)
        self.assertIn(b'Open and sign these documents first',response.data)
        self.sign_documents(client)
        with patch('app.routes.signing.send_email',return_value=True) as mail:
            response=client.post('/online-application/fictional-online-test',data=data)
            self.assertEqual(response.status_code,200)
            self.assertIn(b'Documents Submitted',response.data)
            self.assertEqual(mail.call_args_list[0].args[3],[])
            self.assertIn('awaiting verification',mail.call_args_list[0].args[2])
            self.assertIn('not active yet',mail.call_args_list[0].args[2])
            self.assertIn('/sign/fictional-online-test/supporting-documents',mail.call_args_list[0].args[2])
        signatures=DocumentSignature.query.filter_by(application_id=self.record_id).all()
        self.assertGreaterEqual(len(signatures),7)
        self.assertEqual(len({row.signature_image_path for row in signatures}),len(signatures))
        self.assertIsNone(self.record.whatsapp_journey.activated_at)
        self.assertEqual(client.get('/online-application/fictional-online-test').status_code,410)
        self.assertEqual(self.record.beneficiary_full_names,'Example Beneficiary')
        from app.routes.qa import QA_CHECKLIST
        review={key:'on' for key,label in QA_CHECKLIST}
        review.update(decision='QA Approved',policy_number='TEST-POLICY',start_date=date.today().isoformat())
        with patch('app.services.email_service.send_email',return_value=True) as mail:
            self.client.post(f'/qa/application/{self.record_id}',data=review)
            mail.assert_not_called()
            self.assertIsNone(self.record.whatsapp_journey.activated_at)
            self.upload_screening()
            self.client.post(f'/qa/application/{self.record_id}',data=review)
            self.assertEqual(self.record.status,'Active')
            self.assertEqual(self.record.whatsapp_journey.notice_status,'Sent')
            self.assertIn('TEST-POLICY',mail.call_args.args[2])
            self.assertIn('now active',mail.call_args.args[2])
            self.assertEqual(len(mail.call_args.args[3]),6)
            self.assertEqual(Path(mail.call_args.args[3][-1]).name,'martins_business_bank_confirmation_letter.pdf')
            self.client.post(f'/qa/application/{self.record_id}',data=review)
            self.assertEqual(mail.call_count,1)

    def test_questionnaire_security_and_email_validation(self):
        client,nonce=self.prepare()
        stranger=self.app.test_client()
        self.assertEqual(stranger.get('/online-application/fictional-online-test').status_code,403)
        self.assertEqual(client.post('/online-application/fictional-online-test',data={'action':'save','nonce':'bad'}).status_code,400)
        data=self.save(client,nonce);data['email']='invalid'
        response=client.post('/online-application/fictional-online-test?edit=1',data=data)
        self.assertIn(b'Enter an email address',response.data)
        self.assertEqual(self.record.email,'client@example.test')

    def test_whatsapp_journey_never_uses_email_for_invitation(self):
        self.prepare()
        with patch('app.routes.applications.ensure_screened',return_value=(True,[])),patch('app.routes.applications.send_email') as email,patch('app.services.whatsapp_service.send_application_link') as wa:
            wa.return_value.ok=True
            self.client.post(f'/applications/{self.record_id}/send-sign-link')
            wa.assert_called_once();email.assert_not_called()

    def test_product_campaign_generates_qr_and_starts_direct_application(self):
        campaign=CommunicationCampaign(name='Gold flyer',message_body='Apply now',created_by_id=self.user_id,
          product_id=self.record.product_id,public_application_token='public-product-campaign')
        db.session.add(campaign);db.session.commit()
        public=self.app.test_client()
        page=public.get('/join/campaign/public-product-campaign')
        self.assertEqual(page.status_code,200)
        self.assertIn(b'The policy is already selected from the flyer',page.data)
        self.assertIn(b'Test Family',page.data)
        self.assertEqual(campaign.qr_scan_count,1)
        self.assertEqual(CommunicationEvent.query.filter_by(campaign_id=campaign.id,event_type='qr_scan').count(),1)
        public.get('/join/campaign/public-product-campaign')
        self.assertEqual(campaign.qr_scan_count,1)
        response=public.post('/join/campaign/public-product-campaign',data={
          'first_names':'QR','surname':'Applicant','id_number':'8001015009087','cell_number':'0821234567',
          'email':'qr@example.test','total_members':'3'})
        self.assertEqual(response.status_code,302,response.data[:400])
        self.assertIn('/online-application/',response.location)
        application=ClientApplication.query.filter_by(application_type='New Policy - QR Campaign').one()
        self.assertEqual(application.product_id,self.record.product_id)
        self.assertEqual(application.total_members,3)
        self.assertEqual(application.whatsapp_journey.campaign_id,campaign.id)
        self.assertEqual(application.cell_number,'+27821234567')
        self.assertEqual(CommunicationEvent.query.filter_by(campaign_id=campaign.id,event_type='application_started').count(),1)
        follow=public.get(response.location)
        self.assertEqual(follow.status_code,200)
        self.assertIn(b'Your selected requirements',follow.data)
        qr=self.client.get(f'/communications/{campaign.id}/application-qr.png?download=1')
        self.assertEqual(qr.status_code,200)
        self.assertEqual(qr.mimetype,'image/png')
        self.assertGreater(len(qr.data),100)

    def test_recipient_campaign_with_product_skips_product_selection(self):
        from app.models import CampaignRecipient, LapsedPolicy
        lead=LapsedPolicy(member_id='QR-RECIPIENT',initials='Test',surname='Recipient',
          id_number='8001015009087',cell_number='0821234567',branch='A',assigned_agent_id=self.user_id)
        campaign=CommunicationCampaign(name='Specific policy',message_body='Apply',created_by_id=self.user_id,
          product_id=self.record.product_id,public_application_token='specific-policy')
        db.session.add_all([lead,campaign]);db.session.flush()
        recipient=CampaignRecipient(campaign_id=campaign.id,lapsed_policy_id=lead.id,secure_token='specific-recipient')
        db.session.add(recipient);db.session.commit()
        public=self.app.test_client()
        page=public.get('/join/specific-recipient')
        self.assertIn(b'Apply for Test Family',page.data)
        self.assertNotIn(b'Choose cover',page.data)
        response=public.post('/join/specific-recipient',data={
          'id_number':'8001015009087','total_members':'2','cover_amount':'10000'})
        self.assertEqual(response.status_code,302)
        self.assertIn(f'/join/specific-recipient/application/{self.record.product_id}',response.location)

    def test_named_button_has_no_raw_url_in_body(self):
        import os
        from app.services.whatsapp_service import send_application_link
        with self.app.test_request_context('/'),patch.dict(os.environ,{'WHATSAPP_ENABLED':'true','WHATSAPP_PROVIDER':'360dialog','D360_API_KEY':__import__('uuid').uuid4().hex}),patch('app.services.whatsapp_service.requests.post') as post:
            post.return_value.status_code=200;post.return_value.content=b'{}';post.return_value.json.return_value={'messages':[{'id':'test'}]}
            result=send_application_link(self.record,'https://example.test/sign/opaque','Dear Fictional Test, https://example.test/sign/opaque')
            self.assertTrue(result.ok)
            payload=post.call_args.kwargs['json']
            self.assertEqual(payload['type'],'interactive')
            self.assertEqual(payload['interactive']['action']['parameters']['display_text'],'Fictional Test')
            self.assertNotIn('https://',payload['interactive']['body']['text'])

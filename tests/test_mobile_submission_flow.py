import io
import json
import unittest
from pathlib import Path
from unittest.mock import patch
from PIL import Image,ImageDraw
from pypdf import PdfReader
import test_analytics_security as fixtures
import test_online_application as online_fixtures
from app import db
from app.models import ClientApplication,PolicyProduct,PolicyProductRule,ApplicationCDD,ClientFicaDocument,DocumentSignature
from app.services.mobile_members import member_form,adapt_answers
from app.services.signature_fields import application_fields


class MobileSubmissionFlowTests(unittest.TestCase):
    tearDown=fixtures.AnalyticsSecurityTests.tearDown
    prepare=online_fixtures.OnlineApplicationTests.prepare
    save=online_fixtures.OnlineApplicationTests.save
    sign_documents=online_fixtures.OnlineApplicationTests.sign_documents

    def setUp(self):
        fixtures.AnalyticsSecurityTests.setUp(self)
        self.user_id=self.user_ids['admin']
        product=PolicyProduct(product_name='Gold Family Plan',plan_name='Family',monthly_premium=100,cover_amount=10000,waiting_period_months=6)
        db.session.add(product);db.session.flush()
        db.session.add(PolicyProductRule(product_id=product.id,plan_type='family'))
        a=ClientApplication(application_ref='MOBILE-PREVIEW',product=product,form_template='gold_family_fillable',
            first_names='Fictional',surname='Example',id_number='8001015009087',cell_number='0821234567',email='client@example.test',
            payment_method='Cash',total_members=2,status='Draft',branch='A',agent_id=self.user_id,cover_amount=10000,monthly_premium=100)
        db.session.add(a);db.session.commit();self.record_id=a.id

    @property
    def record(self):return db.session.get(ClientApplication,self.record_id)

    def test_two_people_means_one_other_three_means_two_and_relationship_is_mapped(self):
        client,nonce=self.prepare()
        page=client.get('/online-application/fictional-online-test').data
        self.assertIn(b'name="covered_1_relationship"',page)
        self.assertNotIn(b'name="covered_2_relationship"',page)
        self.record.total_members=3;db.session.commit()
        page=client.get('/online-application/fictional-online-test').data
        self.assertIn(b'name="covered_2_relationship"',page)
        self.assertNotIn(b'name="covered_3_relationship"',page)
        data=self.save(client,nonce)
        data.update(covered_count='2',covered_1_full_name='Example Spouse',covered_1_relationship='Spouse',covered_1_id_or_dob='1985-01-01',
                    covered_2_full_name='Example Child',covered_2_relationship='Child',covered_2_id_or_dob='2010-01-01',postal_address='')
        response=client.post('/online-application/fictional-online-test',data=data)
        self.assertEqual(response.status_code,302)
        self.assertEqual(self.record.spouse_surname,'Spouse')
        self.assertEqual(len(json.loads(self.record.dependents_json)),1)
        self.assertEqual(len(json.loads(self.record.product_dependents_json)),2)
        self.assertEqual(json.loads(ApplicationCDD.query.filter_by(application_id=self.record_id).one().answers_json)['postal_address'],'1 Example Road')
        with self.assertRaises(ValueError):adapt_answers(self.record,{'covered_count':'3'})

    def signed(self):
        client,nonce=self.prepare();data=self.save(client,nonce,include_supporting_documents=False)
        data.update(covered_count='1',covered_1_full_name='Example Spouse',covered_1_relationship='Spouse',covered_1_id_or_dob='1985-01-01')
        self.assertEqual(client.post('/online-application/fictional-online-test',data=data).status_code,302)
        self.sign_documents(client)
        return client,nonce

    def enter_id_step(self,client,nonce):
        with patch('app.routes.signing.send_email') as mail:
            response=client.post('/online-application/fictional-online-test',data={'nonce':nonce,'action':'final_submit'})
            mail.assert_not_called()
        self.assertEqual(response.status_code,302)
        self.assertTrue(response.location.endswith('/identity-documents'))
        self.assertIsNone(self.record.signed_at)
        self.assertIsNone(self.record.whatsapp_journey.signed_bundle_at)
        page=client.get(response.location)
        self.assertIn(b'Take photos or upload documents now',page.data)
        self.assertIn(b'I will send the documents by email',page.data)
        self.assertIn(b'Spouse ID Copy',page.data)
        with client.session_transaction() as session:return session[f'identity_documents_nonce_{self.record_id}']

    def test_email_choice_keeps_existing_followup_email_and_finishes_once(self):
        client,nonce=self.signed();step_nonce=self.enter_id_step(client,nonce)
        with patch('app.routes.signing.send_email',return_value=True) as mail:
            response=client.post('/sign/fictional-online-test/identity-documents',data={'nonce':step_nonce,'document_choice':'email'})
            self.assertEqual(response.status_code,200,response.data[:300])
            self.assertTrue(any('/supporting-documents' in str(call) for call in mail.call_args_list))
            self.assertIsNotNone(self.record.whatsapp_journey.signed_bundle_at)
            self.assertTrue(self.record.sign_token_revoked)
            sent=mail.call_count
            self.assertEqual(client.post('/sign/fictional-online-test/identity-documents',data={'nonce':step_nonce,'document_choice':'email'}).status_code,410)
            self.assertEqual(mail.call_count,sent)

    def test_photo_upload_saves_original_and_empty_choice_cannot_finish(self):
        client,nonce=self.signed();step_nonce=self.enter_id_step(client,nonce)
        response=client.post('/sign/fictional-online-test/identity-documents',data={'nonce':step_nonce,'document_choice':'upload'})
        self.assertIn(b'Take or choose a document photo',response.data)
        self.assertIsNone(self.record.signed_at)
        photo=io.BytesIO();Image.new('RGB',(250,180),'blue').save(photo,format='PNG');photo.seek(0)
        with patch('app.routes.signing.send_email',return_value=True),patch('app.routes.signing.validate_fica_upload',return_value=('Needs Review','Test photo')):
            response=client.post('/sign/fictional-online-test/identity-documents',data={'nonce':step_nonce,'document_choice':'upload','camera_id_copy':(photo,'id-photo.png')})
        self.assertEqual(response.status_code,200,response.data[:300])
        self.assertEqual(ClientFicaDocument.query.filter_by(application_id=self.record_id,document_type='id_copy').count(),1)
        self.assertIsNotNone(self.record.signed_at)

    def test_page_three_has_its_own_required_signature_and_pdf_ink(self):
        from app.services.pdf_service import generate_application_pdf
        fields=application_fields(self.record)
        target=next(f for f in fields if f['key']=='application:policy_terms')
        self.assertEqual(target['page'],3)
        image=Image.new('RGB',(450,100),'white');ImageDraw.Draw(image).line([(10,80),(100,10),(120,70),(220,20),(310,70),(440,30)],fill='navy',width=7)
        signature=Path(self.tmp.name)/'signature.png';image.save(signature)
        db.session.add(DocumentSignature(application_id=self.record_id,document_type=target['key'],typed_name='Fictional Example',signature_image_path=str(signature)));db.session.commit()
        from app.services.client_storage import application_folder
        out=Path(application_folder(self.record))/'application.pdf';generate_application_pdf(self.record,str(out))
        pdf=PdfReader(out)
        self.assertTrue(pdf.pages[2]['/Resources'].get('/XObject'))
        self.assertIn('Main member signature',pdf.pages[2].extract_text())

    def test_annexure_save_uses_residential_address_only_when_postal_is_blank(self):
        from app.services.cdd_service import FIELDS,save_answers,answers_for
        data={key:options.split('|')[0] if options else 'Example' for key,label,options in FIELDS}
        data.update(birth_date='1980-01-01',postal_address='   ',residential_address='1 Example Road')
        save_answers(self.record,data)
        self.assertEqual(answers_for(self.record)['postal_address'],'1 Example Road')
        data['postal_address']='PO Box 123';save_answers(self.record,data)
        self.assertEqual(answers_for(self.record)['postal_address'],'PO Box 123')

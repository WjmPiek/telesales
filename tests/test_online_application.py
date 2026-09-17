import unittest
import io
import base64
from pathlib import Path
from datetime import date
from unittest.mock import patch
import test_application_flow as fixtures
from app import db
from app.models import CommunicationCampaign, ApplicationJourney, ClientFicaDocument, DocumentSignature
from app.services.client_storage import application_folder, store_document
from app.services.cdd_service import FIELDS as CDD_FIELDS

class OnlineApplicationTests(unittest.TestCase):
    setUp=fixtures.ApplicationFlowTests.setUp
    tearDown=fixtures.ApplicationFlowTests.tearDown
    record=fixtures.ApplicationFlowTests.record
    upload_screening=fixtures.ApplicationFlowTests.upload_screening

    def prepare(self):
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

    def save(self,client,nonce):
        from PIL import Image
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

    def test_full_whatsapp_questionnaire_signature_and_activation(self):
        client,nonce=self.prepare();self.save(client,nonce)
        from PIL import Image
        out=io.BytesIO();Image.new('RGB',(80,30),'black').save(out,format='PNG')
        data={'nonce':nonce,'action':'sign','signature_data':'data:image/png;base64,'+base64.b64encode(out.getvalue()).decode()}
        response=client.post('/online-application/fictional-online-test',data=data)
        self.assertIn(b'Confirm that you agree',response.data)
        data['consent_bundle']='yes'
        response=client.post('/online-application/fictional-online-test',data=data)
        self.assertIn(b'Open and review every document',response.data)
        for kind in ['application','popia','disclosure','welcome','cdd']:
            with client.get('/online-application/fictional-online-test/document/'+kind) as response:
                self.assertEqual(response.status_code,200)
        with patch('app.routes.signing.send_email',return_value=True) as mail:
            response=client.post('/online-application/fictional-online-test',data=data)
            self.assertEqual(response.status_code,200)
            self.assertIn(b'Documents Submitted',response.data)
            self.assertEqual(len(mail.call_args_list[0].args[3]),5)
        signatures=DocumentSignature.query.filter_by(application_id=self.record_id).all()
        self.assertGreaterEqual(len(signatures),7)
        self.assertEqual(len({row.signature_image_path for row in signatures}),1)
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

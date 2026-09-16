import base64
import io
import os
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

os.environ['DATABASE_URL'] = 'sqlite:///:memory:'
os.environ['ENABLE_WHATSAPP_SCHEDULER'] = '0'
os.environ['AUTO_CREATE_TABLES'] = '1'
from app import create_app, db
from app.models import (Role, User, PolicyProduct, ClientApplication, ClientStoredFile, TelesalesScriptSession,
                        LapsedPolicy, WhatsAppContact, WhatsAppConversation, WhatsAppMessage)
from app.services.client_storage import application_folder
from app.services.email_service import send_email


class ApplicationFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = create_app()
        self.app.config.update(TESTING=True, UPLOAD_FOLDER=self.tmp.name, BASE_URL='https://example.test')
        from flask import g
        @self.app.before_request
        def clear_test_user_cache():
            g.pop('_login_user', None)
        self.ctx = self.app.app_context(); self.ctx.push()
        db.drop_all(); db.create_all()
        role = Role(name='Super Admin')
        self.user = User(name='Test admin', email='admin@example.test', role=role, password_hash='unused', branch='A')
        product = PolicyProduct(product_name='Test Family', plan_name='Test', monthly_premium=100, cover_amount=10000)
        db.session.add_all([self.user, product]); db.session.flush()
        record = ClientApplication(application_ref='TEST-ONLY', first_names='Fictional', surname='Test',
            id_number='8001015009087', email='test@example.test', cell_number='0821234567',
            branch='A', agent_id=self.user.id, product=product, payment_method='Cash', monthly_premium=100, cover_amount=10000)
        db.session.add(record); db.session.commit()
        self.record_id = record.id
        self.user_id = self.user.id
        self.client = self.app.test_client()
        with self.client.session_transaction() as session:
            session['_user_id'] = str(self.user_id); session['_fresh'] = True

    @property
    def record(self):
        return db.session.get(ClientApplication, self.record_id)

    def tearDown(self):
        db.session.remove(); db.drop_all(); self.ctx.pop(); self.tmp.cleanup()

    def test_named_email_link_escapes_client_values(self):
        from app.services.email_service import signing_email_html
        self.record.first_names = '<Alex & Sam>'
        link = 'https://example.test/sign/opaque-token'
        html = signing_email_html(self.record, link, 'Dear client,\n\n' + link)
        self.assertIn('&lt;Alex &amp; Sam&gt; Test - Online Application</a>', html)
        self.assertNotIn('<Alex', html)
        self.assertEqual(html.count(link), 1)

    def test_both_application_templates_keep_full_values_and_signature_target(self):
        import json
        from pypdf import PdfReader
        from app.services.pdf_service import generate_application_pdf
        from app.services.application_layout import SIGNATURE_RECTS
        self.record.first_names = 'Alexandra Elizabeth Catherine'
        self.record.surname = 'Van der Westhuizen'
        for template in ['single_family', 'member_product']:
            self.record.form_template = template
            dest = str(Path(application_folder(self.record)) / (template + '.pdf'))
            generate_application_pdf(self.record, dest)
            pdf = PdfReader(dest)
            self.assertIn(self.record.first_names, pdf.pages[0].extract_text())
            self.assertIn(self.record.surname, pdf.pages[0].extract_text())
            target = json.loads(pdf.metadata['/Subject'].removeprefix('martins-signature:'))
            self.assertEqual(target['rect'], SIGNATURE_RECTS[template])
            self.assertEqual(len(pdf.pages), 3)

    def test_email_failure_is_not_reported_as_sent(self):
        with patch('app.routes.applications.send_email', return_value=False):
            response = self.client.post(f'/applications/{self.record.id}/send-sign-link', follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.record.status, 'Signing Link Prepared')
        self.assertIn(b'Email was not sent', response.data)

    def test_script_email_sign_upload_restore_and_search(self):
        from app.routes.recovery import _send_script_selected_signing_link
        db.session.add(TelesalesScriptSession(application_id=self.record_id, agent_id=self.user_id, branch='A', client_name='Fictional Test', status='Completed', answers_json='{}'))
        db.session.commit()
        with self.app.test_request_context('/'), patch('app.routes.recovery.send_email', return_value=True) as mail:
            link, sent, errors = _send_script_selected_signing_link(self.record, 'email')
            self.assertTrue(sent); self.assertEqual(errors, [])
            self.assertIn('/sign/', mail.call_args.args[2])
            self.assertIn('Fictional Test - Online Application</a>', mail.call_args.kwargs['html_body'])
            self.assertIn('href="' + link + '"', mail.call_args.kwargs['html_body'])
        token = self.record.sign_token
        public = self.app.test_client()
        self.assertEqual(public.get(f'/sign/{token}/document/application').status_code, 403)
        public.post(f'/sign/{token}', data={'action':'unlock', 'id_number':'wrong'})
        self.assertEqual(public.get(f'/sign/{token}/document/application').status_code, 403)
        public.post(f'/sign/{token}', data={'action':'unlock','id_number':self.record.id_number})
        with public.get(f'/sign/{token}/document/application') as response:
            self.assertEqual(response.status_code, 200)
        # Clearly fictional PDF uploads; they remain Needs Review, never approved.
        from reportlab.pdfgen import canvas
        for kind in ['id_copy', 'proof_of_address']:
            stream = io.BytesIO(); c = canvas.Canvas(stream)
            c.drawString(50, 750, 'FICTIONAL TEST DOCUMENT - NOT VALID'); c.save(); stream.seek(0)
            response = public.post(f'/sign/{token}', data={'action':'upload_fica','document_type':kind,'file':(stream,kind+'.pdf')}, content_type='multipart/form-data')
            self.assertEqual(response.status_code, 302)
        # Synthetic signature only for the isolated test record.
        from PIL import Image
        image = io.BytesIO(); Image.new('RGB',(80,30),'black').save(image,format='PNG')
        signature = 'data:image/png;base64,' + base64.b64encode(image.getvalue()).decode()
        for kind in ['application','popia','disclosure','welcome']:
            self.assertEqual(public.get(f'/sign/{token}/review/{kind}').status_code, 200)
            with public.session_transaction() as session:
                nonce = session[f'document_review_{self.record_id}_{kind}']
            response = public.post(f'/sign/{token}',data={'action':'sign_document','document_type':kind,'typed_name':'Fictional Test','signature_data':signature,'review_nonce':nonce})
            self.assertEqual(response.status_code,302)
            from pypdf import PdfReader
            from app.models import DocumentSignature
            row = DocumentSignature.query.filter_by(application_id=self.record_id, document_type=kind).one()
            with public.get(f'/sign/{token}/document/{kind}') as saved:
                pdf = PdfReader(io.BytesIO(saved.data))
                import json
                target = json.loads(pdf.metadata['/Subject'].removeprefix('martins-signature:'))
                page = pdf.pages[target['page']-1]
                self.assertTrue(any(img.image.size == (80,30) for img in page.images))
                self.assertIn(b'Document signed', public.get(f'/sign/{token}/review/{kind}').data)
        with patch('app.routes.signing.send_email', return_value=False):
            response = public.post(f'/sign/{token}',data={'action':'final_submit'})
        self.assertEqual(response.status_code,200)
        self.assertEqual(self.record.status,'Signed')
        self.assertTrue(self.record.sign_token_revoked)
        self.assertEqual(public.get(f'/sign/{token}/document/signed_application').status_code,404)
        stored = ClientStoredFile.query.filter_by(application_id=self.record.id).all()
        self.assertGreaterEqual(len(stored),11)
        folder = Path(application_folder(self.record))
        originals = {str(p.relative_to(folder)):p.read_bytes() for p in folder.rglob('*') if p.is_file()}
        for p in folder.rglob('*'):
            if p.is_file(): p.unlink()
        application_folder(self.record)
        for name, content in originals.items(): self.assertEqual((folder/name).read_bytes(),content)
        response = self.client.post('/client-files/',data={'id_number':self.record.id_number})
        self.assertEqual(response.status_code,200)
        for text in [b'TEST-ONLY',b'Details',b'Chat history',b'Documents',b'Signed application']:
            self.assertIn(text,response.data)
        with self.client.get(f'/applications/{self.record.id}/download/signed_application') as response:
            self.assertEqual(response.status_code,200)

    def test_client_file_scope_and_chat_link(self):
        contact = WhatsAppContact(wa_id='27821234567',phone_number='27821234567',branch='B')
        convo = WhatsAppConversation(contact=contact)
        db.session.add(WhatsAppMessage(conversation=convo,direction='inbound',body='Private branch B message'))
        role = Role(name='Manager'); manager = User(name='Manager',email='manager@example.test',password_hash='unused',role=role,branch='A')
        db.session.add(manager); db.session.commit()
        manager_id = manager.id
        # Admin can see the phone-linked conversation.
        self.assertIn(b'Private branch B message',self.client.post('/client-files/',data={'id_number':self.record.id_number}).data)
        with self.client.session_transaction() as session: session['_user_id']=str(manager_id)
        self.assertNotIn(b'Private branch B message',self.client.post('/client-files/',data={'id_number':self.record.id_number}).data)
        self.record.branch='B'; db.session.commit()
        self.assertNotIn(b'TEST-ONLY',self.client.post('/client-files/',data={'id_number':self.record.id_number}).data)
        self.assertEqual(self.client.get(f'/client-files/?application_id={self.record.id}').status_code,403)

    def test_signature_requires_document_review_and_successful_pdf_save(self):
        from app.models import DocumentSignature
        self.record.sign_token = 'local-document-review-test'
        db.session.commit()
        token = self.record.sign_token
        public = self.app.test_client()
        self.assertEqual(public.get(f'/sign/{token}/review/application').status_code, 302)
        public.post(f'/sign/{token}', data={'action':'unlock','id_number':self.record.id_number})
        from PIL import Image
        stream = io.BytesIO(); Image.new('RGB',(80,30),'black').save(stream,format='PNG')
        form = {'action':'sign_document','document_type':'application','typed_name':'Fictional Test',
                'signature_data':'data:image/png;base64,'+base64.b64encode(stream.getvalue()).decode()}
        self.assertIn(b'Open the document',public.post(f'/sign/{token}',data=form).data)
        self.assertEqual(DocumentSignature.query.count(),0)
        public.get(f'/sign/{token}/review/application')
        with public.session_transaction() as session:
            form['review_nonce']=session[f'document_review_{self.record_id}_application']
        with patch('app.routes.signing._signable_pdf',side_effect=OSError('Cannot save document')):
            self.assertIn(b'Cannot save document',public.post(f'/sign/{token}',data=form).data)
        self.assertEqual(DocumentSignature.query.count(),0)
        blank=io.BytesIO();Image.new('RGB',(80,30),'white').save(blank,format='PNG')
        form['signature_data']='data:image/png;base64,'+base64.b64encode(blank.getvalue()).decode()
        self.assertIn(b'Please draw your signature',public.post(f'/sign/{token}',data=form).data)
        self.assertEqual(DocumentSignature.query.count(),0)

    def test_custom_smtp_sender_and_reply_address(self):
        # Generated only for the mocked SMTP connection; never a real credential.
        test_password = uuid.uuid4().hex
        settings = {'SMTP_HOST':'smtp.example.test','SMTP_PORT':'465','SMTP_SECURITY':'ssl',
                    'SMTP_USERNAME':'sales@example.test','SMTP_PASSWORD':test_password,
                    'MAIL_FROM':"Martin's Funerals <sales@example.test>",
                    'MAIL_REPLY_TO':'sales@example.test'}
        with patch.dict(os.environ,settings,clear=True), patch('app.services.email_service.smtplib.SMTP_SSL') as smtp:
            smtp.return_value.__enter__.return_value.send_message.return_value = {}
            self.assertTrue(send_email('recipient@example.test','Test','Body'))
            connection = smtp.return_value.__enter__.return_value
            connection.login.assert_called_once_with('sales@example.test',test_password)
            message = connection.send_message.call_args.args[0]
            self.assertEqual(message['Reply-To'],'sales@example.test')
            self.assertIn('sales@example.test',message['From'])
            self.assertEqual(smtp.call_args.args,('smtp.example.test',465))

    def test_custom_host_never_receives_legacy_gmail_credentials(self):
        with patch.dict(os.environ,{'SMTP_HOST':'smtp.example.test','GMAIL_SMTP_USER':'old@example.test','GMAIL_SMTP_PASSWORD':uuid.uuid4().hex},clear=True), patch('app.services.email_service.smtplib.SMTP_SSL') as smtp:
            self.assertFalse(send_email('recipient@example.test','Test','Body'))
            smtp.assert_not_called()

    def test_smtp_failure_returns_false(self):
        import smtplib
        with patch.dict(os.environ,{'GMAIL_SMTP_USER':'sender@example.test','GMAIL_SMTP_PASSWORD':uuid.uuid4().hex},clear=True), patch('app.services.email_service.smtplib.SMTP_SSL',side_effect=smtplib.SMTPAuthenticationError(535,b'bad credentials')):
            self.assertFalse(send_email('test@example.test','Test','Test'))


if __name__ == '__main__': unittest.main()

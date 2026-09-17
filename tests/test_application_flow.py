import json
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

    def test_login_and_home_open_callbacks_for_admin_and_employee(self):
        user = db.session.get(User, self.user_id)
        user.set_password('local-test-password')
        db.session.commit()
        for role_name in ['Super Admin', 'Agent']:
            user = db.session.get(User, self.user_id)
            user.role.name = role_name
            db.session.commit()
            response = self.client.post('/auth/login', data={'email': 'admin@example.test', 'password': 'local-test-password'})
            self.assertTrue(response.location.endswith('/recovery/callbacks'), response.location)
            self.assertTrue(self.client.get('/').location.endswith('/recovery/callbacks'))
            self.assertTrue(self.client.get('/home').location.endswith('/recovery/callbacks'))
            self.assertEqual(self.client.get('/recovery/callbacks').status_code, 200)

    def test_script_versions_preserve_existing_calls_and_answers(self):
        from app.routes.recovery import _current_script_steps
        sid = self._new_call_script(1, {'1': {'answer': 'yes', 'question': 'Historical wording', 'title': 'Original title'}})
        with self.app.app_context():
            old = TelesalesScriptSession.query.get(sid)
            answers_before = old.answers_json
            defaults = _current_script_steps()
        form = {f'enabled_{step["id"]}': 'on' for step in defaults}
        form['question_1'] = 'New question'
        form.pop('enabled_1')
        self.assertEqual(self.client.post('/recovery/scripts/admin/questions', data=form).status_code, 302)
        with self.app.app_context():
            old = TelesalesScriptSession.query.get(sid)
            self.assertEqual(old.answers_json, answers_before)
            self.assertEqual(_current_script_steps(old)[0]['question'], 'Historical wording')
            self.assertTrue(_current_script_steps(old)[0].get('enabled', True))
            self.assertEqual(_current_script_steps()[0]['question'], 'New question')
            self.assertFalse(_current_script_steps()[0]['enabled'])
        self.client.post('/recovery/scripts/admin/questions/reset')
        with self.app.app_context():
            self.assertEqual(_current_script_steps(TelesalesScriptSession.query.get(sid))[0]['question'], 'Historical wording')

    def test_all_disabled_questions_complete_without_recording_consent(self):
        self.client.post('/recovery/scripts/admin/questions', data={})
        sid = self._new_call_script(1)
        with patch('app.routes.recovery._save_script_pdf'):
            response = self.client.get(f'/recovery/script/{sid}')
        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            call = TelesalesScriptSession.query.get(sid)
            self.assertEqual(call.status, 'Completed')
            answers = json.loads(call.answers_json)
            self.assertEqual(len(answers), 31)
            self.assertEqual({item['answer'] for item in answers.values()}, {'skipped'})
            self.assertEqual(call.qa_score, 0)

    def test_callback_time_reminders_and_completion(self):
        sid = self._new_call_script(1)
        call = db.session.get(TelesalesScriptSession, sid)
        pid = call.lapsed_policy_id
        response = self.client.post(f'/recovery/{pid}/schedule-callback', data={'callback_at':'2020-01-01T14:30'})
        self.assertEqual(response.status_code,302)
        p = db.session.get(LapsedPolicy,pid)
        self.assertEqual(p.callback_at.hour,14)
        self.assertEqual(p.callback_at.minute,30)
        self.assertEqual(p.recovery_status,'Callback')
        self.assertEqual(len(self.client.get('/recovery/callback-reminders').json['reminders']),1)
        self.assertIn('2020-01-01 14:30', self.client.get('/recovery/callbacks').get_data(as_text=True))
        self.client.post(f'/recovery/{pid}/schedule-callback', data={'callback_at':'2099-01-01T14:30'})
        self.assertEqual(self.client.get('/recovery/callback-reminders').json['reminders'],[])
        p = db.session.get(LapsedPolicy,pid)
        p.recovery_status='Closed'; db.session.commit()
        self.assertEqual(self.client.get('/recovery/callback-reminders').json['reminders'],[])

    def test_callback_schedule_rejects_other_agents_and_invalid_time(self):
        sid = self._new_call_script(1)
        pid = db.session.get(TelesalesScriptSession,sid).lapsed_policy_id
        self.client.post(f'/recovery/{pid}/schedule-callback',data={'callback_at':'invalid'})
        self.assertIsNone(db.session.get(LapsedPolicy,pid).callback_at)
        user=db.session.get(User,self.user_id);user.role=Role(name='Agent')
        db.session.get(LapsedPolicy,pid).assigned_agent_id=None;db.session.commit()
        self.assertEqual(self.client.post(f'/recovery/{pid}/schedule-callback',data={'callback_at':'2020-01-01T10:00'}).status_code,403)
        self.assertEqual(self.client.get('/recovery/callback-reminders').json['reminders'],[])

    def _new_call_script(self, step, answers=None):
        import json
        policy = LapsedPolicy(initials='Test', surname='Callback', cell_number='0821234567',
                              branch='A', assigned_agent_id=self.user_id, recovery_status='Callback')
        session = TelesalesScriptSession(lapsed_policy=policy, agent_id=self.user_id, branch='A',
                                        client_name='Test Callback', current_step=step, status='In Progress',
                                        answers_json=json.dumps(answers or {}))
        db.session.add_all([policy, session]); db.session.commit()
        return session.id

    def test_script_separates_employee_confirmations_from_client_answers(self):
        import json
        sid = self._new_call_script(2)
        path = f'/recovery/script/{sid}'
        page = self.client.get(path).get_data(as_text=True)
        self.assertIn('Explained / completed', page)
        self.client.post(path, data={'step_id': '2', 'answer': 'no'})
        self.assertEqual(db.session.get(TelesalesScriptSession, sid).current_step, 2)
        self.client.post(path, data={'step_id': '2', 'answer': 'yes'})
        self.assertEqual(db.session.get(TelesalesScriptSession, sid).current_step, 3)
        self.client.post(path, data={'step_id': '2', 'answer': 'yes'})
        self.assertEqual(db.session.get(TelesalesScriptSession, sid).current_step, 3)
        sid = self._new_call_script(8)
        self.client.post(f'/recovery/script/{sid}', data={'step_id':'8', 'answer':'no'})
        session = db.session.get(TelesalesScriptSession, sid)
        self.assertEqual(session.current_step, 9)
        self.assertEqual(json.loads(session.answers_json)['8']['answer'], 'no')

    def test_script_counts_lives_and_skips_non_debit_questions(self):
        import json
        sid = self._new_call_script(10)
        path = f'/recovery/script/{sid}'
        self.client.post(path, data={'step_id':'10', 'answer':'yes'})
        self.assertEqual(db.session.get(TelesalesScriptSession, sid).current_step, 10)
        self.client.post(path, data={'step_id':'10', 'number_of_lives':'4'})
        session = db.session.get(TelesalesScriptSession, sid)
        self.assertEqual(json.loads(session.answers_json)['10']['number_of_lives'], 4)
        for method in ['Cash', 'Stop Order']:
            sid = self._new_call_script(27, {'19': {'payment_method': method}})
            self.client.get(f'/recovery/script/{sid}')
            session = db.session.get(TelesalesScriptSession, sid)
            self.assertEqual(session.current_step, 29)
            self.assertEqual(json.loads(session.answers_json)['28']['answer'], 'na')

    def test_debit_consent_no_blocks_application(self):
        sid = self._new_call_script(28, {'19': {'payment_method': 'Debit Order'}})
        with patch('app.routes.recovery._save_script_pdf'):
            self.client.post(f'/recovery/script/{sid}', data={'step_id':'28', 'answer':'no'})
        self.assertEqual(db.session.get(TelesalesScriptSession, sid).status, 'Blocked')

    def test_complete_call_script_saves_answers_without_sending(self):
        import json
        sid = self._new_call_script(1)
        path = f'/recovery/script/{sid}'
        product_id = self.record.product_id
        fields = {
            9: {'coverage_choice':'myself_spouse_children', 'client_id_number':'8001015009087'},
            10: {'number_of_lives':'4'}, 11: {'product_id':str(product_id)},
            19: {'payment_method':'Cash'},
            26: {'beneficiary_name':'Test Person', 'beneficiary_contact':'0821234567', 'beneficiary_relationship':'Spouse'},
            31: {'delivery_method':'auto'},
        }
        with patch('app.routes.recovery._save_script_pdf'), patch('app.services.email_service.send_email') as email:
            for expected in [*range(1,27),29,30,31]:
                page = self.client.get(path, follow_redirects=True)
                self.assertEqual(page.status_code, 200, expected)
                self.assertEqual(db.session.get(TelesalesScriptSession, sid).current_step, expected)
                data = {'step_id':str(expected), 'answer':'yes', **fields.get(expected,{})}
                response = self.client.post(path, data=data)
                self.assertEqual(response.status_code, 302, expected)
            email.assert_not_called()
        session = db.session.get(TelesalesScriptSession, sid)
        self.assertEqual(session.status,'Completed')
        self.assertEqual(json.loads(session.answers_json)['10']['number_of_lives'],4)

    def test_unfinished_script_is_searchable_and_resumed_without_duplicate(self):
        import json
        sid = self._new_call_script(8, {'2': {'answer':'yes', 'note':'Saved conversation'}})
        session = db.session.get(TelesalesScriptSession, sid)
        pid = session.lapsed_policy_id
        session.policy_number = 'RESUME-POLICY'
        session.client_cell = '0821234567'
        db.session.commit()
        for term in ['Callback', '0821234567', 'RESUME-POLICY']:
            response = self.client.get('/recovery/not-finalised', query_string={'q':term})
            self.assertEqual(response.status_code,200)
            self.assertIn('Resume script',response.get_data(as_text=True))
        before = TelesalesScriptSession.query.count()
        response = self.client.get(f'/recovery/{pid}/script/start')
        self.assertTrue(response.location.endswith(f'/recovery/script/{sid}'))
        self.assertEqual(TelesalesScriptSession.query.count(),before)
        session = db.session.get(TelesalesScriptSession,sid)
        self.assertEqual(session.current_step,8)
        self.assertEqual(json.loads(session.answers_json)['2']['note'],'Saved conversation')

    def test_script_navigation_respects_employee_scope_and_admin_setup(self):
        sid = self._new_call_script(8)
        private = self._new_call_script(8)
        other_role = Role(name='Agent')
        other = User(name='Other',email='other@example.test',password_hash='unused',role=other_role,branch='B')
        db.session.add(other);db.session.flush()
        private_session = db.session.get(TelesalesScriptSession,private)
        private_session.agent_id=other.id;private_session.branch='B';private_session.client_name='PRIVATE OTHER CLIENT'
        db.session.commit()
        self.assertEqual(self.client.get('/recovery/scripts/admin/questions').status_code,200)
        user=db.session.get(User,self.user_id);user.role=Role.query.filter_by(name='Agent').one();db.session.commit()
        for path in ['/recovery/not-finalised','/recovery/scripts']:
            response=self.client.get(path)
            self.assertEqual(response.status_code,200)
            self.assertIn('Resume script',response.get_data(as_text=True))
            self.assertNotIn('PRIVATE OTHER CLIENT',response.get_data(as_text=True))
        self.assertEqual(self.client.get('/recovery/scripts/admin/questions').status_code,403)
        self.assertEqual(self.client.get(f'/recovery/script/{private}').status_code,403)

    def test_finished_script_cannot_be_changed_by_resume_post(self):
        sid=self._new_call_script(8,{'8':{'answer':'no'}})
        session=db.session.get(TelesalesScriptSession,sid);session.status='Completed';db.session.commit()
        response=self.client.post(f'/recovery/script/{sid}',data={'answer':'yes','step_id':'8'})
        self.assertTrue(response.location.endswith('/complete'))
        self.assertEqual(db.session.get(TelesalesScriptSession,sid).answers_json,'{"8": {"answer": "no"}}')

    def test_finalised_applications_leave_unfinished_list(self):
        self.record.status='Draft';db.session.commit()
        self.assertIn('TEST-ONLY',self.client.get('/recovery/not-finalised').get_data(as_text=True))
        self.record.status='QA Approved';db.session.commit()
        self.assertNotIn('TEST-ONLY',self.client.get('/recovery/not-finalised').get_data(as_text=True))

    def tearDown(self):
        db.session.remove(); db.drop_all(); self.ctx.pop(); self.tmp.cleanup()

    def test_import_email_is_optional_but_identity_and_phone_are_required(self):
        from app.routes.recovery import _missing_contact_fields, _policy_missing_fields
        row={'ID_Number':'8001015009087','Cell_Number':'0821234567','Email Address':''}
        self.assertEqual(_missing_contact_fields(row)[0],[])
        self.assertEqual(_policy_missing_fields(LapsedPolicy(id_number=row['ID_Number'],cell_number=row['Cell_Number'],email_address='')),[])
        row['ID_Number']=''
        self.assertIn('ID number',_missing_contact_fields(row)[0])
        row['Cell_Number']=''
        self.assertIn('contact number',_missing_contact_fields(row)[0])

    def test_email_priority_and_whatsapp_fallback(self):
        from app.routes.recovery import _send_script_selected_signing_link
        with self.app.test_request_context('/'), patch('app.routes.recovery.ensure_screened',return_value=(True,[])), patch('app.routes.recovery.send_email',return_value=True) as mail, patch('app.routes.recovery.send_whatsapp_message',return_value=True) as wa:
            self.assertTrue(_send_script_selected_signing_link(self.record,'auto')[1])
            mail.assert_called_once();wa.assert_not_called()
        with self.app.test_request_context('/'), patch('app.routes.recovery.ensure_screened',return_value=(True,[])), patch('app.routes.recovery.send_email',return_value=False) as mail, patch('app.routes.recovery.send_whatsapp_message',return_value=True) as wa:
            self.assertTrue(_send_script_selected_signing_link(self.record,'auto')[1])
            mail.assert_called_once();wa.assert_called_once()
        self.record.email='';db.session.commit()
        with self.app.test_request_context('/'), patch('app.routes.recovery.ensure_screened',return_value=(True,[])), patch('app.routes.recovery.send_email') as mail, patch('app.routes.recovery.send_whatsapp_message',return_value=True) as wa:
            self.assertTrue(_send_script_selected_signing_link(self.record,'auto')[1])
            mail.assert_not_called();wa.assert_called_once()

    def test_document_email_validation_and_optional_blank(self):
        from app.services.delivery_preferences import receipt_address
        self.assertEqual(receipt_address({'document_email':'','document_email_confirm':''},self.record),'')
        with self.assertRaises(ValueError):receipt_address({'document_email':'bad','document_email_confirm':'bad'},self.record)
        with self.assertRaises(ValueError):receipt_address({'document_email':'copy@example.test','document_email_confirm':'other@example.test'},self.record)
        self.assertEqual(receipt_address({'document_email':'copy@example.test','document_email_confirm':'copy@example.test'},self.record),'copy@example.test')
        self.assertEqual(self.record.email,'test@example.test')

    def test_upload_duplicate_rejected_and_audited(self):
        from app.models import ClientFicaDocument, AuditLog
        payload=b'%PDF-1.4 fictional manual document'
        response=self.client.post(f'/documents/application/{self.record_id}', data={'document_type':'application','file':(io.BytesIO(payload),'manual.pdf')})
        self.assertEqual(response.status_code,302)
        self.assertEqual(ClientFicaDocument.query.count(),1)
        self.assertEqual(ClientStoredFile.query.count(),1)
        response=self.client.post(f'/documents/application/{self.record_id}', data={'document_type':'application','file':(io.BytesIO(payload),'renamed.pdf')})
        self.assertEqual(response.status_code,302)
        self.assertEqual(ClientFicaDocument.query.count(),1)
        self.assertEqual(ClientStoredFile.query.count(),1)
        self.assertEqual(AuditLog.query.filter_by(action='Duplicate upload rejected').count(),1)
        from app.services.document_status_service import document_summary
        row=next(r for r in document_summary(self.record)['rows'] if r['key']=='application')
        self.assertEqual(row['status'],'Needs Review')
        self.client.post(f'/documents/fica/{ClientFicaDocument.query.one().id}/approve')
        row=next(r for r in document_summary(self.record)['rows'] if r['key']=='application')
        self.assertEqual(row['status'],'Approved')

    def test_qa_missing_documents_cannot_be_checkbox_approved(self):
        from app.routes.qa import QA_CHECKLIST
        from app.models import ComplianceReview
        data={key:'on' for key,label in QA_CHECKLIST};data['decision']='QA Approved'
        self.assertEqual(self.client.post(f'/qa/application/{self.record_id}',data=data).status_code,302)
        self.assertEqual(ComplianceReview.query.count(),0)
        self.assertEqual(self.client.get(f'/qa/application/{self.record_id}').status_code,200)
        self.assertEqual(self.client.get('/qa/').status_code,200)

    def test_live_performance_and_visible_search(self):
        response=self.client.get('/wallboard/data')
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.json['sales_today'],1)
        self.assertTrue(any(a['sales']==1 for a in response.json['agents']))
        response=self.client.get('/client-files/?id_number=8001015009087')
        self.assertEqual(response.status_code,200)
        self.assertIn(b'TEST-ONLY',response.data)
        self.assertIn(b'Search clients by main member ID',response.data)

    def test_popia_choices_update_lists_and_preserve_other_blocks(self):
        from app.services.marketing_consent import apply_consent, consent_value, telephone_blocked
        from app.services.communication_service import preference_for, contact_hash, normalize_phone
        from app.models import ContactSuppression
        apply_consent(self.record, True); db.session.commit()
        policy=LapsedPolicy.query.one(); contact=WhatsAppContact.query.one()
        self.assertTrue(consent_value(self.record))
        self.assertTrue(preference_for(policy).whatsapp_allowed)
        apply_consent(self.record, False); db.session.commit()
        self.assertTrue(telephone_blocked(policy)); self.assertTrue(contact.opted_out)
        self.assertEqual(policy.recovery_status, 'Opted Out')
        from app.routes.recovery import open_recovery_query
        with self.app.test_request_context('/'):
            from flask_login import login_user
            login_user(db.session.get(User, self.user_id))
            self.assertEqual(open_recovery_query().count(), 0)
        apply_consent(self.record, True); db.session.commit()
        policy=LapsedPolicy.query.one();contact=WhatsAppContact.query.one()
        self.assertFalse(telephone_blocked(policy)); self.assertFalse(contact.opted_out)
        self.assertEqual(LapsedPolicy.query.count(), 1);self.assertEqual(WhatsAppContact.query.count(), 1)
        db.session.add(ContactSuppression(phone_hash=contact_hash(normalize_phone(self.record.cell_number)),source='manual',reason='Administrative block'))
        db.session.commit()
        apply_consent(self.record, False);apply_consent(self.record, True);db.session.commit()
        self.assertTrue(telephone_blocked(policy));self.assertTrue(contact.opted_out)
        self.assertEqual(ContactSuppression.query.filter_by(source='manual').count(),1)

    def test_one_signature_does_not_complete_or_copy_to_other_fields(self):
        from app.models import DocumentSignature
        from app.services.signature_fields import application_fields, signed_documents
        from app.services.pdf_service import generate_application_pdf
        from PIL import Image, ImageDraw
        from pypdf import PdfReader
        import json
        path=str(Path(self.tmp.name)/'one.png')
        im=Image.new('RGBA',(80,30));ImageDraw.Draw(im).line([(2,25),(40,2),(75,20)],fill='black',width=2);im.save(path)
        db.session.add(DocumentSignature(application_id=self.record.id,document_type='application:principal',signature_image_path=path,typed_name='Fictional Test'))
        db.session.flush()
        self.assertNotIn('application', signed_documents(self.record))
        dest=str(Path(application_folder(self.record))/'one.pdf');generate_application_pdf(self.record,dest)
        pdf=PdfReader(dest);targets=json.loads(pdf.metadata['/Subject'].removeprefix('martins-signature:'))['fields']
        self.assertEqual(sum(t['signed'] for t in targets),1)
        self.assertFalse(next(t['signed'] for t in targets if t['page']==2))
        self.assertFalse(any(image.image.size==(80,30) for image in pdf.pages[1].images))

    def test_failed_screening_blocks_email_and_token(self):
        with patch('app.routes.applications.ensure_screened',return_value=(False,['FIC unavailable'])), patch('app.routes.applications.send_email') as mail:
            response=self.client.post(f'/applications/{self.record_id}/send-sign-link',follow_redirects=True)
            self.assertIn(b'FIC unavailable',response.data)
            mail.assert_not_called()
        self.assertIsNone(self.record.sign_token)

    def upload_screening(self,outcome='no_match',data=None):
        from datetime import datetime,timedelta
        from PIL import Image
        raw=io.BytesIO();Image.new('RGB',(150,100),'white').save(raw,format='PNG');raw.seek(0)
        return self.client.post(f'/applications/{self.record_id}/screening',data=data or dict(action='upload',outcome=outcome,notes='Searched ID and full name and reviewed both results.',checked_at=(datetime.utcnow()+timedelta(hours=2)).isoformat(timespec='minutes'),confirmed='yes',screenshots=(raw,'result.png')),content_type='multipart/form-data',follow_redirects=True)

    def test_employee_screening_requires_evidence_and_identity_match(self):
        from app.services.screening_service import ensure_screened,latest
        self.assertFalse(ensure_screened(self.record)[0])
        response=self.upload_screening()
        self.assertIn(b'screenshots saved',response.data)
        self.assertTrue(ensure_screened(self.record)[0]);self.assertEqual(latest(self.record).status,'Employee checked')
        stored=ClientStoredFile.query.one();self.assertTrue(stored.content.startswith(b'\x89PNG'))
        self.record.surname='Changed';db.session.commit()
        self.assertFalse(ensure_screened(self.record)[0])

    def test_possible_match_blocks_until_admin_review(self):
        from app.services.screening_service import ensure_screened,latest
        self.upload_screening('possible_match');row=latest(self.record)
        self.assertFalse(ensure_screened(self.record)[0])
        self.client.post(f'/applications/{self.record_id}/screening',data={'action':'review','screening_id':row.id,'notes':'Reviewed independently; fictional test record is not the returned person.','confirmed':'yes'})
        self.assertTrue(ensure_screened(self.record)[0])

    def test_invalid_screenshot_does_not_release_application(self):
        from datetime import datetime,timedelta
        from app.services.screening_service import ensure_screened,latest
        self.upload_screening(data=dict(action='upload',outcome='no_match',notes='Searched full name and ID.',confirmed='yes',checked_at=(datetime.utcnow()+timedelta(hours=2)).isoformat(timespec='minutes'),screenshots=(io.BytesIO(b'not an image'),'fake.png')))
        self.assertIsNone(latest(self.record));self.assertFalse(ensure_screened(self.record)[0])

    def test_chat_history_combines_channels_and_saves_external_reply(self):
        from datetime import datetime,timedelta
        from app.services.conversation_history import record_communication
        from app.models import ClientCommunication
        for channel,body,offset in [('Call','Discussed cover and debit order',0),('Email','Please review https://example.test/sign/secret-token',1),('WhatsApp','Thank you for the information',2)]:
            record_communication(channel,body,'Recorded',application_id=self.record_id,occurred_at=datetime(2026,1,2,9,offset))
        db.session.commit()
        self.client.post('/client-files/communication',data=dict(application_id=self.record_id,channel='Email',direction='inbound',subject='Client reply',body='I received the documents.',occurred_at='2026-01-02T12:00'))
        response=self.client.get(f'/client-files/?application_id={self.record_id}')
        text=response.data.decode()
        self.assertIn('11:00:00',text);self.assertNotIn('secret-token',text)
        self.assertLess(text.index('Discussed cover'),text.index('Please review'))
        self.assertLess(text.index('Please review'),text.index('Thank you for the information'))
        self.assertIn('I received the documents.',text);self.assertEqual(ClientCommunication.query.count(),4)

    def test_debit_details_require_separate_account_signatures(self):
        from app.services.signature_fields import application_fields
        self.record.account_number='TEST-ONLY'
        fields=application_fields(self.record)
        self.assertIn('application:account',[f['key'] for f in fields])
        self.assertIn('application:terms_account',[f['key'] for f in fields])
        self.assertTrue(all(f['rect'][3]-f['rect'][1]>=30 for f in fields if f['page']==2))

    def test_cdd_requires_complete_answers_and_invalidates_changed_signature(self):
        from app.services.cdd_service import save_answers, FIELDS, completed
        from app.models import DocumentSignature
        with self.assertRaises(ValueError):save_answers(self.record,{})
        answers={k:o.split('|')[0] if o else 'Fictional value' for k,l,o in FIELDS}
        answers['birth_date']='1980-01-01';save_answers(self.record,answers)
        db.session.add(DocumentSignature(application_id=self.record_id,document_type='cdd',signature_image_path='unused',typed_name='Fictional'));db.session.flush()
        self.assertTrue(completed(self.record))
        answers['birth_place']='Changed birthplace';answers['birth_date']='1980-01-01';save_answers(self.record,answers)
        self.assertEqual(DocumentSignature.query.filter_by(document_type='cdd').count(),0)

    def test_persisted_birth_date_opens_every_debit_order_document(self):
        from app.services.cdd_service import answers_for
        self.record.date_of_birth='01/01/1980'
        self.record.payment_method='Debit Order'
        self.record.account_number='TEST-ONLY'
        self.record.sign_token='local-regression-test'
        db.session.commit();db.session.expire_all()
        self.assertIsInstance(self.record.date_of_birth,str)
        self.assertEqual(answers_for(self.record)['birth_date'],'1980-01-01')
        public=self.app.test_client()
        with public.session_transaction() as session:session[f'sign_unlocked_{self.record_id}']=True
        for kind in ['application','popia','disclosure','welcome','cdd']:
            self.assertEqual(public.get('/sign/local-regression-test/review/'+kind).status_code,200,kind)
            self.assertEqual(public.get('/sign/local-regression-test/document/'+kind).status_code,200,kind)
        self.record.date_of_birth='1980-01-01';db.session.commit()
        self.assertEqual(answers_for(self.record)['birth_date'],'1980-01-01')

    def test_bank_statement_not_required_for_debit_order(self):
        from app.routes.signing import _required_fica_types
        from app.services.document_status_service import required_fica_types,document_summary
        from app.services.pdf_service import generate_fica_pdf
        from pypdf import PdfReader
        self.record.payment_method='Debit Order'
        self.assertNotIn('bank_statement',_required_fica_types(self.record))
        self.assertNotIn('bank_statement',required_fica_types(self.record))
        self.assertNotIn('bank_statement',[row['key'] for row in document_summary(self.record)['missing']])
        path=str(Path(application_folder(self.record))/'fica.pdf');generate_fica_pdf(self.record,path)
        text=' '.join(page.extract_text() for page in PdfReader(path).pages)
        self.assertNotIn('Bank statement',text);self.assertNotIn('Bank Verification',text)

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
            self.assertEqual(next(f['rect'] for f in target['fields'] if f['key']=='application:principal'), SIGNATURE_RECTS[template])
            self.assertTrue(any(f['page']==2 for f in target['fields']))
            self.assertEqual(len(pdf.pages), 3)

    def test_email_failure_is_not_reported_as_sent(self):
        with patch('app.routes.applications.ensure_screened', return_value=(True,[])), patch('app.routes.applications.send_email', return_value=False), patch('app.routes.applications.send_whatsapp_text') as wa:
            wa.return_value.ok=False
            response = self.client.post(f'/applications/{self.record.id}/send-sign-link', follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.record.status, 'Signing Link Prepared')
        self.assertIn(b'Email was not sent', response.data)

    def test_script_email_sign_upload_restore_and_search(self):
        from app.routes.recovery import _send_script_selected_signing_link
        db.session.add(TelesalesScriptSession(application_id=self.record_id, agent_id=self.user_id, branch='A', client_name='Fictional Test', status='Completed', answers_json='{}'))
        db.session.commit()
        with self.app.test_request_context('/'), patch('app.routes.recovery.ensure_screened', return_value=(True,[])), patch('app.routes.recovery.send_email', return_value=True) as mail:
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
        from app.services.signature_fields import application_fields
        for kind in ['application','popia','disclosure','welcome','cdd']:
            keys=[f['key'] for f in application_fields(self.record)] if kind=='application' else [kind]
            for index,key in enumerate(keys):
                self.assertEqual(public.get(f'/sign/{token}/review/{kind}').status_code, 200)
                with public.session_transaction() as session:
                    nonce = session[f'document_review_{self.record_id}_{kind}']
                if kind=='popia':
                    public.post(f'/sign/{token}',data={'action':'save_marketing_consent','marketing_choice':'no','review_nonce':nonce})
                    public.get(f'/sign/{token}/review/{kind}')
                    with public.session_transaction() as session:
                        nonce=session[f'document_review_{self.record_id}_{kind}']
                if kind=='cdd':
                    self.assertIn(b'value="0821234567"',public.get(f'/sign/{token}/review/cdd').data)
                    with public.session_transaction() as session:nonce=session[f'document_review_{self.record_id}_{kind}']
                    from app.services.cdd_service import FIELDS
                    answers={key:options.split('|')[-1] if options else 'Fictional answer' for key,label,options in FIELDS}
                    answers['birth_date']='1980-01-01'
                    public.post(f'/sign/{token}',data=dict(answers,action='save_cdd',review_nonce=nonce))
                    public.get(f'/sign/{token}/review/{kind}')
                    with public.session_transaction() as session:
                        nonce=session[f'document_review_{self.record_id}_{kind}']
                response = public.post(f'/sign/{token}',data={'action':'sign_document','document_type':kind,'signature_field':key,'typed_name':'Fictional Test','signature_data':signature,'review_nonce':nonce})
                self.assertEqual(response.status_code,302)
                self.assertTrue(response.location.endswith(f'/sign/{token}') if index==len(keys)-1 else '/review/application' in response.location)
            from pypdf import PdfReader
            with public.get(f'/sign/{token}/document/{kind}') as saved:
                pdf = PdfReader(io.BytesIO(saved.data))
                import json
                target = json.loads(pdf.metadata['/Subject'].removeprefix('martins-signature:'))
                targets=target['fields'] if kind=='application' else [target]
                for field in targets:
                    page = pdf.pages[field['page']-1]
                    self.assertTrue(any(img.image.size == (80,30) for img in page.images))
                self.assertIn(b'Document signed', public.get(f'/sign/{token}/review/{kind}').data)
        with patch.dict(os.environ,{'MAIL_DOCUMENTS_TO':'copies@example.test'}), patch('app.routes.signing.send_email', return_value=True) as delivery:
            response = public.post(f'/sign/{token}',data={'action':'final_submit','document_email':'copies@example.test','document_email_confirm':'copies@example.test'})
            self.assertEqual(delivery.call_args.args[0],'copies@example.test')
            self.assertEqual(self.record.document_email,'copies@example.test')
            self.assertEqual(delivery.call_count,1)
            self.assertEqual(len(delivery.call_args.args[3]),5)
            self.assertTrue(all(Path(p).exists() for p in delivery.call_args.args[3]))
            self.assertIn('attached for your records',delivery.call_args.args[2])
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
        form = {'action':'sign_document','document_type':'application','signature_field':'application:principal','typed_name':'Fictional Test',
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
            self.assertTrue(send_email('recipient@example.test','Test','Body',application_id=self.record_id))
            from app.models import ClientCommunication
            db.session.flush()
            self.assertEqual(ClientCommunication.query.one().body,'Body')
            self.assertEqual(ClientCommunication.query.one().status,'Accepted by mail server')
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

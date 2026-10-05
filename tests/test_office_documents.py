import os,tempfile,unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from flask import g
os.environ['DATABASE_URL']='sqlite:///:memory:'
os.environ['ENABLE_WHATSAPP_SCHEDULER']='0'
os.environ['AUTO_CREATE_TABLES']='1'
from app import create_app,db
from app.models import User,Role,ClientApplication,ApplicationJourney,BranchFilingOffice,ApplicationFilingDelivery,AuditLog
from app.services.client_storage import application_folder
from app.services.branch_filing import send_branch_pack

class OfficeDocumentTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.app=create_app();self.app.config.update(TESTING=True,UPLOAD_FOLDER=self.tmp.name)
        @self.app.before_request
        def clear_user(): g.pop('_login_user',None)
        self.ctx=self.app.app_context();self.ctx.push();db.drop_all();db.create_all()
        owner=User(name='Wjm Piek',email='wjm@martinsdirect.com',role=Role(name='Super Admin'),branch='Brokers',active=True,password_hash='unused')
        db.session.add(owner);db.session.flush();self.uid=owner.id
        app=ClientApplication(application_ref='TEST-FILING',policy_number='TEST-POLICY',first_names='Sample',surname='Client',id_number='8001015009087',branch='Brokers',filing_branch='Northcliff',agent_id=owner.id,status='Active',signed_at=datetime.utcnow())
        db.session.add(app);db.session.flush();self.aid=app.id
        db.session.add(ApplicationJourney(application_id=app.id,notice_status='Sent',activated_at=datetime.utcnow(),signed_bundle_at=datetime.utcnow()))
        db.session.add(BranchFilingOffice(branch_key='northcliff',branch_name='Northcliff',email='filing@example.com'));db.session.commit()
        self.client=self.app.test_client();self.login(self.uid)
        with self.client.session_transaction() as s: s['branch_filing_csrf']='test-csrf'
    def tearDown(self): db.session.remove();self.ctx.pop();self.tmp.cleanup()
    def login(self,uid):
        g.pop('_login_user',None)
        with self.client.session_transaction() as s: s['_user_id']=str(uid);s['_fresh']=True
    def pack(self,missing=None):
        a=db.session.get(ClientApplication,self.aid);folder=Path(application_folder(a))
        for n in ['signed_application','welcome_pack','popia_consent','policy_disclosure','annexure_j1']:
            if n!=missing:(folder/f'{n}_{self.aid}.pdf').write_bytes(b'%PDF-1.4 synthetic test file')
    def post(self,path,data=None): return self.client.post(path,data=dict(data or {},branch_filing_csrf='test-csrf'))
    def test_missing_directory_records_no_send(self):
        db.session.delete(BranchFilingOffice.query.one());db.session.commit()
        with patch('app.services.branch_filing.send_email') as mail:
            attempt=send_branch_pack(self.aid,self.uid);self.assertEqual(attempt.status,'Not configured');mail.assert_not_called()
    def test_missing_pack_records_failure(self):
        self.pack('annexure_j1')
        with patch('app.services.branch_filing.send_email') as mail:
            attempt=send_branch_pack(self.aid,self.uid);self.assertEqual(attempt.status,'Failed');mail.assert_not_called()
    def test_send_snapshots_recipient_and_blocks_duplicates(self):
        self.pack()
        with patch('app.services.branch_filing.send_email',return_value=True) as mail:
            first=send_branch_pack(self.aid,self.uid);fid=first.id
            self.assertEqual(first.recipient_email,'filing@example.com');self.assertEqual(first.branch_name,'Northcliff');self.assertEqual(len(mail.call_args.args[3]),5)
            self.assertEqual(send_branch_pack(self.aid,self.uid).id,fid);self.assertEqual(mail.call_count,1)
            send_branch_pack(self.aid,self.uid,resend=True);self.assertEqual(mail.call_count,2)
        office=BranchFilingOffice.query.one();office.email='new-filing@example.com';db.session.commit()
        self.assertEqual(db.session.get(ApplicationFilingDelivery,fid).recipient_email,'filing@example.com')
        db.session.delete(office);db.session.commit();self.assertEqual(db.session.get(ApplicationFilingDelivery,fid).branch_name,'Northcliff')
    def test_failed_send_retries_and_client_notice_is_independent(self):
        self.pack();a=db.session.get(ClientApplication,self.aid);a.whatsapp_journey.notice_status='Failed';db.session.commit()
        with patch('app.services.branch_filing.send_email',side_effect=[False,True]) as mail:
            self.assertEqual(send_branch_pack(self.aid,self.uid).status,'Failed');self.assertEqual(send_branch_pack(self.aid,self.uid).status,'Accepted by mail server');self.assertEqual(mail.call_count,2)
    def test_nonapproved_application_cannot_send(self):
        a=db.session.get(ClientApplication,self.aid);a.status='New';db.session.commit()
        with patch('app.services.branch_filing.send_email') as mail:
            with self.assertRaises(ValueError):send_branch_pack(self.aid,self.uid)
            mail.assert_not_called()
    def test_directory_edit_delete_and_validation(self):
        self.assertEqual(self.post('/settings/branch-filing',{'branch_name':'West','email':'west@example.com'}).status_code,302)
        office=BranchFilingOffice.query.filter_by(branch_key='west').one();oid=office.id
        self.post('/settings/branch-filing',{'office_id':oid,'branch_name':'West renamed','email':'updated@example.com'})
        self.assertEqual(db.session.get(BranchFilingOffice,oid).email,'updated@example.com')
        self.post('/settings/branch-filing',{'branch_name':'Bad','email':'invalid'})
        self.assertIsNone(BranchFilingOffice.query.filter_by(branch_key='bad').first())
        self.post('/settings/branch-filing',{'office_id':oid,'action':'delete'});self.assertIsNone(db.session.get(BranchFilingOffice,oid))
        self.assertEqual(self.client.post('/settings/branch-filing',data={'branch_name':'Forged'}).status_code,400)
    def test_only_owner_manages_directory_and_cross_branch_access(self):
        user=User(name='Agent B',email='agent-b@example.com',role=Role(name='Agent'),branch='Other',active=True,owner_approved_at=datetime.utcnow(),password_hash='unused');db.session.add(user);db.session.commit();uid=user.id
        self.login(uid);self.assertEqual(self.client.get('/settings/branch-filing').status_code,403)
        with patch('app.services.branch_filing.send_email') as mail:
            self.assertEqual(self.post(f'/applications/{self.aid}/send-office-documents',{'filing_branch':'Northcliff'}).status_code,403);mail.assert_not_called()
    def test_application_save_selector_send_and_csrf(self):
        self.pack();a=db.session.get(ClientApplication,self.aid);a.status='New';db.session.commit()
        with patch('app.services.branch_filing.send_email') as mail:
            self.post(f'/applications/{self.aid}/send-office-documents',{'filing_branch':'Northcliff','save_destination':'1'});mail.assert_not_called()
        a=db.session.get(ClientApplication,self.aid);a.status='Active';db.session.commit()
        with patch('app.services.branch_filing.send_email',return_value=True):
            self.post(f'/applications/{self.aid}/send-office-documents',{'filing_branch':'Northcliff'})
        page=self.client.get(f'/applications/{self.aid}');self.assertIn(b'filing@example.com',page.data);self.assertIn(b'Accepted by mail server',page.data)
        self.assertEqual(self.client.post(f'/applications/{self.aid}/send-office-documents',data={'filing_branch':'Northcliff'}).status_code,400)
    def test_report_export_and_reports_navigation(self):
        self.pack()
        with patch('app.services.branch_filing.send_email',return_value=True):send_branch_pack(self.aid,self.uid)
        page=self.client.get('/reports/branch-filing?q=TEST-FILING');self.assertEqual(page.status_code,200);self.assertIn(b'filing@example.com',page.data)
        self.assertIn(b'TEST-FILING',self.client.get('/reports/branch-filing?export=csv').data)
        from flask import render_template
        with self.app.test_request_context('/reports/branch-filing'):
            from flask_login import login_user
            login_user(db.session.get(User,self.uid));html=render_template('_sidebar.html',admin_role=True,manager_role=False,unread_notification_count=0)
            reports=html.split('Reports<small>')[1].split('</details>')[0];admin=html.split('Administration<small>')[1]
            self.assertIn('Audit reports',reports);self.assertIn('WhatsApp audit',reports);self.assertNotIn('Audit reports',admin)
    def test_approval_sends_branch_pack_even_if_client_email_fails(self):
        from app.routes.qa import QA_CHECKLIST
        self.pack();a=db.session.get(ClientApplication,self.aid);a.status='FICA Outstanding';a.whatsapp_journey.activated_at=None;db.session.commit()
        data={key:'on' for key,label in QA_CHECKLIST};data.update(decision='Compliance Approved',policy_number='TEST-POLICY',start_date=datetime.utcnow().date().isoformat(),filing_branch='Northcliff')
        with patch('app.services.cover_eligibility.lock_member_approvals'),patch('app.services.cover_eligibility.coverage_report',return_value={'missing_ids':[],'blocked':False}),patch('app.services.cover_eligibility.coverage_errors',return_value=[]),patch('app.services.compliance_service.assert_application_rules',return_value=(True,[])),patch('app.routes.qa.document_summary',return_value={'complete':True}),patch('app.routes.qa.ensure_screened',return_value=(True,[])),patch('app.services.online_application.notify_activation',return_value=False),patch('app.services.branch_filing.send_email',return_value=True) as mail:
            response=self.client.post(f'/qa/application/{self.aid}',data=data)
            self.assertEqual(response.status_code,302);self.assertEqual(mail.call_count,1)
        self.assertEqual(ApplicationFilingDelivery.query.one().status,'Accepted by mail server')
    def test_interrupted_send_requires_explicit_resend(self):
        self.pack();db.session.add(ApplicationFilingDelivery(application_id=self.aid,branch_name='Northcliff',recipient_email='filing@example.com',status='Sending',attempted_at=datetime.utcnow()-timedelta(minutes=10)));db.session.commit()
        with patch('app.services.branch_filing.send_email',return_value=True) as mail:
            self.assertEqual(send_branch_pack(self.aid,self.uid).status,'Sending');mail.assert_not_called()
            self.assertEqual(send_branch_pack(self.aid,self.uid,resend=True).status,'Accepted by mail server');self.assertEqual(mail.call_count,1)

    def test_approval_requires_configured_branch(self):
        from app.routes.qa import QA_CHECKLIST
        a=db.session.get(ClientApplication,self.aid);a.status='FICA Outstanding';a.filing_branch=None;a.whatsapp_journey.activated_at=None;db.session.commit()
        data={key:'on' for key,label in QA_CHECKLIST};data['decision']='Compliance Approved'
        with patch('app.services.branch_filing.send_email') as mail:
            response=self.client.post(f'/qa/application/{self.aid}',data=data)
            self.assertEqual(response.status_code,302);mail.assert_not_called()
        self.assertEqual(db.session.get(ClientApplication,self.aid).status,'FICA Outstanding')
        self.assertEqual(ApplicationFilingDelivery.query.count(),0)
    def test_report_unattempted_filter_and_history_snapshot(self):
        page=self.client.get('/reports/branch-filing?status=Not+attempted');self.assertIn(b'TEST-FILING',page.data)
        self.pack()
        with patch('app.services.branch_filing.send_email',return_value=True):send_branch_pack(self.aid,self.uid)
        office=BranchFilingOffice.query.one();office.email='changed@example.com';db.session.commit()
        page=self.client.get('/reports/branch-filing?status=Accepted+by+mail+server');self.assertIn(b'filing@example.com',page.data);self.assertNotIn(b'changed@example.com',page.data)
        self.assertNotIn(b'TEST-FILING',self.client.get('/reports/branch-filing?status=Not+attempted').data)

    def test_default_seed_contact_edits_and_deletion_survive_restart(self):
        from app.services.branch_filing import seed_default_office, filing_branch
        seed_default_office()
        office=BranchFilingOffice.query.filter_by(is_default=True).one()
        self.assertEqual(office.email,'lowhann@martinsdirect.com');self.assertEqual(office.contact_first_name,'Lowhann')
        a=db.session.get(ClientApplication,self.aid);a.filing_branch=None;db.session.commit()
        self.assertEqual(filing_branch(a),"Martin's Brokers")
        self.post('/settings/branch-filing',{'office_id':office.id,'branch_name':"Martin's Brokers",'contact_first_name':'Updated','contact_surname':'Contact','email':'updated@example.com','is_default':'1'})
        seed_default_office();self.assertEqual(BranchFilingOffice.query.filter_by(is_default=True).one().contact_first_name,'Updated')
        db.session.delete(BranchFilingOffice.query.filter_by(is_default=True).one());db.session.commit();seed_default_office()
        self.assertIsNone(BranchFilingOffice.query.filter_by(is_default=True).first())

class BranchActivationIntegrationTests(unittest.TestCase):
    from test_online_application import OnlineApplicationTests as OnlineFixture
    tearDown=OnlineFixture.tearDown
    record=OnlineFixture.record
    prepare=OnlineFixture.prepare
    save=OnlineFixture.save
    valid_id=staticmethod(OnlineFixture.valid_id)
    upload_screening=OnlineFixture.upload_screening
    sign_documents=OnlineFixture.sign_documents
    def setUp(self):
        self.OnlineFixture.setUp(self)
        @self.app.before_request
        def clear_cached_user(): g.pop('_login_user',None)
        db.session.get(User,self.user_id).owner_approved_at=datetime.utcnow()
        self.record.filing_branch='Northcliff'
        db.session.add(BranchFilingOffice(branch_key='northcliff',branch_name='Northcliff',email='filing@example.com'));db.session.commit()
    def test_real_signed_pack_activation_and_branch_delivery(self):
        with patch('app.services.branch_filing.send_email',return_value=True) as branch_mail:
            self.OnlineFixture.test_full_whatsapp_questionnaire_signature_and_activation(self)
            self.assertEqual(branch_mail.call_count,1)
            self.assertEqual(branch_mail.call_args.args[0],'filing@example.com')
            self.assertEqual(len(branch_mail.call_args.args[3]),5)
        self.assertEqual(ApplicationFilingDelivery.query.one().status,'Accepted by mail server')
        from pypdf import PdfReader
        pdf=Path(application_folder(self.record))/f'welcome_pack_{self.record_id}.pdf'
        text=' '.join(page.extract_text() for page in PdfReader(pdf).pages)
        for phrase in ['Step 1','Step 2','Step 3','Step 4','Step 5','claim form','Main Member ID','beneficiary ID','front and back','DHA-1663']:
            self.assertIn(phrase,text)
        self.assertNotIn('BI-1663',text)

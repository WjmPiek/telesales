from datetime import datetime, timedelta
from unittest.mock import patch
from itsdangerous import URLSafeTimedSerializer
import test_user_management_owner as fixture
from app import db
from app.models import User, AuditLog, QRLoginToken

class OwnerAccessTests(fixture.OwnerUserManagementTests):
    def setUp(self):
        super().setUp()
        for uid in (self.admin_id, self.agent_id):
            user=db.session.get(User,uid); user.owner_approved_at=datetime.utcnow(); user.set_password('test-password-123')
        db.session.get(User,self.owner_id).set_password('test-password-123')
        db.session.commit()

    def post(self,path,data=None):
        if path.startswith('/auth/users/'):
            with self.client.session_transaction() as session:
                session['user_management_csrf']='test-csrf'
            data=dict(data or {},user_management_csrf='test-csrf')
        return super().post(path,data)

    def test_user_mutations_reject_missing_csrf(self):
        self.login(self.owner_id)
        self.get('/auth/users')
        response=super().post('/auth/users/create',{'name':'Forged'})
        self.assertEqual(response.status_code,400)

    def test_public_registration_is_closed(self):
        count=User.query.count()
        self.assertEqual(self.get('/auth/register').status_code,403)
        self.assertEqual(self.post('/auth/register',{'name':'Spam','email':'spam@example.test','password':'test-password-123','confirm_password':'test-password-123'}).status_code,403)
        self.assertEqual(User.query.count(),count)

    def test_only_owner_creates_users(self):
        self.login(self.admin_id)
        self.assertNotIn(b'Create new user',self.get('/auth/users').data)
        data={'name':'New agent','email':'new@example.test','password':'test-password-123','role_id':self.role_ids['Agent'],'branch':'Brokers'}
        self.assertEqual(self.post('/auth/users/create',data).status_code,403)
        self.login(self.owner_id)
        self.assertEqual(self.post('/auth/users/create',data).status_code,302)
        self.assertIsNotNone(User.query.filter_by(email='new@example.test').one().owner_approved_at)
        self.assertEqual(AuditLog.query.filter_by(action='USER_CREATED',user_id=self.owner_id).count(),1)

    def test_owner_approval_gates_password_and_existing_session(self):
        u=db.session.get(User,self.agent_id);u.owner_approved_at=None;db.session.commit()
        self.assertEqual(self.post('/auth/login',{'email':'agent@example.com','password':'test-password-123'}).status_code,200)
        self.login(self.agent_id);self.assertEqual(self.get('/recovery/callbacks').status_code,302)
        self.login(self.owner_id)
        self.post(f'/auth/users/{self.agent_id}/update',{'name':'Example Agent','branch':'Head Office','role_id':self.role_ids['Agent'],'active':'1'})
        self.assertIsNotNone(db.session.get(User,self.agent_id).owner_approved_at)
        self.assertEqual(AuditLog.query.filter_by(action='USER_APPROVED',user_id=self.owner_id).count(),1)
        self.get('/auth/logout')
        self.assertEqual(self.post('/auth/login',{'email':'agent@example.com','password':'test-password-123'}).status_code,302)

    def test_sso_cannot_create_activate_or_promote(self):
        def launch(email, admin=True):
            token=URLSafeTimedSerializer('test-launch-secret',salt='martins-insurance-launch-v1').dumps({'module':'insurance','email':email,'is_admin':admin,'name':'Changed','franchises':['Changed']})
            return self.get('/auth/launch?token='+token)
        with patch.dict('os.environ',{'INSURANCE_LAUNCH_SECRET':'test-launch-secret'}):
            self.assertEqual(launch('unknown@example.test').status_code,403)
            self.assertIsNone(User.query.filter_by(email='unknown@example.test').first())
            u=db.session.get(User,self.agent_id);u.active=False;db.session.commit()
            self.assertEqual(launch(u.email).status_code,403)
            u=db.session.get(User,self.agent_id);u.active=True;u.owner_approved_at=None;db.session.commit()
            self.assertEqual(launch(u.email).status_code,403)
            u=db.session.get(User,self.agent_id);u.owner_approved_at=datetime.utcnow();db.session.commit()
            self.assertEqual(launch(u.email).status_code,302)
            u=db.session.get(User,self.agent_id);self.assertEqual(u.role.name,'Agent');self.assertEqual(u.branch,'Head Office');self.assertEqual(u.name,'Example Agent')

    def test_qr_approval_and_status_require_owner_approval(self):
        u=db.session.get(User,self.agent_id);u.owner_approved_at=None;db.session.add(QRLoginToken(token='test-qr',status='pending',expires_at=datetime.utcnow()+timedelta(minutes=2),desktop_ip='127.0.0.1',desktop_user_agent='Werkzeug/3.0.3'));db.session.commit()
        self.post('/auth/qr/approve/test-qr',{'email':'agent@example.com','password':'test-password-123'})
        self.assertEqual(QRLoginToken.query.filter_by(token='test-qr').one().status,'pending')
        q=QRLoginToken.query.filter_by(token='test-qr').one();q.status='approved';q.approved_user_id=self.agent_id;db.session.commit()
        response=self.get('/auth/qr/status/test-qr');self.assertEqual(response.json['status'],'rejected')

    def test_owner_audit_search_export_and_navigation(self):
        db.session.add(AuditLog(user_id=self.owner_id,action='USER_CREATED',entity_type='User',entity_id='123',details='Target person target@example.test'));db.session.commit()
        self.login(self.owner_id)
        page=self.get('/security-center/audit?q=target@example.test');self.assertEqual(page.status_code,200);self.assertIn(b'Target person',page.data);self.assertIn(b'Admin Portal',page.data)
        self.assertIn(b'target@example.test',self.get('/security-center/audit?q=target@example.test&export=csv').data)
        self.assertEqual(self.get('/security-center/audit?start=bad').status_code,400)
        self.login(self.admin_id);self.assertEqual(self.get('/security-center/audit').status_code,403)

    def test_trusted_device_cannot_bypass_approval(self):
        from app.models import QRTrustedDevice
        from app.routes.auth import _hash_token, _trusted_device_from_cookie
        user=db.session.get(User,self.agent_id);user.owner_approved_at=None
        db.session.add(QRTrustedDevice(user_id=self.agent_id,device_token_hash=_hash_token('device-secret'),expires_at=datetime.utcnow()+timedelta(days=1),active=True))
        db.session.commit()
        with self.app.test_request_context(headers={'Cookie':'mf_qr_trusted_device=device-secret'}):
            self.assertIsNone(_trusted_device_from_cookie())

    def test_report_excludes_unapproved_accounts(self):
        user=db.session.get(User,self.agent_id);user.owner_approved_at=None;db.session.commit()
        self.login(self.owner_id)
        self.assertNotIn(b'Example Agent', self.get('/reports/').data)
        self.assertNotIn(b'Example Agent', self.get('/reports/export.csv').data)

    def test_public_registration_is_not_attributed_to_reassigned_owner(self):
        db.session.add(AuditLog(user_id=self.owner_id,action='USER_REGISTERED',details='User registration pending Admin role assignment for original@example.test / Test'))
        db.session.commit();self.login(self.owner_id)
        page=self.get('/security-center/audit?q=original@example.test')
        self.assertIn(b'Public registrant',page.data)
        self.assertIn(b'Public registrant',self.get('/security-center/audit?q=original@example.test&export=csv').data)

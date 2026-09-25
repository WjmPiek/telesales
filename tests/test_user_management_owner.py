import os
import unittest
from flask import g

os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["ENABLE_WHATSAPP_SCHEDULER"] = "0"
os.environ["AUTO_CREATE_TABLES"] = "1"

from app import create_app, db
from app.models import ApplicationJourney, AuditLog, ClientApplication, CommunicationCampaign, LapsedPolicy, Role, User
from app.services.brokers_branch import reconcile_brokers_branch


class OwnerUserManagementTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config.update(TESTING=True)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.drop_all()
        db.create_all()
        self.roles = {name: Role(name=name) for name in ("Super Admin", "Admin", "Branch Manager", "Agent")}
        db.session.add_all(self.roles.values())
        self.owner = User(name="Wjm Piek", email="wjm@martinsdirect.com", role=self.roles["Super Admin"],
                          branch="Brokers", active=True, password_hash="unused")
        self.admin = User(name="Lowhann Barkhuizen", email="lowhann@martinsdirect.com", role=self.roles["Admin"],
                          branch="Head Office", active=True, password_hash="unused")
        self.agent = User(name="Example Agent", email="agent@example.com", role=self.roles["Agent"],
                          branch="Head Office", active=True, password_hash="unused")
        db.session.add_all([self.owner, self.admin, self.agent])
        db.session.flush()
        self.role_ids = {name: role.id for name, role in self.roles.items()}
        self.owner_id, self.admin_id, self.agent_id = self.owner.id, self.admin.id, self.agent.id
        db.session.commit()
        self.client = self.app.test_client()

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()

    def login(self, user_id):
        g.pop("_login_user", None)
        with self.client.session_transaction() as session:
            session["_user_id"] = str(user_id)
            session["_fresh"] = True

    def get(self, path):
        g.pop("_login_user", None)
        return self.client.get(path)

    def post(self, path, data=None):
        g.pop("_login_user", None)
        return self.client.post(path, data=data)

    def test_owner_can_edit_admin_and_other_admin_cannot_edit_or_delete(self):
        self.login(self.owner_id)
        page = self.get("/auth/users")
        self.assertIn(f"/auth/users/{self.admin_id}/update".encode(), page.data)
        self.assertIn(f"/auth/users/{self.admin_id}/delete".encode(), page.data)
        changed = self.post(f"/auth/users/{self.admin_id}/update", data={
            "name": "Lowhann Barkhuizen", "branch": "Brokers", "role_id": self.role_ids["Admin"],
            "active": "1"})
        self.assertEqual(changed.status_code, 302)
        lowhann = db.session.get(User, self.admin_id)
        self.assertEqual((lowhann.branch, lowhann.role.name, lowhann.active), ("Brokers", "Admin", True))

        self.login(self.admin_id)
        page = self.get("/auth/users")
        self.assertNotIn(f"/auth/users/{self.agent_id}/update".encode(), page.data)
        self.assertNotIn(f"/auth/users/{self.agent_id}/delete".encode(), page.data)
        self.post(f"/auth/users/{self.agent_id}/update", data={
            "name": "Changed", "branch": "Brokers", "role_id": self.role_ids["Agent"], "active": "1"})
        self.post(f"/auth/users/{self.agent_id}/delete")
        self.assertEqual(db.session.get(User, self.agent_id).name, "Example Agent")

    def test_owner_role_branch_and_active_status_remain_protected(self):
        self.login(self.owner_id)
        self.post(f"/auth/users/{self.owner_id}/update", data={
            "name": "Wjm Piek", "branch": "Alberton", "role_id": self.role_ids["Agent"],
            "active": "0"})
        owner = db.session.get(User, self.owner_id)
        self.assertEqual((owner.role.name, owner.branch, owner.active), ("Super Admin", "Brokers", True))
        self.post(f"/auth/users/{self.owner_id}/delete")
        self.assertIsNotNone(db.session.get(User, self.owner_id))

    def test_only_owner_can_permanently_delete_admin_account(self):
        self.login(self.admin_id)
        self.post(f"/auth/users/{self.agent_id}/delete")
        self.assertIsNotNone(db.session.get(User, self.agent_id))

        self.login(self.owner_id)
        response = self.post(f"/auth/users/{self.admin_id}/delete")
        self.assertEqual(response.status_code, 302)
        self.assertIsNone(db.session.get(User, self.admin_id))
        self.assertIsNotNone(db.session.get(User, self.owner_id))
        self.assertEqual(AuditLog.query.filter_by(action="USER_PERMANENTLY_DELETED").count(), 1)

    def test_suspended_user_cannot_keep_using_an_existing_session(self):
        self.login(self.agent_id)
        self.assertEqual(self.get("/dashboard").status_code, 200)
        db.session.get(User, self.agent_id).active = False
        db.session.commit()
        response = self.get("/dashboard")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/auth/login", response.location)

    def test_reported_qr_branch_and_campaign_are_corrected_once(self):
        owner = db.session.get(User, self.owner_id)
        owner.branch = "Alberton"
        campaign = CommunicationCampaign(name="QR Test", message_body="Test", branch="Alberton",
                                         created_by_id=self.owner_id)
        lead = LapsedPolicy(member_id="QR-TEST", surname="Client", branch="Alberton")
        db.session.add_all([campaign, lead])
        db.session.flush()
        application = ClientApplication(application_ref="QR-20260924-82C9E8", agent_id=self.owner_id,
                                        application_type="New Policy - QR Campaign", branch="Alberton",
                                        lapsed_policy_id=lead.id)
        db.session.add(application)
        db.session.flush()
        db.session.add(ApplicationJourney(application_id=application.id, campaign_id=campaign.id))
        app_id, campaign_id, lead_id = application.id, campaign.id, lead.id
        db.session.commit()

        self.assertEqual(reconcile_brokers_branch(), {"owner": True, "application": True, "campaign": True})
        self.assertEqual(db.session.get(User, self.owner_id).branch, "Brokers")
        self.assertEqual(db.session.get(ClientApplication, app_id).branch, "Brokers")
        self.assertEqual(db.session.get(LapsedPolicy, lead_id).branch, "Brokers")
        self.assertEqual(db.session.get(CommunicationCampaign, campaign_id).branch, "Brokers")
        audit_count = AuditLog.query.count()
        self.assertEqual(reconcile_brokers_branch(), {"owner": False, "application": False, "campaign": False})
        self.assertEqual(AuditLog.query.count(), audit_count)


if __name__ == "__main__":
    unittest.main()

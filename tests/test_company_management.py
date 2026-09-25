import os
import unittest
from io import BytesIO
from flask import g
from openpyxl import Workbook

os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["ENABLE_WHATSAPP_SCHEDULER"] = "0"
os.environ["AUTO_CREATE_TABLES"] = "1"

from app import create_app, db
from app.models import AuditLog, CampaignRecipient, ClientApplication, CommunicationCampaign, CompanyGroupState, LapsedPolicy, RecoveryCallLog, Role, User
from app.services.company_groups import company_is_suspended, get_or_create_company
from app.routes.communications import _send_to_recipient


class CompanyManagementTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config.update(TESTING=True)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.drop_all()
        db.create_all()
        super_role = Role(name="Super Admin")
        admin_role = Role(name="Admin")
        agent_role = Role(name="Agent")
        owner = User(name="Wjm Piek", email="wjm@martinsdirect.com", branch="Brokers",
                     role=super_role, active=True, password_hash="unused")
        admin = User(name="Other Admin", email="admin@example.com", branch="Alberton",
                     role=admin_role, active=True, password_hash="unused")
        agent = User(name="Company Agent", email="agent@example.com", branch="Brokers",
                     role=agent_role, active=True, password_hash="unused")
        db.session.add_all([owner, admin, agent,
                            LapsedPolicy(company_name="Alberton", franchise="Alberton", branch="Brokers", policy_number="B1"),
                            LapsedPolicy(company_name="Alberton", franchise="Alberton", branch="Alberton", policy_number="A1")])
        db.session.commit()
        self.owner_id, self.admin_id, self.agent_id = owner.id, admin.id, agent.id
        self.client = self.app.test_client()

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()

    def login(self, user_id):
        g.pop("_login_user", None)
        with self.client.session_transaction() as session:
            session["_user_id"] = str(user_id)
            session["_fresh"] = True

    def post(self, path, data):
        g.pop("_login_user", None)
        return self.client.post(path, data=data)

    def get(self, path):
        g.pop("_login_user", None)
        return self.client.get(path)

    def test_distinct_branches_and_owner_only_mutation(self):
        self.login(self.owner_id)
        page = self.client.get("/franchise/details")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"All branches", page.data)
        self.assertEqual(page.data.count(b">Alberton</td>"), 3)  # two names, one branch
        response = self.post("/franchise/details/company/manage", data={
            "name": "Alberton", "branch": "Brokers", "new_name": "Brokers Company", "action": "rename"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(LapsedPolicy.query.filter_by(branch="Brokers").one().company_name, "Brokers Company")
        self.assertEqual(LapsedPolicy.query.filter_by(branch="Alberton").one().company_name, "Alberton")
        self.assertEqual(AuditLog.query.filter_by(action="COMPANY_RENAMED").count(), 1)

        self.login(self.admin_id)
        denied = self.post("/franchise/details/company/manage", data={
            "name": "Alberton", "branch": "Alberton", "action": "delete"})
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(LapsedPolicy.query.count(), 2)

    def test_suspend_and_delete_listing_retain_policies(self):
        self.login(self.owner_id)
        for action, expected in (("suspend", "Suspended"), ("activate", "Active"),
                                 ("delete", "Deleted"), ("restore", "Active")):
            response = self.post("/franchise/details/company/manage", data={
                "name": "Alberton", "branch": "Brokers", "action": action})
            self.assertEqual(response.status_code, 302)
            self.assertEqual(CompanyGroupState.query.filter_by(company_name="Alberton", branch="Brokers").one().status, expected)
            self.assertEqual(LapsedPolicy.query.count(), 2)
            self.assertEqual(company_is_suspended(LapsedPolicy.query.filter_by(branch="Brokers").one()), expected != "Active")
            self.assertFalse(company_is_suspended(LapsedPolicy.query.filter_by(branch="Alberton").one()))

    def test_agent_assignment_and_campaign_recipient_scope(self):
        self.login(self.owner_id)
        company = get_or_create_company("Alberton", "Brokers")
        other = get_or_create_company("Alberton", "Alberton")
        db.session.commit()
        self.assertNotEqual(company.id, other.id)
        company_id = company.id
        other_id = other.id
        self.post("/franchise/details/company/agents", data={
            "name": "Alberton", "branch": "Brokers", "agent_ids": str(self.agent_id)})
        self.assertEqual([u.id for u in db.session.get(CompanyGroupState, company_id).agents], [self.agent_id])
        campaign = CommunicationCampaign(name="Company campaign", message_body="Test", branch="Brokers",
                                         company_id=company_id, created_by_id=self.agent_id)
        foreign_campaign = CommunicationCampaign(name="Other company campaign", message_body="Test", branch="Alberton",
                                                 company_id=other_id, created_by_id=self.owner_id)
        db.session.add_all([campaign, foreign_campaign])
        db.session.commit()
        campaign_id = campaign.id
        foreign_campaign_id = foreign_campaign.id
        policy_ids = [policy.id for policy in LapsedPolicy.query.order_by(LapsedPolicy.id).all()]

        self.login(self.agent_id)
        self.assertEqual(self.get("/communications/templates").status_code, 200)
        self.assertEqual(self.get("/communications/new").status_code, 302)
        self.assertEqual(self.get(f"/communications/{campaign_id}").status_code, 200)
        self.assertEqual(self.get(f"/communications/{foreign_campaign_id}").status_code, 403)
        self.assertEqual(self.post(f"/communications/{foreign_campaign_id}/send", data={}).status_code, 403)
        self.post(f"/communications/{campaign_id}/add-recipients", data={"policy_ids": [str(item) for item in policy_ids]})
        recipients = CampaignRecipient.query.filter_by(campaign_id=campaign_id).all()
        self.assertEqual(len(recipients), 1)
        self.assertEqual(recipients[0].policy.branch, "Brokers")
        foreign_recipient = CampaignRecipient(campaign_id=campaign_id, lapsed_policy_id=policy_ids[1], secure_token="foreign-token")
        db.session.add(foreign_recipient)
        db.session.flush()
        self.assertEqual(_send_to_recipient(db.session.get(CommunicationCampaign, campaign_id), foreign_recipient, "whatsapp"),
                         (False, "Recipient does not belong to this company"))
        db.session.delete(foreign_recipient)
        db.session.add_all([
            RecoveryCallLog(lapsed_policy_id=policy_ids[0], agent_id=self.agent_id, outcome="Called"),
            RecoveryCallLog(lapsed_policy_id=policy_ids[1], agent_id=self.agent_id, outcome="Called"),
            ClientApplication(application_ref="COMPANY-B", branch="Brokers", lapsed_policy_id=policy_ids[0], agent_id=self.agent_id),
            ClientApplication(application_ref="COMPANY-A", branch="Alberton", lapsed_policy_id=policy_ids[1], agent_id=self.agent_id),
        ])
        db.session.commit()

        self.login(self.owner_id)
        report = self.get(f"/reports/?company_id={company_id}")
        self.assertEqual(report.status_code, 200)
        self.assertIn(b"Campaigns:</strong> 1", report.data)
        self.assertIn(b'<div class="text-muted">Calls</div><h3>1</h3>', report.data)
        self.assertIn(b'<div class="text-muted">Applications</div><h3>1</h3>', report.data)

    def test_import_registers_company_under_brokers(self):
        workbook = Workbook()
        sheet = workbook.active
        sheet.append(["Policy_Number", "Company", "Branch", "ID_Number", "Cell_Number", "Surname"])
        sheet.append(["IMPORTED-1", "New Business Client", "Brokers", "8001015009087", "0821234567", "Example"])
        output = BytesIO()
        workbook.save(output)
        output.seek(0)
        self.login(self.owner_id)
        g.pop("_login_user", None)
        response = self.client.post("/recovery/import", data={"file": (output, "clients.xlsx")},
                                    content_type="multipart/form-data")
        self.assertEqual(response.status_code, 302)
        company = CompanyGroupState.query.filter_by(company_name="New Business Client", branch="Brokers").one()
        self.assertEqual(company.parent_company, "Martin's Brokers")
        policy = LapsedPolicy.query.filter_by(policy_number="IMPORTED-1").one()
        self.assertEqual((policy.company_name, policy.company_id), (company.company_name, company.id))


if __name__ == "__main__":
    unittest.main()

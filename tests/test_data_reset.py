import os
import tempfile
import unittest

os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["ENABLE_WHATSAPP_SCHEDULER"] = "0"
os.environ["AUTO_CREATE_TABLES"] = "1"

from app import create_app, db
from app.models import (
    ApplicationJourney,
    ApplicationMarketingConsent,
    CampaignRecipient,
    ClientApplication,
    CommunicationCampaign,
    ContactCommunicationPreference,
    ContactSuppression,
    LapsedPolicy,
    PolicyProduct,
    Role,
    SystemSetting,
    User,
    WhatsAppContact,
    WhatsAppConversation,
    WhatsAppMessage,
    WhatsAppTemplate,
)
from app.services.data_reset import reset_operational_data


class DataResetTests(unittest.TestCase):
    def setUp(self):
        self.uploads = tempfile.TemporaryDirectory()
        self.app = create_app()
        self.app.config.update(TESTING=True, UPLOAD_FOLDER=self.uploads.name)
        self.context = self.app.app_context()
        self.context.push()
        db.drop_all()
        db.create_all()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.context.pop()
        self.uploads.cleanup()

    def test_reset_deletes_activity_and_preserves_configuration_and_opt_outs(self):
        role = Role(name="Super Admin")
        user = User(name="Owner", email="owner@example.test", role=role, active=True)
        user.set_password("safe-test-password")
        product = PolicyProduct(product_name="Family", plan_name="Gold", active=True)
        setting = SystemSetting(category="Email", key="sender", value="sales@example.test")
        db.session.add_all([role, user, product, setting])
        db.session.flush()

        policy = LapsedPolicy(
            policy_number="OLD-1", cell_number="0821234567",
            email_address="client@example.test", recovery_status="Imported")
        db.session.add(policy)
        db.session.flush()
        db.session.add(ContactCommunicationPreference(
            lapsed_policy_id=policy.id, opted_out_all=True,
            telephone_allowed=False, whatsapp_allowed=False, email_allowed=False))

        campaign = CommunicationCampaign(
            name="Approved template", message_body="Hello {{1}}",
            created_by_id=user.id, template_status="Approved")
        db.session.add(campaign)
        db.session.flush()
        template = WhatsAppTemplate(
            campaign_id=campaign.id, name="approved_template", language="en",
            body_text="Hello {{1}}", status="Approved", created_by_id=user.id)
        recipient = CampaignRecipient(
            campaign_id=campaign.id, lapsed_policy_id=policy.id, secure_token="secure-token")
        db.session.add_all([template, recipient])

        application = ClientApplication(
            application_ref="APP-RESET", product_id=product.id,
            lapsed_policy_id=policy.id, cell_number="0821234567",
            email="client@example.test")
        db.session.add(application)
        db.session.flush()
        db.session.add_all([
            ApplicationJourney(application_id=application.id, campaign_id=campaign.id),
            ApplicationMarketingConsent(application_id=application.id, allowed=False),
        ])

        contact = WhatsAppContact(
            wa_id="27821234567", phone_number="27821234567", opted_out=True)
        db.session.add(contact)
        db.session.flush()
        conversation = WhatsAppConversation(contact_id=contact.id)
        db.session.add(conversation)
        db.session.flush()
        db.session.add(WhatsAppMessage(
            conversation_id=conversation.id, direction="outbound", body="test"))
        db.session.commit()

        counts = reset_operational_data()

        self.assertEqual(counts["applications"], 1)
        self.assertEqual(ClientApplication.query.count(), 0)
        self.assertEqual(LapsedPolicy.query.count(), 0)
        self.assertEqual(CampaignRecipient.query.count(), 0)
        self.assertEqual(WhatsAppMessage.query.count(), 0)
        self.assertEqual(WhatsAppConversation.query.count(), 0)
        self.assertEqual(WhatsAppContact.query.count(), 0)
        self.assertEqual(PolicyProduct.query.count(), 1)
        self.assertEqual(User.query.count(), 1)
        self.assertEqual(SystemSetting.query.count(), 1)
        self.assertEqual(CommunicationCampaign.query.count(), 1)
        self.assertEqual(WhatsAppTemplate.query.count(), 1)
        self.assertGreaterEqual(ContactSuppression.query.count(), 1)
        self.assertTrue(all(
            row.campaign_id is None and row.lapsed_policy_id is None
            for row in ContactSuppression.query.all()))


if __name__ == "__main__":
    unittest.main()

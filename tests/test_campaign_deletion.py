import os
import unittest

os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["AUTO_CREATE_TABLES"] = "1"
os.environ["ENABLE_WHATSAPP_SCHEDULER"] = "0"

from app import create_app, db
from app.models import (
    CommunicationCampaign,
    Role,
    User,
    WhatsAppMediaAsset,
    WhatsAppMediaVersion,
    WhatsAppProviderLog,
    WhatsAppTemplate,
)


class CampaignDeletionTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config.update(TESTING=True)
        self.client = self.app.test_client()
        self.context = self.app.app_context()
        self.context.push()
        db.drop_all()
        db.create_all()

        role = Role(name="Super Admin")
        user = User(name="Admin", email="admin@example.test", password_hash="test", role=role)
        db.session.add_all([role, user])
        db.session.flush()
        self.user_id = user.id

        campaign = CommunicationCampaign(
            name="Template: obsolete",
            subject="WhatsApp template",
            message_body="Test",
            whatsapp_template_name="obsolete",
            created_by_id=user.id,
        )
        db.session.add(campaign)
        db.session.flush()
        self.campaign_id = campaign.id

        template = WhatsAppTemplate(
            campaign_id=campaign.id,
            name="obsolete",
            language="en",
            body_text="Test",
            created_by_id=user.id,
        )
        asset = WhatsAppMediaAsset(campaign_id=campaign.id, filename="test.png", created_by_id=user.id)
        log = WhatsAppProviderLog(
            operation="template_status",
            campaign_id=campaign.id,
            provider="meta",
            status="failed",
        )
        db.session.add_all([template, asset, log])
        db.session.flush()
        db.session.add(WhatsAppMediaVersion(media_asset_id=asset.id, version_number=1, created_by_id=user.id))
        db.session.commit()

        with self.client.session_transaction() as session:
            session["_user_id"] = str(self.user_id)
            session["_fresh"] = True

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.context.pop()

    def test_delete_campaign_removes_provider_logs_and_media_versions(self):
        response = self.client.post(
            f"/communications/{self.campaign_id}/delete",
            data={"confirm_name": "Template: obsolete"},
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(CommunicationCampaign.query.count(), 0)
        self.assertEqual(WhatsAppTemplate.query.count(), 0)
        self.assertEqual(WhatsAppMediaAsset.query.count(), 0)
        self.assertEqual(WhatsAppMediaVersion.query.count(), 0)
        self.assertEqual(WhatsAppProviderLog.query.count(), 0)


if __name__ == "__main__":
    unittest.main()

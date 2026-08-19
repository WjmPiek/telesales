import hashlib
import hmac
import json
import os
import unittest
from unittest.mock import patch

os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["AUTO_CREATE_TABLES"] = "1"
os.environ["ENABLE_WHATSAPP_SCHEDULER"] = "0"
os.environ["WHATSAPP_VERIFY_TOKEN"] = "test-verify-token"

from app import create_app, db
from app.models import (
    CampaignRecipient,
    CommunicationCampaign,
    ContactCommunicationPreference,
    ContactSuppression,
    LapsedPolicy,
    Role,
    User,
    WhatsAppContact,
    WhatsAppConversation,
    WhatsAppMessage,
    WhatsAppWebhookEvent,
)
from app.services.whatsapp_service import SendResult, normalize_phone


class WhatsAppProcessTests(unittest.TestCase):
    def setUp(self):
        os.environ.pop("META_APP_SECRET", None)
        self.app = create_app()
        self.app.config.update(TESTING=True)
        self.client = self.app.test_client()
        self.context = self.app.app_context()
        self.context.push()
        db.drop_all()
        db.create_all()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.context.pop()

    def _post(self, payload, headers=None):
        return self.client.post("/whatsapp/webhook", json=payload, headers=headers or {})

    def _campaign_recipient(self, phone="0676200748"):
        role = Role(name="Agent")
        user = User(name="Agent One", email="agent@example.com", password_hash="test", role=role)
        db.session.add_all([role, user])
        db.session.flush()
        policy = LapsedPolicy(member_id="M-1", initials="Test", surname="Client", cell_number=phone, assigned_agent_id=user.id)
        campaign = CommunicationCampaign(name="Test", message_body="Hello", created_by=user, send_whatsapp=True, send_email=False)
        recipient = CampaignRecipient(campaign=campaign, policy=policy, secure_token="recipient-token")
        db.session.add_all([policy, campaign, recipient])
        db.session.commit()
        return recipient

    def test_verification_and_health(self):
        self.assertEqual(self.client.get("/whatsapp/webhook").status_code, 200)
        response = self.client.get(
            "/whatsapp/webhook?hub.verify_token=test-verify-token&hub.challenge=12345"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_data(as_text=True), "12345")
        self.assertEqual(
            self.client.get("/whatsapp/webhook?hub.verify_token=wrong&hub.challenge=12345").status_code,
            403,
        )

    def test_phone_normalization_accepts_local_and_international_forms(self):
        self.assertEqual(normalize_phone("067 620 0748"), "27676200748")
        self.assertEqual(normalize_phone("+27 67 620 0748"), "27676200748")
        self.assertEqual(normalize_phone("0027 67 620 0748"), "27676200748")

    def test_waiting_conversation_is_reused_for_inbound_message(self):
        contact = WhatsAppContact(wa_id="27676200748", phone_number="27676200748", display_name="Test")
        db.session.add(contact)
        db.session.flush()
        db.session.add(WhatsAppConversation(contact_id=contact.id, status="Waiting"))
        db.session.commit()
        payload = {
            "object": "whatsapp_business_account",
            "entry": [{"changes": [{"value": {"messages": [{
                "id": "wamid.inbound-1", "from": "27676200748", "type": "text",
                "timestamp": "1787120000", "text": {"body": "Hello"},
            }]}}]}],
        }
        self.assertEqual(self._post(payload).status_code, 200)
        self.assertEqual(WhatsAppConversation.query.count(), 1)
        self.assertEqual(WhatsAppMessage.query.one().body, "Hello")

    def test_delivery_receipt_updates_campaign_recipient(self):
        recipient = self._campaign_recipient()
        contact = WhatsAppContact(wa_id="27676200748", phone_number="27676200748")
        db.session.add(contact)
        db.session.flush()
        conversation = WhatsAppConversation(contact_id=contact.id)
        db.session.add(conversation)
        db.session.flush()
        message = WhatsAppMessage(
            conversation_id=conversation.id,
            campaign_recipient_id=recipient.id,
            provider_message_id="wamid.outbound-1",
            direction="outbound",
            body="Hello",
            status="sent",
        )
        db.session.add(message)
        db.session.commit()
        message_id = message.id
        recipient_id = recipient.id
        payload = {
            "object": "whatsapp_business_account",
            "entry": [{"changes": [{"value": {"statuses": [{
                "id": "wamid.outbound-1", "status": "read", "timestamp": "1787120001"
            }]}}]}],
        }
        self.assertEqual(self._post(payload).status_code, 200)
        self.assertEqual(db.session.get(WhatsAppMessage, message_id).status, "read")
        self.assertEqual(db.session.get(CampaignRecipient, recipient_id).whatsapp_status, "Read")

    def test_background_campaign_send_records_inbox_message(self):
        recipient = self._campaign_recipient()
        campaign_id = recipient.campaign_id
        recipient_id = recipient.id
        from app.routes.communications import _send_to_recipient
        with patch(
            "app.routes.communications.send_whatsapp_text",
            return_value=SendResult(True, message_id="wamid.campaign-1", response_json={"messages": [{"id": "wamid.campaign-1"}]}),
        ):
            ok, error = _send_to_recipient(
                db.session.get(CommunicationCampaign, campaign_id),
                db.session.get(CampaignRecipient, recipient_id),
                "whatsapp",
            )
        db.session.commit()
        self.assertTrue(ok)
        self.assertIsNone(error)
        message = WhatsAppMessage.query.filter_by(provider_message_id="wamid.campaign-1").one()
        self.assertEqual(message.campaign_recipient_id, recipient_id)
        self.assertEqual(message.direction, "outbound")

    def test_stop_keyword_suppresses_campaign_contact(self):
        recipient = self._campaign_recipient()
        policy_id = recipient.lapsed_policy_id
        payload = {
            "object": "whatsapp_business_account",
            "entry": [{"changes": [{"value": {"messages": [{
                "id": "wamid.stop-1", "from": "27676200748", "type": "text",
                "text": {"body": "STOP"},
            }]}}]}],
        }
        self.assertEqual(self._post(payload).status_code, 200)
        pref = ContactCommunicationPreference.query.filter_by(lapsed_policy_id=policy_id).one()
        self.assertTrue(pref.opted_out_all)
        self.assertEqual(ContactSuppression.query.count(), 1)
        self.assertTrue(WhatsAppContact.query.one().opted_out)

    def test_failed_event_is_retryable(self):
        payload = {"object": "whatsapp_business_account", "entry": []}
        with patch("app.routes.whatsapp._process_payload", side_effect=RuntimeError("temporary")):
            self.assertEqual(self._post(payload).status_code, 500)
        event = WhatsAppWebhookEvent.query.one()
        self.assertFalse(event.processed)
        self.assertEqual(self._post(payload).status_code, 200)
        self.assertTrue(WhatsAppWebhookEvent.query.one().processed)
        self.assertEqual(WhatsAppWebhookEvent.query.count(), 1)

    def test_signature_is_required_when_app_secret_is_set(self):
        os.environ["META_APP_SECRET"] = "test-secret"
        payload = {"object": "whatsapp_business_account", "entry": []}
        raw = json.dumps(payload, separators=(",", ":")).encode()
        self.assertEqual(
            self.client.post("/whatsapp/webhook", data=raw, content_type="application/json").status_code,
            403,
        )
        signature = "sha256=" + hmac.new(b"test-secret", raw, hashlib.sha256).hexdigest()
        response = self.client.post(
            "/whatsapp/webhook",
            data=raw,
            content_type="application/json",
            headers={"X-Hub-Signature-256": signature},
        )
        self.assertEqual(response.status_code, 200)


if __name__ == "__main__":
    unittest.main()

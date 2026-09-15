import hashlib
import hmac
import json
import os
import unittest
from unittest.mock import patch, Mock

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
        os.environ["META_APP_SECRET"] = "test-secret"
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
        raw = json.dumps(payload).encode()
        signature = "sha256=" + hmac.new(b"test-secret", raw, hashlib.sha256).hexdigest()
        return self.client.post("/whatsapp/webhook", data=raw, content_type="application/json", headers=headers or {"X-Hub-Signature-256": signature})

    def test_missing_secret_fails_closed(self):
        os.environ.pop("META_APP_SECRET")
        self.assertEqual(self._post({"entry": []}).status_code, 503)

    def test_360dialog_authentication_and_channel_guard(self):
        payload = {"entry": [{"changes": [{"field": "messages", "value": {
            "metadata": {"phone_number_id": "live-phone"},
            "messages": [{"id": "d360-inbound", "from": "27821234567", "type": "text", "text": {"body": "Hi"}}]
        }}]}]}
        with patch.dict(os.environ, {"WHATSAPP_PROVIDER": "360dialog", "D360_WEBHOOK_SECRET": "test-webhook", "D360_PHONE_NUMBER_ID": "live-phone", "META_PHONE_NUMBER_ID": "old-test-phone"}):
            self.assertEqual(self._post(payload).status_code, 403)
            self.assertEqual(self._post(payload, {"Authorization": "Bearer wrong"}).status_code, 403)
            self.assertEqual(WhatsAppContact.query.count(), 0)
            self.assertEqual(self._post(payload, {"Authorization": "Bearer test-webhook"}).status_code, 200)
            self.assertEqual(WhatsAppMessage.query.count(), 1)
            self.assertEqual(self._post(payload, {"Authorization": "Bearer test-webhook"}).status_code, 200)
            self.assertEqual(WhatsAppMessage.query.count(), 1)
            payload["entry"][0]["changes"][0]["value"]["metadata"]["phone_number_id"] = "other-phone"
            payload["entry"][0]["changes"][0]["value"]["messages"][0]["id"] = "wrong-channel"
            self.assertEqual(self._post(payload, {"Authorization": "Bearer test-webhook"}).status_code, 200)
            self.assertEqual(WhatsAppMessage.query.count(), 1)

    def test_360dialog_missing_configuration_fails_closed(self):
        with patch.dict(os.environ, {"WHATSAPP_PROVIDER": "360dialog", "D360_WEBHOOK_SECRET": "", "D360_PHONE_NUMBER_ID": ""}):
            self.assertEqual(self._post({"entry": []}).status_code, 503)

    def test_coexistence_events_do_not_execute_customer_actions(self):
        self._campaign_recipient()
        for field in ["history", "smb_message_echoes", "smb_app_state_sync"]:
            payload = {"entry": [{"changes": [{"field": field, "value": {"messages": [
                {"id": field, "from": "27676200748", "type": "text", "text": {"body": "STOP"}}
            ]}}]}]}
            self.assertEqual(self._post(payload).status_code, 200)
        self.assertEqual(ContactSuppression.query.count(), 0)
        self.assertEqual(WhatsAppMessage.query.count(), 0)
        self.assertEqual(CampaignRecipient.query.one().policy.cell_number, "0676200748")

    def test_template_creation_uploads_image_and_uses_handle(self):
        from app.services.whatsapp_service import create_whatsapp_image_template
        responses = []
        for data in [{"id": "upload:test"}, {"h": "meta-image-handle"}, {"id": "template-1", "status": "PENDING"}]:
            response = Mock(status_code=200, content=b"json")
            response.json.return_value = data
            responses.append(response)
        with patch.dict(os.environ, {"META_APP_ID": "app-1", "META_ACCESS_TOKEN": "test-token", "META_WABA_ID": "waba-1", "WHATSAPP_PROVIDER": "meta"}), patch("requests.post", side_effect=responses) as post:
            result = create_whatsapp_image_template("callback_request", "en_US", "Hello {{1}}, may we call you?", "https://example.com/image.png", image_data=b"image", image_mimetype="image/png")
        self.assertTrue(result.ok)
        payload = post.call_args_list[2].kwargs["json"]
        self.assertEqual(payload["language"], "en_US")
        self.assertEqual(payload["components"][0]["example"]["header_handle"], ["meta-image-handle"])
        self.assertEqual([b["text"] for b in payload["components"][-1]["buttons"]], ["Call me back", "Delete my number"])

    def test_wrong_configured_phone_is_ignored(self):
        with patch.dict(os.environ, {"META_PHONE_NUMBER_ID": "expected"}):
            self.assertEqual(self._post({"messages": [{"id": "other-phone", "from": "27676200748", "type": "text", "text": {"body": "Hi"}}]}).status_code, 200)
        self.assertEqual(WhatsAppContact.query.count(), 0)

    def test_bulk_without_template_is_blocked(self):
        recipient = self._campaign_recipient()
        from app.routes.communications import _send_to_recipient
        with patch("requests.post") as post:
            ok, error = _send_to_recipient(recipient.campaign, recipient, "whatsapp")
        self.assertFalse(ok)
        post.assert_not_called()

    def test_scheduled_sender_saves_each_recipient(self):
        recipient = self._campaign_recipient()
        recipient.campaign.status = "Scheduled"
        recipient.campaign.scheduled_at = __import__('datetime').datetime.utcnow()
        recipient.campaign.queue_status = "queued"
        recipient.campaign.template_status = "Approved"
        db.session.commit()
        from app.services.whatsapp_campaign_engine import process_scheduled_campaigns
        def send(campaign, target, channel):
            target.whatsapp_status = "Sent"
            return True, None
        with patch("app.routes.communications._refresh_template_status", return_value=Mock(ok=True)), patch("app.routes.communications._send_to_recipient", side_effect=send) as sender:
            self.assertEqual(process_scheduled_campaigns()["sent"], 1)
            self.assertEqual(process_scheduled_campaigns()["processed"], 0)
            self.assertEqual(sender.call_count, 1)

    def test_button_sender_must_own_recipient(self):
        recipient = self._campaign_recipient()
        self._post({"messages": [{"id": "wrong-sender", "from": "27821234567", "type": "button", "button": {"payload": "optout:recipient-token", "text": "Delete my number"}}]})
        self.assertEqual(CampaignRecipient.query.one().policy.cell_number, "0676200748")
        self.assertEqual(ContactSuppression.query.count(), 0)

    def test_delete_button_clears_duplicates_and_blocks_recreation(self):
        recipient = self._campaign_recipient()
        duplicate = LapsedPolicy(member_id="M-2", cell_number="+27 67 620 0748", home_tel="0676200748")
        db.session.add(duplicate)
        db.session.commit()
        payload = {"messages": [{"id": "delete-button", "from": "27676200748", "type": "button", "button": {"payload": "optout:recipient-token", "text": "Delete my number"}}]}
        self.assertEqual(self._post(payload).status_code, 200)
        self.assertTrue(all(p.cell_number is None for p in LapsedPolicy.query.all()))
        self.assertTrue(all(p.home_tel is None for p in LapsedPolicy.query.all()))
        self.assertEqual(WhatsAppContact.query.one().phone_number, "")
        self.assertEqual(self._post(payload).status_code, 200)
        self.assertEqual(self._post({"messages": [{"id": "after-delete", "from": "27676200748", "type": "text", "text": {"body": "Hi"}}]}).status_code, 200)
        self.assertEqual(WhatsAppContact.query.count(), 1)
        self.assertTrue(all(e.payload == "{}" for e in WhatsAppWebhookEvent.query.all()))
        from app.services.whatsapp_service import send_whatsapp_text
        with patch("requests.post") as provider:
            self.assertFalse(send_whatsapp_text("0676200748", "Hello").ok)
            provider.assert_not_called()

    def test_callback_button_is_idempotent(self):
        recipient = self._campaign_recipient()
        for message_id in ["callback-one", "callback-two"]:
            self.assertEqual(self._post({"messages": [{"id": message_id, "from": "27676200748", "type": "button", "button": {"payload": "callback:recipient-token", "text": "Call me back"}}]}).status_code, 200)
        self.assertTrue(CampaignRecipient.query.one().callback_created)
        self.assertEqual(CampaignRecipient.query.one().policy.recovery_status, "Callback")
        from app.models import AgentNotification
        self.assertEqual(AgentNotification.query.count(), 1)

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
        recipient.campaign.whatsapp_template_name = "callback_request"
        recipient.campaign.image_url = "https://example.com/image.png"
        db.session.commit()
        campaign_id = recipient.campaign_id
        recipient_id = recipient.id
        from app.routes.communications import _send_to_recipient
        with patch(
            "app.routes.communications.send_whatsapp_template_image",
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

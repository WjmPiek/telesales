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
    ClientApplication,
    CommunicationCampaign,
    ContactCommunicationPreference,
    ContactSuppression,
    LapsedPolicy,
    PolicyProduct,
    PolicyProductRule,
    Role,
    User,
    WhatsAppContact,
    WhatsAppConversation,
    WhatsAppMessage,
    WhatsAppTemplate,
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

    def test_reused_approved_template_keeps_header_image(self):
        recipient = self._campaign_recipient()
        source = recipient.campaign
        source.created_by.role.name = "Super Admin"
        source.image_filename = "campaign.jpg"
        source.image_data = b"test-image"
        source.image_mimetype = "image/jpeg"
        source.image_url = f"https://example.test/communications/media/{source.id}/campaign.jpg"
        template = WhatsAppTemplate(campaign_id=source.id, name="approved_test", language="en",
                                    body_text="Hello {{1}}", status="Approved", created_by_id=source.created_by_id)
        db.session.add(template)
        db.session.commit()
        owner_id, template_id = source.created_by_id, template.id
        with self.client.session_transaction() as session:
            session["_user_id"] = str(owner_id)
            session["_fresh"] = True
        response = self.client.post(f"/communications/new?template_id={template_id}", data={
            "name": "Single-recipient image test", "audience_type": "group", "message_body": "Hello {{1}}",
        })
        self.assertEqual(response.status_code, 302)
        reused = CommunicationCampaign.query.filter_by(name="Single-recipient image test").one()
        self.assertEqual(reused.image_data, b"test-image")
        self.assertEqual(reused.image_mimetype, "image/jpeg")
        self.assertIn(f"/communications/{reused.id}/image", reused.image_url)

    def test_scheduled_sender_saves_each_recipient(self):
        recipient = self._campaign_recipient()
        recipient.campaign.send_email = True  # Legacy campaigns must never email.
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
            self.assertEqual(sender.call_args.args[2], "whatsapp")

    def test_campaign_email_delivery_is_blocked(self):
        recipient = self._campaign_recipient()
        from app.routes.communications import _send_to_recipient
        with patch("app.services.email_service.send_email") as email:
            ok, error = _send_to_recipient(recipient.campaign, recipient, "email")
        self.assertFalse(ok)
        self.assertIn("WhatsApp only", error)
        email.assert_not_called()

    def test_follow_up_forces_whatsapp_and_skips_legacy_email(self):
        from app.models import CommunicationFollowUp
        from datetime import datetime
        recipient = self._campaign_recipient()
        recipient.campaign.created_by.role.name = "Super Admin"
        db.session.commit()
        user_id, campaign_id, recipient_id = recipient.campaign.created_by_id, recipient.campaign_id, recipient.id
        with self.client.session_transaction() as session:
            session['_user_id'] = str(user_id)
            session['_fresh'] = True
        response = self.client.post(f"/communications/{campaign_id}/schedule-follow-up",
                                    data={"channel": "email", "days": "3"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(CommunicationFollowUp.query.one().channel, "whatsapp")
        legacy = CommunicationFollowUp(campaign_id=campaign_id, recipient_id=recipient_id,
                                       due_at=datetime.utcnow(), channel="email", status="Pending")
        db.session.add(legacy); db.session.commit()
        with patch("app.routes.communications._send_to_recipient") as sender:
            result = self.app.test_cli_runner().invoke(args=['process-communication-followups'])
        self.assertEqual(result.exit_code, 0, result.output)
        sender.assert_not_called()
        self.assertEqual(legacy.status, "Skipped")

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

    def test_join_now_qualifies_selects_product_and_opens_application(self):
        recipient = self._campaign_recipient()
        recipient.policy.id_number = "8001015009087"
        recipient.policy.email_address = "client@example.test"
        product = PolicyProduct(product_name="Test Family", plan_name="R10k", monthly_premium=99, cover_amount=10000, active=True)
        db.session.add(product)
        db.session.commit()
        product_id = product.id
        self.assertEqual(self.client.get("/join/recipient-token").status_code, 200)
        response = self.client.post("/join/recipient-token", data={"id_number": "8001015009087", "total_members": "4", "cover_amount": "10000"})
        self.assertTrue(response.location.endswith("/join/recipient-token/products"))
        self.assertIn(b"Test Family", self.client.get(response.location).data)
        response = self.client.get(f"/join/recipient-token/application/{product_id}")
        self.assertIn("/online-application/", response.location)
        application = ClientApplication.query.filter_by(source_campaign_recipient_id=recipient.id).one()
        self.assertEqual(application.total_members, 4)
        self.assertEqual(int(application.requested_cover), 10000)
        self.assertEqual(application.product_id, product_id)
        self.assertEqual(self.client.get(response.location).status_code, 200)

    def test_join_now_offers_closest_product_that_covers_all_members(self):
        recipient = self._campaign_recipient()
        recipient.policy.id_number = "8001015009087"
        gold = PolicyProduct(product_name="Gold Family", plan_name="R40k", monthly_premium=300,
                             cover_amount=40000, min_age=31, max_age=55, active=True)
        alternative = PolicyProduct(product_name="Member +9", plan_name="R30k", monthly_premium=280,
                                    cover_amount=30000, min_age=18, max_age=70, active=True)
        db.session.add_all([gold, alternative]); db.session.flush()
        db.session.add_all([
            PolicyProductRule(product_id=gold.id, plan_type="family", spouse_slots=1, child_slots=6),
            PolicyProductRule(product_id=alternative.id, plan_type="member_product", extra_member_slots=9),
        ])
        db.session.commit(); alternative_id = alternative.id
        response = self.client.post("/join/recipient-token", data={
            "id_number": "8001015009087", "total_members": "10", "cover_amount": "40000"})
        page = self.client.get(response.location)
        self.assertIn(b"No policy matches both choices exactly", page.data)
        self.assertIn(b"Member +9", page.data); self.assertIn(b"R 30,000.00", page.data)
        self.assertNotIn(b"Gold Family", page.data)
        selected = self.client.get(f"/join/recipient-token/application/{alternative_id}")
        self.assertIn("/online-application/", selected.location)
        application = ClientApplication.query.filter_by(source_campaign_recipient_id=recipient.id).one()
        self.assertEqual(int(application.requested_cover), 40000)
        self.assertEqual(int(application.cover_amount), 30000)

    def test_recommendation_cross_checks_member_count_and_cover(self):
        from app.routes.join import _product_choices
        from app.services.compliance_service import classify_product_template
        one = PolicyProduct(product_name="Principal Only", plan_name="R20k", cover_amount=20000, monthly_premium=100, active=True)
        family = PolicyProduct(product_name="Family Eight", plan_name="R20k", cover_amount=20000, monthly_premium=110, active=True)
        ten_30 = PolicyProduct(product_name="1 + 9 R30 000", plan_name="R30k", cover_amount=30000, monthly_premium=270, active=True)
        ten_40 = PolicyProduct(product_name="Member +9 R40 000", plan_name="R40k", cover_amount=40000, monthly_premium=310, active=True)
        larger_40 = PolicyProduct(product_name="Member +13 R40 000", plan_name="R40k", cover_amount=40000, monthly_premium=320, active=True)
        db.session.add_all([one, family, ten_30, ten_40, larger_40]); db.session.flush()
        db.session.add(PolicyProductRule(product_id=one.id, plan_type="single"))
        db.session.commit()
        self.assertEqual(classify_product_template(ten_30), 'member_product')
        products, alternatives = _product_choices({"age": 40, "total_members": 1, "cover_amount": 40000})
        self.assertTrue(alternatives)
        self.assertEqual([p.id for p in products], [one.id])
        products, alternatives = _product_choices({"age": 40, "total_members": 10, "cover_amount": 40000})
        self.assertFalse(alternatives)
        self.assertEqual([p.id for p in products], [ten_40.id])
        products, alternatives = _product_choices({"age": 40, "total_members": 10, "cover_amount": 20000})
        self.assertTrue(alternatives)
        self.assertEqual([p.id for p in products], [ten_30.id, ten_40.id])

    def test_template_send_keeps_apply_callback_delete_order(self):
        from app.services.whatsapp_service import send_whatsapp_template_image
        response = Mock(status_code=200, content=b"json")
        response.json.return_value = {"messages": [{"id": "template-message"}]}
        buttons = [
            {"type": "QUICK_REPLY", "text": "DELETE MY NUMBER"},
            {"type": "URL", "text": "APPLY NOW", "url": "https://example.test/join/{{1}}"},
            {"type": "QUICK_REPLY", "text": "CALL ME BACK"},
        ]
        with patch.dict(os.environ, {"WHATSAPP_ENABLED": "true", "WHATSAPP_PROVIDER": "360dialog", "D360_API_KEY": "test-key"}), patch("requests.post", return_value=response) as post:
            result = send_whatsapp_template_image("0676200748", "test_template", "en", "https://example.test/image.jpg", "callback:abc", "optout:abc", buttons=buttons, join_token="abc")
        self.assertTrue(result.ok)
        components = post.call_args.kwargs["json"]["template"]["components"][2:]
        self.assertEqual([(row["sub_type"], row["index"]) for row in components], [("url", "0"), ("quick_reply", "1"), ("quick_reply", "2")])
        self.assertEqual(components[0]["parameters"][0]["text"], "abc")

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


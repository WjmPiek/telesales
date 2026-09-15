from datetime import datetime, timedelta
import hashlib
import hmac
import json
import os

from flask import Blueprint, abort, current_app, jsonify, render_template, request
from flask_login import current_user, login_required
from sqlalchemy import or_

from app import db
from app.models import User, WhatsAppContact, WhatsAppConversation, WhatsAppMessage, WhatsAppWebhookEvent, CampaignRecipient, AgentNotification
from app.services.whatsapp_service import normalize_phone, send_whatsapp_text
from app.services.communication_service import record_callback, record_opt_out
from app.services.phone_deletion import delete_phone, phone_is_suppressed

whatsapp_bp = Blueprint("whatsapp", __name__, url_prefix="/whatsapp")


def _manager_or_agent():
    return bool(current_user.is_authenticated)


def _serialize_message(message):
    return {
        "id": message.id,
        "direction": message.direction,
        "type": message.message_type,
        "body": message.body or "",
        "status": message.status,
        "created_at": message.created_at.isoformat() if message.created_at else None,
        "sender": message.sender_user.name if message.sender_user else None,
    }


def _get_or_create_contact(wa_id, name=None):
    number = normalize_phone(wa_id)
    contact = WhatsAppContact.query.filter_by(wa_id=number).first()
    if not contact:
        contact = WhatsAppContact(wa_id=number, phone_number=number, display_name=name or number, status="New")
        db.session.add(contact)
        db.session.flush()
    elif name and (not contact.display_name or contact.display_name == contact.phone_number):
        contact.display_name = name
    return contact


def _get_open_conversation(contact):
    conversation = WhatsAppConversation.query.filter(
        WhatsAppConversation.contact_id == contact.id,
        WhatsAppConversation.status.in_(["Open", "Waiting"]),
    ).order_by(WhatsAppConversation.id.desc()).first()
    if not conversation:
        conversation = WhatsAppConversation(contact_id=contact.id, status="Open", last_message_at=datetime.utcnow())
        db.session.add(conversation)
        db.session.flush()
    return conversation


def _event_time(value):
    try:
        return datetime.utcfromtimestamp(int(value))
    except (TypeError, ValueError, OverflowError, OSError):
        return datetime.utcnow()


def _latest_recipient_for_contact(contact):
    """Find the newest campaign recipient for this WhatsApp number."""
    number = normalize_phone(contact.phone_number)
    if not number:
        return None
    from app.models import LapsedPolicy
    candidates = (
        CampaignRecipient.query.join(LapsedPolicy)
        .order_by(CampaignRecipient.created_at.desc())
    )
    return next((r for r in candidates if normalize_phone(r.policy.cell_number) == number), None)



def _process_campaign_button(payload_id, contact):
    """Translate template quick-reply payloads into callback or opt-out actions."""
    if not payload_id or ":" not in str(payload_id):
        return
    action, token = str(payload_id).split(":", 1)
    recipient = CampaignRecipient.query.filter_by(secure_token=token).first()
    if not recipient:
        return
    if normalize_phone(recipient.policy.cell_number) != normalize_phone(contact.phone_number):
        return
    if action == "callback":
        record_callback(recipient, "whatsapp")
        contact.tags = ", ".join(dict.fromkeys([x.strip() for x in ((contact.tags or "") + ", callback, hot").split(",") if x.strip()]))
        contact.status = "Callback Requested"
    elif action in {"optout", "opt_out"}:
        number = contact.phone_number
        record_opt_out(recipient, "whatsapp")
        delete_phone(number)
        contact.opted_out = True
        contact.status = "Opted Out"
    else:
        return


def _process_payload(payload):
    entries = payload.get("entry") or []
    changes = []
    for entry in entries:
        changes.extend(entry.get("changes") or [])
    if not changes and (payload.get("messages") or payload.get("statuses")):
        changes = [{"value": payload}]

    for change in changes:
        # Coexistence history, contact sync and business-app echoes are not
        # customer replies. Never run callback/deletion actions on those events.
        if change.get("field") not in {None, "messages"}:
            continue
        value = change.get("value") or {}
        provider = os.getenv("WHATSAPP_PROVIDER", "meta").strip().lower()
        configured_phone = (os.getenv("D360_PHONE_NUMBER_ID") if provider == "360dialog"
                            else os.getenv("META_PHONE_NUMBER_ID") or os.getenv("WHATSAPP_PHONE_NUMBER_ID"))
        if configured_phone and str((value.get("metadata") or {}).get("phone_number_id")) != configured_phone:
            continue
        contacts = value.get("contacts") or []
        names = {str(c.get("wa_id")): ((c.get("profile") or {}).get("name")) for c in contacts}

        for item in value.get("messages") or []:
            provider_id = item.get("id")
            if provider_id and WhatsAppMessage.query.filter_by(provider_message_id=provider_id).first():
                continue
            sender = str(item.get("from") or "")
            if not sender or not provider_id or phone_is_suppressed(sender):
                continue
            contact = _get_or_create_contact(sender, names.get(sender))
            conversation = _get_open_conversation(contact)
            message_type = item.get("type") or "text"
            body = ""
            media_id = None
            mime = None
            if message_type == "text":
                body = (item.get("text") or {}).get("body") or ""
                if body.strip().casefold() in {"stop", "unsubscribe", "opt out", "opt-out", "cancel", "end", "quit"}:
                    recipient = _latest_recipient_for_contact(contact)
                    if recipient:
                        record_opt_out(recipient, "whatsapp")
                    delete_phone(sender)
                    contact.opted_out = True
                    contact.status = "Opted Out"
            elif message_type in {"image", "document", "audio", "video", "sticker"}:
                media = item.get(message_type) or {}
                media_id = media.get("id")
                mime = media.get("mime_type")
                body = media.get("caption") or f"[{message_type.title()} received]"
            elif message_type == "button":
                button = item.get("button") or {}
                body = button.get("text") or "[Button response]"
                payload_id = button.get("payload") or button.get("id")
                _process_campaign_button(payload_id, contact)
            elif message_type == "interactive":
                interactive = item.get("interactive") or {}
                reply = interactive.get("button_reply") or interactive.get("list_reply") or {}
                body = reply.get("title") or reply.get("id") or "[Interactive response]"
                _process_campaign_button(reply.get("id"), contact)
            else:
                body = f"[{message_type.title()} message]"

            timestamp = item.get("timestamp")
            if contact.opted_out:
                body = "Number deleted"
            created_at = _event_time(timestamp)
            db.session.add(WhatsAppMessage(
                conversation_id=conversation.id,
                provider_message_id=provider_id,
                direction="inbound",
                message_type=message_type,
                body=body,
                media_id=media_id,
                media_mime_type=mime,
                status="received",
                raw_payload=None,
                created_at=created_at,
            ))
            conversation.last_message_preview = body[:500]
            conversation.last_message_at = created_at
            conversation.unread_count = (conversation.unread_count or 0) + 1
            contact.updated_at = datetime.utcnow()

        for status in value.get("statuses") or []:
            provider_id = status.get("id")
            message = WhatsAppMessage.query.filter_by(provider_message_id=provider_id).first()
            if not message:
                continue
            state = str(status.get("status") or message.status or "").lower()
            order = {"sent": 1, "delivered": 2, "read": 3}
            if message.status in order and state in order and order[state] < order[message.status]:
                continue
            message.status = state
            timestamp = status.get("timestamp")
            event_at = _event_time(timestamp)
            if state == "delivered":
                message.delivered_at = event_at
            elif state == "read":
                message.read_at = event_at
            elif state == "failed":
                errors = status.get("errors") or []
                message.error_message = json.dumps(errors) if errors else "Delivery failed"
            if message.campaign_recipient:
                recipient_state = {
                    "sent": "Sent",
                    "delivered": "Delivered",
                    "read": "Read",
                    "failed": "Failed",
                }.get(state)
                if recipient_state:
                    message.campaign_recipient.whatsapp_status = recipient_state


@whatsapp_bp.route("/")
@login_required
def inbox():
    if not _manager_or_agent():
        abort(403)
    q = (request.args.get("q") or "").strip()
    status = (request.args.get("status") or "Open").strip()
    query = WhatsAppConversation.query.join(WhatsAppContact)
    if status and status != "All":
        query = query.filter(WhatsAppConversation.status == status)
    if q:
        like = f"%{q}%"
        query = query.filter(or_(WhatsAppContact.display_name.ilike(like), WhatsAppContact.phone_number.ilike(like), WhatsAppConversation.last_message_preview.ilike(like)))
    conversations = query.order_by(WhatsAppConversation.last_message_at.desc()).limit(250).all()
    selected = None
    selected_id = request.args.get("conversation", type=int)
    if selected_id:
        selected = WhatsAppConversation.query.get(selected_id)
    if not selected and conversations:
        selected = conversations[0]
    if selected:
        selected.unread_count = 0
        db.session.commit()
    agents = User.query.filter_by(active=True).order_by(User.name).all()
    metrics = {
        "open": WhatsAppConversation.query.filter_by(status="Open").count(),
        "waiting": WhatsAppConversation.query.filter_by(status="Waiting").count(),
        "unread": db.session.query(db.func.coalesce(db.func.sum(WhatsAppConversation.unread_count), 0)).scalar() or 0,
        "today": WhatsAppMessage.query.filter(WhatsAppMessage.created_at >= datetime.utcnow().date()).count(),
    }
    provider_ready = (
        os.getenv("WHATSAPP_ENABLED", "false").lower() in {"1", "true", "yes", "y"}
        and bool(os.getenv("META_ACCESS_TOKEN") or os.getenv("WHATSAPP_ACCESS_TOKEN") or os.getenv("D360_API_KEY"))
        and bool(os.getenv("META_PHONE_NUMBER_ID") or os.getenv("WHATSAPP_PHONE_NUMBER_ID") or os.getenv("D360_API_KEY"))
    )
    return render_template("whatsapp/inbox.html", conversations=conversations, selected=selected, agents=agents, metrics=metrics, q=q, status=status, provider_ready=provider_ready)


@whatsapp_bp.route("/api/conversations/<int:conversation_id>")
@login_required
def conversation_data(conversation_id):
    conversation = WhatsAppConversation.query.get_or_404(conversation_id)
    messages = WhatsAppMessage.query.filter_by(conversation_id=conversation.id).order_by(WhatsAppMessage.created_at.asc()).all()
    conversation.unread_count = 0
    db.session.commit()
    return jsonify({"conversation": {"id": conversation.id, "status": conversation.status, "contact": conversation.contact.display_name or conversation.contact.phone_number}, "messages": [_serialize_message(m) for m in messages]})


@whatsapp_bp.route("/api/conversations/<int:conversation_id>/send", methods=["POST"])
@login_required
def send_message(conversation_id):
    conversation = WhatsAppConversation.query.get_or_404(conversation_id)
    data = request.get_json(silent=True) or request.form
    body = (data.get("message") or "").strip()
    if not body:
        return jsonify({"ok": False, "error": "Message cannot be empty."}), 400
    if conversation.contact.opted_out:
        return jsonify({"ok": False, "error": "This contact has opted out."}), 409
    latest = WhatsAppMessage.query.join(WhatsAppConversation).filter(
        WhatsAppConversation.contact_id == conversation.contact_id,
        WhatsAppMessage.direction == "inbound",
    ).order_by(WhatsAppMessage.created_at.desc()).first()
    if not latest or latest.created_at < datetime.utcnow() - timedelta(hours=24):
        return jsonify({"ok": False, "error": "The 24-hour reply window is closed. Send an approved campaign template."}), 409
    result = send_whatsapp_text(conversation.contact.phone_number, body)
    message = WhatsAppMessage(
        conversation_id=conversation.id,
        provider_message_id=result.message_id,
        direction="outbound",
        message_type="text",
        body=body,
        status="sent" if result.ok else "failed",
        error_message=result.error,
        sender_user_id=current_user.id,
        raw_payload=json.dumps(result.response_json or {}),
    )
    db.session.add(message)
    conversation.last_message_preview = body[:500]
    conversation.last_message_at = datetime.utcnow()
    conversation.assigned_agent_id = conversation.assigned_agent_id or current_user.id
    db.session.commit()
    return jsonify({"ok": result.ok, "error": result.error, "message": _serialize_message(message)}), (200 if result.ok else 502)


@whatsapp_bp.route("/api/conversations/<int:conversation_id>/update", methods=["POST"])
@login_required
def update_conversation(conversation_id):
    conversation = WhatsAppConversation.query.get_or_404(conversation_id)
    data = request.get_json(silent=True) or request.form
    if "status" in data and data.get("status") in {"Open", "Waiting", "Closed"}:
        conversation.status = data.get("status")
        conversation.closed_at = datetime.utcnow() if conversation.status == "Closed" else None
    if "priority" in data and data.get("priority") in {"Low", "Normal", "High", "Urgent"}:
        conversation.priority = data.get("priority")
    if "assigned_agent_id" in data:
        agent_id = data.get("assigned_agent_id")
        conversation.assigned_agent_id = int(agent_id) if str(agent_id).isdigit() else None
        conversation.contact.assigned_agent_id = conversation.assigned_agent_id
    if "tags" in data:
        conversation.contact.tags = (data.get("tags") or "")[:500]
    if "notes" in data:
        conversation.contact.notes = data.get("notes") or ""
    db.session.commit()
    return jsonify({"ok": True})


@whatsapp_bp.route("/webhook", methods=["GET", "POST"])
def webhook():
    if request.method == "GET":
        token = request.args.get("hub.verify_token")
        challenge = request.args.get("hub.challenge")
        configured = current_app.config.get("WHATSAPP_VERIFY_TOKEN") or os.getenv("WHATSAPP_VERIFY_TOKEN")

        # A normal browser/health-check request should confirm that the endpoint exists.
        # Meta verification requests still require the configured verify token.
        if not token and not challenge:
            return jsonify({"ok": True, "service": "whatsapp-webhook"}), 200
        if configured and token == configured:
            return challenge or "OK", 200
        return "Invalid verification token", 403

    provider = os.getenv("WHATSAPP_PROVIDER", "meta").strip().lower()
    if provider == "360dialog":
        secret = os.getenv("D360_WEBHOOK_SECRET", "")
        if not secret or not os.getenv("D360_PHONE_NUMBER_ID"):
            return jsonify({"ok": False, "error": "360dialog webhook configuration is incomplete"}), 503
        supplied = request.headers.get("Authorization", "")
        if not hmac.compare_digest(supplied.encode(), ("Bearer " + secret).encode()):
            return jsonify({"ok": False, "error": "Invalid webhook authorization"}), 403
    elif provider == "meta":
        app_secret = os.getenv("META_APP_SECRET")
        if not app_secret:
            return jsonify({"ok": False, "error": "META_APP_SECRET is required"}), 503
        signature = request.headers.get("X-Hub-Signature-256", "")
        expected = "sha256=" + hmac.new(app_secret.encode("utf-8"), request.get_data(), hashlib.sha256).hexdigest()
        if not signature or not hmac.compare_digest(signature, expected):
            return jsonify({"ok": False, "error": "Invalid Meta webhook signature"}), 403
    else:
        return jsonify({"ok": False, "error": "Unsupported WhatsApp provider"}), 503
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict) or payload.get("object") not in {None, "whatsapp_business_account"}:
        return jsonify({"ok": False, "error": "Invalid WhatsApp webhook payload"}), 400
    payload = payload or {}
    raw = json.dumps(payload, sort_keys=True)
    event_key = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    event = WhatsAppWebhookEvent.query.filter_by(event_key=event_key).first()
    if event and event.processed:
        return jsonify({"ok": True, "duplicate": True})
    if not event:
        event = WhatsAppWebhookEvent(event_key=event_key, payload="{}")
        db.session.add(event)
    else:
        event.error = None
    try:
        _process_payload(payload)
        event.processed = True
        event.processed_at = datetime.utcnow()
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        failed = WhatsAppWebhookEvent.query.filter_by(event_key=event_key).first()
        if not failed:
            failed = WhatsAppWebhookEvent(event_key=event_key, payload="{}")
            db.session.add(failed)
        failed.processed = False
        failed.error = str(exc)
        db.session.commit()
        current_app.logger.exception("WhatsApp webhook processing failed")
        return jsonify({"ok": False, "error": "Webhook processing failed"}), 500
    return jsonify({"ok": True})

import hashlib
import re
from datetime import datetime, date
from flask import current_app, url_for
from app import db
from app.models import ContactCommunicationPreference, ContactSuppression, AgentNotification, AuditLog


def normalize_phone(value):
    digits = re.sub(r"\D", "", str(value or ""))
    if digits.startswith("0"):
        digits = "27" + digits[1:]
    return digits


def normalize_email(value):
    return str(value or "").strip().lower()


def contact_hash(value):
    value = str(value or "").strip().lower()
    return hashlib.sha256(value.encode("utf-8")).hexdigest() if value else None


def preference_for(policy):
    pref = ContactCommunicationPreference.query.filter_by(lapsed_policy_id=policy.id).first()
    if not pref:
        pref = ContactCommunicationPreference(lapsed_policy_id=policy.id)
        db.session.add(pref)
        db.session.flush()
    return pref


def is_suppressed(policy):
    phone = contact_hash(normalize_phone(policy.cell_number))
    email = contact_hash(normalize_email(policy.email_address))
    query = ContactSuppression.query
    checks = []
    if phone:
        checks.append(ContactSuppression.phone_hash == phone)
    if email:
        checks.append(ContactSuppression.email_hash == email)
    return bool(checks and query.filter(db.or_(*checks)).first())


def callback_links(token):
    base = current_app.config.get("BASE_URL", "").rstrip("/")
    return {
        "callback": base + url_for("communications.public_response", token=token, action="callback"),
        "not_interested": base + url_for("communications.public_response", token=token, action="not-interested"),
        "opt_out": base + url_for("communications.public_response", token=token, action="opt-out"),
    }


def record_callback(recipient, channel):
    policy = recipient.policy
    if recipient.response_type == "callback" and recipient.callback_created:
        return policy
    recipient.response_type = "callback"
    recipient.response_channel = channel
    recipient.responded_at = datetime.utcnow()
    recipient.callback_created = True
    policy.recovery_status = "Callback"
    policy.next_action_date = date.today()
    policy.comments = ((policy.comments or "") + f"\nCALLBACK REQUESTED via {channel.title()} on {datetime.utcnow():%Y-%m-%d %H:%M} UTC").strip()
    if policy.assigned_agent_id:
        db.session.add(AgentNotification(
            user_id=policy.assigned_agent_id,
            title="New callback request",
            message=f"{policy.initials or ''} {policy.surname or ''} requested a callback via {channel.title()}.",
            notification_type="callback",
            entity_type="LapsedPolicy",
            entity_id=policy.id,
        ))
    db.session.add(AuditLog(action="CAMPAIGN_CALLBACK_REQUESTED", entity_type="LapsedPolicy", entity_id=str(policy.id), details=f"Channel: {channel}; campaign: {recipient.campaign_id}"))
    return policy


def record_not_interested(recipient, channel):
    recipient.response_type = "not_interested"
    recipient.response_channel = channel
    recipient.responded_at = datetime.utcnow()
    recipient.policy.recovery_status = "Closed"
    recipient.policy.next_action_date = None
    recipient.policy.comments = ((recipient.policy.comments or "") + f"\nNOT INTERESTED via {channel.title()}").strip()


def record_opt_out(recipient, channel):
    policy = recipient.policy
    pref = preference_for(policy)
    pref.telephone_allowed = False
    pref.whatsapp_allowed = False
    pref.email_allowed = False
    pref.opted_out_all = True
    pref.opted_out_at = datetime.utcnow()
    pref.opt_out_source = channel
    recipient.response_type = "opt_out"
    recipient.response_channel = channel
    recipient.responded_at = datetime.utcnow()
    policy.recovery_status = "Opted Out"
    policy.next_action_date = None
    phone_hash = contact_hash(normalize_phone(policy.cell_number))
    email_hash = contact_hash(normalize_email(policy.email_address))
    existing = ContactSuppression.query.filter(db.or_(
        ContactSuppression.phone_hash == phone_hash if phone_hash else db.false(),
        ContactSuppression.email_hash == email_hash if email_hash else db.false(),
    )).first()
    if not existing:
        db.session.add(ContactSuppression(phone_hash=phone_hash, email_hash=email_hash, source=channel, campaign_id=recipient.campaign_id, lapsed_policy_id=policy.id))
    policy.comments = ((policy.comments or "") + f"\nOPTED OUT via {channel.title()} on {datetime.utcnow():%Y-%m-%d %H:%M} UTC").strip()

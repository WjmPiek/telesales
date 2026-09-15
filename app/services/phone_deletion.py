"""Transactional removal of a marketing number; callers commit with the response."""
import re
from datetime import datetime

from app import db
from app.models import ContactSuppression, LapsedPolicy, WhatsAppContact, WhatsAppMessage, WhatsAppWebhookEvent
from app.services.communication_service import contact_hash, normalize_phone, preference_for


def phone_is_suppressed(number):
    digest = contact_hash(normalize_phone(number))
    return bool(digest and ContactSuppression.query.filter_by(phone_hash=digest).first())


def delete_phone(number):
    number = normalize_phone(number)
    if not number:
        return
    digest = contact_hash(number)
    if not phone_is_suppressed(number):
        db.session.add(ContactSuppression(phone_hash=digest, source="whatsapp"))
    # Disable every duplicate marketing record before clearing its number.
    for policy in LapsedPolicy.query.filter(LapsedPolicy.cell_number.isnot(None)).yield_per(500):
        if normalize_phone(policy.cell_number) == number:
            pref = preference_for(policy)
            pref.telephone_allowed = pref.whatsapp_allowed = pref.email_allowed = False
            pref.opted_out_all = True
            pref.opted_out_at = datetime.utcnow()
            pref.opt_out_source = "whatsapp"
            policy.recovery_status = "Opted Out"
            policy.next_action_date = None
    for contact in WhatsAppContact.query.all():
        if normalize_phone(contact.phone_number) == number:
            contact.wa_id = "deleted:" + digest[:32]
            contact.phone_number = ""
            contact.display_name = "Deleted number"
            contact.opted_out = True
            contact.status = "Opted Out"
            contact.notes = None
            for conversation in contact.conversations:
                conversation.status = "Closed"
                conversation.last_message_preview = "Number deleted"
                for message in conversation.messages:
                    message.body = "[Removed on number deletion]"
                    message.raw_payload = None
                    message.error_message = None
    # Cover structured phone fields in applications, recovery forms and policies.
    fields = {"cell_number", "home_tel", "work_tel"}
    for mapper in db.Model.registry.mappers:
        model = mapper.class_
        columns = [c.key for c in mapper.columns if c.key in fields]
        if columns:
            for row in db.session.query(model).yield_per(500):
                for field in columns:
                    if normalize_phone(getattr(row, field)) == number:
                        setattr(row, field, None)
    # Raw provider envelopes can contain the number even after contact deletion.
    # They are diagnostic duplicates, not the delivery ledger.
    pattern = re.compile(r"(?<!\w)\+?\d[\d ()-]{6,}\d(?!\w)")
    text_fields = {"comments", "notes", "body", "details", "message", "raw_payload", "payload", "error_message", "error", "last_message_preview"}
    for mapper in db.Model.registry.mappers:
        columns = [c.key for c in mapper.columns if c.key in text_fields and isinstance(c.type, db.String)]
        if columns:
            for row in db.session.query(mapper.class_).yield_per(500):
                for field in columns:
                    value = getattr(row, field)
                    if isinstance(value, str):
                        setattr(row, field, pattern.sub(lambda m: "[number deleted]" if normalize_phone(m.group()) == number else m.group(), value))

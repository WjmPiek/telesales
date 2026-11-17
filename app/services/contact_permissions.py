"""Read contact permissions in bulk without creating preferences on page loads."""
from app import db
from app.models import ContactCommunicationPreference, ContactSuppression
from app.services.communication_service import contact_hash, normalize_phone, normalize_email


def allowed_contacts(policies, channel):
    policies = list(policies)
    if not policies:
        return {}
    preferences = {p.lapsed_policy_id: p for p in ContactCommunicationPreference.query.filter(
        ContactCommunicationPreference.lapsed_policy_id.in_([p.id for p in policies])).all()}
    hashes = db.session.query(ContactSuppression.phone_hash, ContactSuppression.email_hash).all()
    phones = {phone for phone, email in hashes if phone}
    emails = {email for phone, email in hashes if email}
    result = {}
    for policy in policies:
        pref = preferences.get(policy.id)
        result[policy.id] = bool(
            (pref is None or (not pref.opted_out_all and getattr(pref, channel + '_allowed'))) and
            contact_hash(normalize_phone(policy.cell_number)) not in phones and
            contact_hash(normalize_email(policy.email_address)) not in emails)
    return result

"""Read-only, phone-deduplicated client communication directory."""
from collections import defaultdict
from datetime import datetime
from sqlalchemy.orm import joinedload
from app import db
from app.models import CampaignRecipient, ContactCommunicationPreference, ContactSuppression
from app.services.communication_service import contact_hash, normalize_phone, normalize_email


def client_rows(policies, campaign_ids):
    groups = {}
    policy_keys = {}
    for policy in policies:
        phone = normalize_phone(policy.cell_number)
        key = phone or f'policy:{policy.id}'
        policy_keys[policy.id] = key
        row = groups.setdefault(key, dict(policy=policy, phone=phone, branches=set(), history=[], opted_out=False))
        row['branches'].add(policy.branch or '')
    if not groups:
        return []
    preferences = ContactCommunicationPreference.query.filter(
        ContactCommunicationPreference.lapsed_policy_id.in_(policy_keys)).all()
    for pref in preferences:
        if pref.opted_out_all or not pref.whatsapp_allowed:
            groups[policy_keys[pref.lapsed_policy_id]]['opted_out'] = True
    phone_hashes = {item[0] for item in db.session.query(ContactSuppression.phone_hash).filter(ContactSuppression.phone_hash.isnot(None)).all()}
    email_hashes = {item[0] for item in db.session.query(ContactSuppression.email_hash).filter(ContactSuppression.email_hash.isnot(None)).all()}
    for policy in policies:
        if contact_hash(normalize_phone(policy.cell_number)) in phone_hashes or contact_hash(normalize_email(policy.email_address)) in email_hashes:
            groups[policy_keys[policy.id]]['opted_out'] = True
    recipients = CampaignRecipient.query.options(joinedload(CampaignRecipient.campaign)).filter(
        CampaignRecipient.lapsed_policy_id.in_(policy_keys), CampaignRecipient.campaign_id.in_(campaign_ids)
    ).order_by(CampaignRecipient.created_at.desc(), CampaignRecipient.id.desc()).all()
    for recipient in recipients:
        groups[policy_keys[recipient.lapsed_policy_id]]['history'].append(recipient)
    for row in groups.values():
        history = row['history']
        row['latest'] = history[0] if history else None
        row['status'] = (history[0].whatsapp_status or 'Not Sent') if history else 'Not Sent'
        row['sent'] = any(r.whatsapp_status in {'Sent', 'Delivered', 'Read'} for r in history)
        row['delivered'] = any(r.whatsapp_status in {'Delivered', 'Read'} for r in history)
        row['callback'] = any(r.response_type == 'callback' for r in history)
        row['opted_out'] |= any(r.response_type == 'opt_out' for r in history)
        row['name'] = f"{row['policy'].initials or ''} {row['policy'].surname or ''}".strip() or 'Unnamed client'
        row['last_at'] = max((r.responded_at or r.created_at for r in history), default=None)
    return sorted(groups.values(), key=lambda r: (r['last_at'] or datetime.min, r['policy'].id), reverse=True)

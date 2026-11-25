"""One campaign message per normalized phone number, retaining source policies."""
from app import db
from app.services.communication_service import normalize_phone
from app.models import CampaignRecipient, LapsedPolicy

SENT_STATUSES = {'Sent', 'Delivered', 'Read'}


def contact_number(policy):
    number = normalize_phone(policy.cell_number)
    return number if number and 7 <= len(number) <= 15 else None


def campaign_number_states(campaign_id, numbers, *, exclude_id=None):
    # Stream phone values in small groups and retain only matches for this batch.
    remaining = set(numbers) - {None, ''}
    if not remaining:
        return {}
    candidates = db.session.query(LapsedPolicy.cell_number, CampaignRecipient.whatsapp_status).select_from(
        CampaignRecipient
    ).join(LapsedPolicy, CampaignRecipient.lapsed_policy_id == LapsedPolicy.id).filter(
        CampaignRecipient.campaign_id == campaign_id,
        CampaignRecipient.whatsapp_status.in_(SENT_STATUSES | {'Sending'}),
    )
    if exclude_id is not None:
        candidates = candidates.filter(CampaignRecipient.id != exclude_id)
    matches = {}
    for phone, status in candidates.yield_per(250):
        number = normalize_phone(phone)
        if number in remaining:
            remaining.remove(number)
            matches[number] = status
            if not remaining:
                break
    return matches


def contact_history():
    """Use normalized phones across campaigns and duplicate policy records."""
    sent = set()
    failed = set()
    rows = CampaignRecipient.query.with_entities(
        LapsedPolicy.cell_number, CampaignRecipient.whatsapp_status
    ).join(LapsedPolicy, CampaignRecipient.lapsed_policy_id == LapsedPolicy.id).filter(
        CampaignRecipient.whatsapp_status.in_(SENT_STATUSES | {'Failed', 'Sending'})
    ).all()
    for phone, status in rows:
        number = normalize_phone(phone)
        if number:
            (sent if status in SENT_STATUSES or status == 'Sending' else failed).add(number)
    return sent, failed - sent


def audience_rows(policies, recipients):
    from app.services.contact_permissions import allowed_contacts
    policies = list(policies)
    permissions = allowed_contacts(policies, 'whatsapp')
    existing = {contact_number(row.policy) for row in recipients if row.policy}
    sent, failed = contact_history()
    existing.discard(None)
    seen = set();unique = [];states = {}
    for policy in policies:
        number = contact_number(policy)
        if number in sent:
            continue
        if number and number in seen:
            continue
        if number:seen.add(number)
        allowed=bool(number and permissions[policy.id])
        states[policy.id]={'number':number or '', 'sent':False,
            'status':'Failed' if number in failed else None, 'failed':number in failed, 'selected':number in existing,
            'selectable':allowed and number not in sent}
        unique.append(policy)
    unique.sort(key=lambda policy: states[policy.id]['failed'])
    return unique,states

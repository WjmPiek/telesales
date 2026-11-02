"""One campaign message per normalized phone number, retaining source policies."""
from app.services.communication_service import normalize_phone, is_suppressed, preference_for
from app.models import CampaignRecipient, LapsedPolicy

SENT_STATUSES = {'Sent', 'Delivered', 'Read'}


def contact_number(policy):
    number = normalize_phone(policy.cell_number)
    return number if number and 7 <= len(number) <= 15 else None


def contact_history():
    """Use normalized phones across campaigns and duplicate policy records."""
    sent = set()
    failed = set()
    rows = CampaignRecipient.query.with_entities(
        LapsedPolicy.cell_number, CampaignRecipient.whatsapp_status
    ).join(LapsedPolicy, CampaignRecipient.lapsed_policy_id == LapsedPolicy.id).filter(
        CampaignRecipient.whatsapp_status.in_(SENT_STATUSES | {'Failed'})
    ).all()
    for phone, status in rows:
        number = normalize_phone(phone)
        if number:
            (sent if status in SENT_STATUSES else failed).add(number)
    return sent, failed - sent


def audience_rows(policies, recipients):
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
        pref=preference_for(policy)
        allowed=bool(number and not pref.opted_out_all and pref.whatsapp_allowed and not is_suppressed(policy))
        states[policy.id]={'number':number or '', 'sent':False,
            'status':'Failed' if number in failed else None, 'failed':number in failed, 'selected':number in existing,
            'selectable':allowed and number not in sent}
        unique.append(policy)
    unique.sort(key=lambda policy: states[policy.id]['failed'])
    return unique,states

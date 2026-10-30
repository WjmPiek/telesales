"""One campaign message per normalized phone number, retaining source policies."""
from app.services.communication_service import normalize_phone, is_suppressed, preference_for

SENT_STATUSES = {'Sent', 'Delivered', 'Read'}


def contact_number(policy):
    number = normalize_phone(policy.cell_number)
    return number if number and 7 <= len(number) <= 15 else None


def audience_rows(policies, recipients):
    existing = {contact_number(row.policy) for row in recipients if row.policy}
    sent = {contact_number(row.policy): row.whatsapp_status for row in recipients
            if row.policy and row.whatsapp_status in SENT_STATUSES}
    existing.discard(None);sent.pop(None, None)
    seen = set();unique = [];states = {}
    for policy in policies:
        number = contact_number(policy)
        if number and number in seen:
            continue
        if number:seen.add(number)
        pref=preference_for(policy)
        allowed=bool(number and not pref.opted_out_all and pref.whatsapp_allowed and not is_suppressed(policy))
        states[policy.id]={'number':number or '', 'sent':number in sent,
            'status':sent.get(number), 'selected':number in existing,
            'selectable':allowed and number not in existing}
        unique.append(policy)
    return unique,states

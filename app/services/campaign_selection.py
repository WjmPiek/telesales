"""Queue an explicit selection without sending other saved campaign recipients."""
import json
import secrets
from app import db
from app.models import CampaignRecipient
from app.services.campaign_audience import contact_number, SENT_STATUSES
from app.services.communication_service import preference_for, is_suppressed


def select_recipients(campaign, policies):
    existing = {}
    sent = set()
    for recipient in campaign.recipients:
        number = contact_number(recipient.policy)
        if recipient.whatsapp_status in SENT_STATUSES:
            sent.add(number)
        existing.setdefault(number, recipient)
    selected = []
    seen = set()
    for policy in policies:
        number = contact_number(policy)
        if not number or number in seen or number in sent:
            continue
        seen.add(number)
        pref = preference_for(policy)
        if pref.opted_out_all or not pref.whatsapp_allowed or is_suppressed(policy):
            continue
        recipient = existing.get(number)
        if not recipient:
            recipient = CampaignRecipient(campaign_id=campaign.id, lapsed_policy_id=policy.id,
                                          secure_token=secrets.token_urlsafe(32))
            db.session.add(recipient)
            db.session.flush()
        selected.append(recipient.id)
    campaign.send_recipient_ids_json = json.dumps(selected)
    return selected


def queued_recipients(campaign):
    if campaign.send_recipient_ids_json is None:
        return list(campaign.recipients)
    # Invalid batch data must fail closed, never expand to the whole campaign.
    ids = json.loads(campaign.send_recipient_ids_json)
    if not isinstance(ids, list) or any(type(value) is not int for value in ids):
        raise ValueError('Invalid campaign client selection')
    return [r for r in campaign.recipients if r.id in set(ids)]

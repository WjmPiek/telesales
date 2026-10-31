"""Queue an explicit selection without sending other saved campaign recipients."""
import json
import secrets
from app import db
from app.models import (CampaignRecipient,CommunicationEvent,CommunicationFollowUp,
                        WhatsAppMessage,ClientApplication)
from app.services.campaign_audience import contact_number, SENT_STATUSES
from app.services.communication_service import preference_for, is_suppressed


def select_recipients(campaign, policies):
    if campaign.audience_type=='individual':
        if len(policies)!=1: raise ValueError('Choose one individual client.')
        policy=policies[0]
        pref=preference_for(policy)
        if not contact_number(policy) or pref.opted_out_all or not pref.whatsapp_allowed or is_suppressed(policy):
            return []
        previous=list(campaign.recipients)
        if previous and not any(contact_number(r.policy)==contact_number(policy) for r in previous):
            ids=[r.id for r in previous]
            history=(any(r.whatsapp_status not in {None,'Not Sent'} or r.response_type or r.responded_at for r in previous)
                or CommunicationEvent.query.filter(CommunicationEvent.recipient_id.in_(ids),CommunicationEvent.event_type!='recipient_added').first()
                or CommunicationFollowUp.query.filter(CommunicationFollowUp.recipient_id.in_(ids)).first()
                or WhatsAppMessage.query.filter(WhatsAppMessage.campaign_recipient_id.in_(ids)).first()
                or ClientApplication.query.filter(ClientApplication.source_campaign_recipient_id.in_(ids)).first())
            if history: raise ValueError('This individual message already has client history. Create a new campaign to choose another client.')
            CommunicationEvent.query.filter(CommunicationEvent.recipient_id.in_(ids)).update({CommunicationEvent.recipient_id:None},synchronize_session=False)
            for recipient in previous: db.session.delete(recipient)
            db.session.flush();db.session.expire(campaign,['recipients'])
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

"""Read-only, phone-deduplicated client communication directory."""
from datetime import datetime, timedelta
from sqlalchemy import func
from sqlalchemy.orm import joinedload
from app import db
from app.models import CampaignRecipient, ContactCommunicationPreference, ContactSuppression, CommunicationCampaign, WhatsAppMessage
from app.services.communication_service import contact_hash, normalize_phone, normalize_email


def visible_campaigns():
    from flask_login import current_user
    from app.routes.communications import _is_manager, _available_companies
    query = CommunicationCampaign.query.filter(CommunicationCampaign.deleted_at.is_(None))
    if _is_manager():
        if current_user.role.name.lower().replace('_', ' ') not in {'admin', 'super admin'}:
            query = query.filter(CommunicationCampaign.branch == current_user.branch)
    else:
        query = query.filter(CommunicationCampaign.company_id.in_([c.id for c in _available_companies()]))
    return query.order_by(CommunicationCampaign.created_at.desc()).all()


def message_date_range(values):
    start, end = values.get('sent_from',''), values.get('sent_to','')
    try:
        low=datetime.strptime(start,'%Y-%m-%d')-timedelta(hours=2) if start else None
        high=datetime.strptime(end,'%Y-%m-%d')+timedelta(days=1,hours=-2) if end else None
        if low and high and low>=high:
            raise ValueError()
        return low,high
    except ValueError:
        raise ValueError('Choose a valid message date range in South African time.')


def client_rows(policies, campaign_ids, *, include_uncontacted=False):
    groups = {}
    policy_keys = {}
    for policy in policies:
        phone = normalize_phone(policy.cell_number)
        key = phone or f'policy:{policy.id}'
        policy_keys[policy.id] = key
        row = groups.setdefault(key, dict(policy=policy, phone=phone, branches=set(), history=[], opted_out=False,
                                          policies=[], selections=[], franchises=set(), source_files=set()))
        row['branches'].add(policy.branch or '')
        row['policies'].append(policy)
        row['franchises'].add(policy.franchise or policy.company_name or policy.branch or '')
        if policy.source_filename:
            row['source_files'].add(policy.source_filename)
        if policy.recovery_status == 'Opted Out':
            row['opted_out'] = True
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
        CampaignRecipient.lapsed_policy_id.in_(policy_keys), CampaignRecipient.campaign_id.in_(campaign_ids),
    ).order_by(CampaignRecipient.created_at.desc(), CampaignRecipient.id.desc()).all()
    for recipient in recipients:
        row = groups[policy_keys[recipient.lapsed_policy_id]]
        row['selections'].append(recipient)
        if recipient.whatsapp_status in {'Sent', 'Delivered', 'Read', 'Failed', 'Held'}:
            row['history'].append(recipient)
    recipient_ids = [r.id for r in recipients]
    sent_at = dict(db.session.query(WhatsAppMessage.campaign_recipient_id, func.max(WhatsAppMessage.created_at)).filter(
        WhatsAppMessage.campaign_recipient_id.in_(recipient_ids), WhatsAppMessage.direction == 'outbound',
        WhatsAppMessage.status.in_(['sent', 'delivered', 'read'])).group_by(WhatsAppMessage.campaign_recipient_id).all()) if recipient_ids else {}
    message_dates={}
    if recipient_ids:
        for recipient_id, at in db.session.query(WhatsAppMessage.campaign_recipient_id,WhatsAppMessage.created_at).filter(
                WhatsAppMessage.campaign_recipient_id.in_(recipient_ids),WhatsAppMessage.direction=='outbound'):
            message_dates.setdefault(recipient_id,[]).append(at)
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
        row['last_sent_at'] = max((sent_at[r.id] for r in history if r.id in sent_at), default=None)
        row['message_dates'] = [at for r in history for at in message_dates.get(r.id,[])]
        row['delivery_key'] = ('read' if any(r.whatsapp_status == 'Read' for r in history) else
            'delivered' if row['delivered'] else 'sent' if row['sent'] else
            'failed' if row['status'] == 'Failed' else 'held' if row['status'] == 'Held' else 'not_sent')
        row['delivery_label'] = {'read':'Read', 'delivered':'Delivered', 'sent':'Sent',
            'failed':'Failed', 'held':'Held — not sent', 'not_sent':'Not sent'}[row['delivery_key']]
    return sorted((row for row in groups.values() if include_uncontacted or row['history']),
                  key=lambda r: (r['sent'], r['delivery_key'] in {'failed', 'held'}, -r['policy'].id))


def directory_delivery(policies):
    rows = client_rows(policies, [c.id for c in visible_campaigns()], include_uncontacted=True)
    return {policy.id: row for row in rows for policy in row['policies']}

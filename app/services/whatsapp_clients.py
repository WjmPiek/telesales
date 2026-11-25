"""Read-only, phone-deduplicated client communication directory."""
from dataclasses import dataclass
from datetime import datetime, timedelta
from sqlalchemy import func
from sqlalchemy.orm import joinedload
from app import db
from app.models import CampaignRecipient, ContactCommunicationPreference, ContactSuppression, CommunicationCampaign, LapsedPolicy, WhatsAppMessage
from app.services.communication_service import contact_hash, normalize_phone, normalize_email


def visible_campaigns(*, summaries=False):
    from flask_login import current_user
    from app.routes.communications import _is_manager, _available_companies
    query = CommunicationCampaign.query.filter(CommunicationCampaign.deleted_at.is_(None))
    if _is_manager():
        if current_user.role.name.lower().replace('_', ' ') not in {'admin', 'super admin'}:
            query = query.filter(CommunicationCampaign.branch == current_user.branch)
    else:
        query = query.filter(CommunicationCampaign.company_id.in_([c.id for c in _available_companies()]))
    if summaries:
        query = query.with_entities(CommunicationCampaign.id, CommunicationCampaign.name)
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


SENT = {'Sent', 'Delivered', 'Read'}
FAILED_OR_HELD = {'Failed', 'Held'}
HISTORY = SENT | FAILED_OR_HELD | {'Sending'}


def delivery_key(status, sent, delivered, read):
    if read:
        return 'read'
    if delivered:
        return 'delivered'
    if sent:
        return 'sent'
    return {'Failed': 'failed', 'Held': 'held', 'Sending': 'review'}.get(status, 'not_sent')


def client_rows(policies, campaign_ids, *, include_uncontacted=False, include_message_dates=True):
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
    recipients = CampaignRecipient.query.options(joinedload(CampaignRecipient.campaign).load_only(
        CommunicationCampaign.name, CommunicationCampaign.queue_status, CommunicationCampaign.audience_type
    )).filter(
        CampaignRecipient.lapsed_policy_id.in_(policy_keys), CampaignRecipient.campaign_id.in_(campaign_ids),
    ).order_by(CampaignRecipient.created_at.desc(), CampaignRecipient.id.desc()).all()
    for recipient in recipients:
        row = groups[policy_keys[recipient.lapsed_policy_id]]
        row['selections'].append(recipient)
        if recipient.whatsapp_status in HISTORY:
            row['history'].append(recipient)
    recipient_ids = [r.id for r in recipients]
    sent_at = dict(db.session.query(WhatsAppMessage.campaign_recipient_id, func.max(WhatsAppMessage.created_at)).filter(
        WhatsAppMessage.campaign_recipient_id.in_(recipient_ids), WhatsAppMessage.direction == 'outbound',
        WhatsAppMessage.status.in_(['sent', 'delivered', 'read'])).group_by(WhatsAppMessage.campaign_recipient_id).all()) if recipient_ids else {}
    message_dates={}
    if include_message_dates and recipient_ids:
        for recipient_id, at in db.session.query(WhatsAppMessage.campaign_recipient_id,WhatsAppMessage.created_at).filter(
                WhatsAppMessage.campaign_recipient_id.in_(recipient_ids),WhatsAppMessage.direction=='outbound'):
            message_dates.setdefault(recipient_id,[]).append(at)
    for row in groups.values():
        history = row['history']
        row['latest'] = history[0] if history else None
        row['status'] = (history[0].whatsapp_status or 'Not Sent') if history else 'Not Sent'
        row['sent'] = any(r.whatsapp_status in SENT for r in history)
        row['delivered'] = any(r.whatsapp_status in {'Delivered', 'Read'} for r in history)
        row['callback'] = any(r.response_type == 'callback' for r in history)
        row['opted_out'] |= any(r.response_type == 'opt_out' for r in history)
        row['name'] = f"{row['policy'].initials or ''} {row['policy'].surname or ''}".strip() or 'Unnamed client'
        row['last_at'] = max((r.responded_at or r.created_at for r in history), default=None)
        row['last_sent_at'] = max((sent_at[r.id] for r in history if r.id in sent_at), default=None)
        row['message_dates'] = [at for r in history for at in message_dates.get(r.id,[])]
        row['delivery_key'] = delivery_key(row['status'], row['sent'], row['delivered'],
                                           any(r.whatsapp_status == 'Read' for r in history))
        row['delivery_label'] = {'read':'Read', 'delivered':'Delivered', 'sent':'Sent',
            'failed':'Failed', 'held':'Held — not sent', 'review':'Sending / interrupted attempt — needs checking', 'not_sent':'Not sent'}[row['delivery_key']]
    return sorted((row for row in groups.values() if include_uncontacted or row['history']),
                  key=lambda r: (r['sent'], r['delivery_key'] in {'failed', 'held'}, -r['policy'].id))



@dataclass(slots=True)
class ContactSummary:
    latest_policy_id: int
    display_policy_id: int | None = None
    latest_status: str | None = None
    sent: bool = False
    delivered: bool = False
    read: bool = False
    failed: bool = False
    opted_out: bool = False
    callback: bool = False
    matches_search: bool = False
    matches_campaign: bool = False
    matches_dates: bool = False

    @property
    def delivery(self):
        return delivery_key(self.latest_status, self.sent, self.delivered, self.read)


def contact_key(policy_id, phone):
    return normalize_phone(phone) or policy_id


def _directory_contacts(policies, batch_policies, values):
    batch_keys = None
    if batch_policies is not None:
        batch_keys = {}
        batch_rows = batch_policies.with_entities(LapsedPolicy.id, LapsedPolicy.cell_number).order_by(
            LapsedPolicy.id.desc())
        for policy_id, phone in batch_rows.yield_per(500):
            batch_keys.setdefault(contact_key(policy_id, phone), policy_id)

    phrase = (values.get('q') or '').strip().lower()
    source, franchise, branch = (values.get(key, '') for key in ('source_file', 'franchise', 'branch'))
    filters = bool(source or franchise or branch)
    contacts, by_policy = {}, {}
    branches, source_files, franchises = set(), set(), set()
    phone_hashes, email_hashes = set(), set()
    for phone, email in db.session.query(ContactSuppression.phone_hash, ContactSuppression.email_hash).yield_per(500):
        if phone:
            phone_hashes.add(phone)
        if email:
            email_hashes.add(email)

    fields = [LapsedPolicy.id, LapsedPolicy.cell_number, LapsedPolicy.email_address, LapsedPolicy.branch,
              LapsedPolicy.franchise, LapsedPolicy.company_name, LapsedPolicy.source_filename,
              LapsedPolicy.recovery_status]
    if phrase:
        fields.extend([LapsedPolicy.initials, LapsedPolicy.surname, LapsedPolicy.policy_number, LapsedPolicy.address])
    for policy in policies.with_entities(*fields).order_by(LapsedPolicy.id.desc()).yield_per(500):
        key = contact_key(policy.id, policy.cell_number)
        if batch_keys is not None and key not in batch_keys:
            continue
        holder = policy.franchise or policy.company_name or policy.branch or ''
        if policy.branch:
            branches.add(policy.branch)
        if holder:
            franchises.add(holder)
        if policy.source_filename:
            source_files.add(policy.source_filename)

        entry = contacts.get(key)
        if entry is None:
            entry = contacts[key] = ContactSummary(policy.id)
        by_policy[policy.id] = key
        if not entry.opted_out:
            entry.opted_out = (policy.recovery_status == 'Opted Out' or
                contact_hash(normalize_phone(policy.cell_number)) in phone_hashes or
                contact_hash(normalize_email(policy.email_address)) in email_hashes)
        if entry.display_policy_id is None:
            if not filters:
                entry.display_policy_id = batch_keys[key] if batch_keys is not None else policy.id
            elif ((not source or policy.source_filename == source) and (not franchise or holder == franchise)
                  and (not branch or policy.branch == branch)):
                entry.display_policy_id = policy.id
        if phrase and not entry.matches_search:
            entry.matches_search = ((isinstance(key, str) and phrase in key) or
                phrase in (f"{policy.initials or ''} {policy.surname or ''} {policy.cell_number or ''} "
                           f"{policy.policy_number or ''} {policy.address or ''}").lower())

    options = dict(branches=sorted(branches), source_files=sorted(source_files), franchises=sorted(franchises))
    return contacts, by_policy, options


def _summarize_directory_history(policies, campaign_ids, contacts, by_policy, campaign_id, low, high):
    scope = policies.with_entities(LapsedPolicy.id).order_by(None).subquery()
    preference = ContactCommunicationPreference
    opted_out = db.session.query(preference.lapsed_policy_id).join(scope, scope.c.id == preference.lapsed_policy_id).filter(
        db.or_(preference.opted_out_all.is_(True), preference.whatsapp_allowed.is_(False)))
    for policy_id, in opted_out.yield_per(500):
        if policy_id in by_policy:
            contacts[by_policy[policy_id]].opted_out = True

    recipient = CampaignRecipient
    fields = [recipient.lapsed_policy_id, recipient.whatsapp_status, recipient.response_type, recipient.campaign_id]
    if low or high:
        dates = db.session.query(WhatsAppMessage.id).filter(
            WhatsAppMessage.campaign_recipient_id == recipient.id, WhatsAppMessage.direction == 'outbound')
        if low:
            dates = dates.filter(WhatsAppMessage.created_at >= low)
        if high:
            dates = dates.filter(WhatsAppMessage.created_at < high)
        fields.append(dates.exists().label('in_date_range'))
    history = db.session.query(*fields).select_from(recipient).join(scope, scope.c.id == recipient.lapsed_policy_id).filter(
        recipient.campaign_id.in_(campaign_ids)
    ).order_by(recipient.created_at.desc(), recipient.id.desc())
    for result in history.yield_per(500):
        if result.lapsed_policy_id not in by_policy:
            continue
        entry = contacts[by_policy[result.lapsed_policy_id]]
        entry.matches_campaign |= result.campaign_id == campaign_id
        status = result.whatsapp_status
        if status not in HISTORY:
            continue
        if entry.latest_status is None:
            entry.latest_status = status
        entry.sent |= status in SENT
        entry.delivered |= status in {'Delivered', 'Read'}
        entry.read |= status == 'Read'
        entry.failed |= status in FAILED_OR_HELD
        entry.callback |= result.response_type == 'callback'
        entry.opted_out |= result.response_type == 'opt_out'
        if low or high:
            entry.matches_dates |= bool(result.in_date_range)


def directory_page(policies, campaign_ids, values, *, batch_policies=None):
    """Stream scalar summaries, then load detailed histories for only the current page."""
    low, high = message_date_range(values)
    status = values.get('status', '')
    checks = {'sent': lambda row: row.sent, 'delivered': lambda row: row.delivered,
              'read': lambda row: row.read, 'callback': lambda row: row.callback,
              'opted_out': lambda row: row.opted_out, 'not_sent': lambda row: not row.sent,
              'undelivered': lambda row: row.delivery in {'sent', 'failed', 'held'},
              'failed': lambda row: row.failed}
    if status and status not in checks:
        raise ValueError('Choose a valid message status.')
    campaign_id = values.get('campaign', type=int)
    has_search = bool((values.get('q') or '').strip())
    contacts, by_policy, options = _directory_contacts(policies, batch_policies, values)
    if contacts:
        _summarize_directory_history(policies, campaign_ids, contacts, by_policy, campaign_id, low, high)

    counts = dict(total=0, sent=0, not_sent=0, failed=0, delivered=0, opted_out=0)
    matches = []
    for key, row in contacts.items():
        if (row.display_policy_id is None or (has_search and not row.matches_search) or
            (campaign_id and not row.matches_campaign) or ((low or high) and not row.matches_dates)):
            continue
        counts['total'] += 1
        counts['sent'] += row.sent
        counts['not_sent'] += not row.sent
        counts['failed'] += row.delivery in {'failed', 'held'}
        counts['delivered'] += row.delivered
        counts['opted_out'] += row.opted_out
        if not status or checks[status](row):
            matches.append(key)
    matches.sort(key=lambda key: (contacts[key].sent, contacts[key].delivery in {'failed', 'held'},
                                  -contacts[key].latest_policy_id))
    total = len(matches)
    pages = max(1, (total + 49) // 50)
    page = min(pages, max(1, values.get('page', 1, type=int) or 1))
    page_keys = matches[(page - 1) * 50:page * 50]
    rows = []
    if page_keys:
        keys = set(page_keys)
        policy_ids = [policy_id for policy_id, key in by_policy.items() if key in keys]
        page_policies = policies.filter(LapsedPolicy.id.in_(policy_ids)).order_by(LapsedPolicy.id.desc()).all()
        details = {(row['phone'] or row['policy'].id): row
                   for row in client_rows(page_policies, campaign_ids, include_uncontacted=True,
                                          include_message_dates=False)}
        page_by_id = {policy.id: policy for policy in page_policies}
        for key in page_keys:
            row = details.get(key)
            if row is None or contacts[key].display_policy_id not in page_by_id:
                continue
            row['policy'] = page_by_id[contacts[key].display_policy_id]
            row['name'] = f"{row['policy'].initials or ''} {row['policy'].surname or ''}".strip() or 'Unnamed client'
            rows.append(row)
    return dict(rows=rows, summary=counts, total=total, page=page, pages=pages, **options)


def directory_delivery(policies):
    rows = client_rows(policies, [c.id for c in visible_campaigns()], include_uncontacted=True)
    return {policy.id: row for row in rows for policy in row['policies']}

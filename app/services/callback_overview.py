"""Branch and saved-file context for the callback worklist."""
from collections import defaultdict
from flask import abort, request, url_for
from app import db
from app.models import ClientImportBatch, ClientImportRow, ClientApplication, LapsedPolicy, CallbackEmailNotification, CallbackEmailCopy, CampaignRecipient, ContactCommunicationPreference, ContactSuppression, CompanyGroupState
from app.services.client_directory import scoped_policies, scoped_applications, new_joining
from app.services.whatsapp_clients import SENT, contact_key, visible_campaigns
from app.services.communication_service import contact_hash, normalize_email, normalize_phone
from app.services.campaign_schedule import schedule_sast


def overview_context():
    base = scoped_policies()
    policies = base.with_entities(
        LapsedPolicy.id, LapsedPolicy.branch, LapsedPolicy.source_filename, LapsedPolicy.recovery_status,
        LapsedPolicy.cell_number, LapsedPolicy.email_address,
    ).all()
    by_id = {p.id: p for p in policies}
    application_scope = scoped_applications()
    applications = application_scope.with_entities(
        ClientApplication.id, ClientApplication.lapsed_policy_id, ClientApplication.branch,
        ClientApplication.signed_at, ClientApplication.status, ClientApplication.application_ref,
        ClientApplication.first_names, ClientApplication.surname, ClientApplication.cell_number,
    ).all()
    branches = sorted({p.branch or 'Unassigned' for p in policies} |
                      {a.branch or 'Unassigned' for a in applications})
    branch = request.args.get('branch', '').strip()
    selected_import = request.args.get('import', '')
    if branch and branch not in branches:
        abort(403)
    memberships = defaultdict(set)
    for batch_id, policy_id in db.session.query(ClientImportRow.batch_id, ClientImportRow.policy_id).filter(
            ClientImportRow.policy_id.in_(base.with_entities(LapsedPolicy.id))).yield_per(500):
        memberships[batch_id].add(policy_id)
    batches = db.session.query(ClientImportBatch.id, ClientImportBatch.filename, ClientImportBatch.description,
        ClientImportBatch.created_at, CompanyGroupState.branch).select_from(ClientImportBatch).join(
        CompanyGroupState, CompanyGroupState.id == ClientImportBatch.company_id
    ).filter(ClientImportBatch.id.in_(memberships)).order_by(ClientImportBatch.id.desc()).all()
    choices = []
    covered = set()
    for batch in batches:
        ids = memberships[batch.id]
        covered.update(ids)
        ids = {pid for pid in ids if not branch or (by_id[pid].branch or 'Unassigned') == branch}
        if ids:
            choices.append(dict(key=f'batch:{batch.id}', label=batch.description, filename=batch.filename,
                branch=batch.branch, date=batch.created_at, ids=ids, url=url_for('sales.clients', batch_id=batch.id)))
    legacy = defaultdict(set)
    for p in policies:
        if p.id not in covered and (not branch or (p.branch or 'Unassigned') == branch):
            legacy[p.source_filename or ''].add(p.id)
    for filename, ids in sorted(legacy.items()):
        choices.append(dict(key='file:' + filename, label=filename or 'Clients without an import file',
            filename=filename, branch=', '.join(sorted({by_id[pid].branch or 'Unassigned' for pid in ids})), date=None, ids=ids,
            url=url_for('client_files.index', source_file=filename, branch=branch) if filename else None))
    chosen = next((c for c in choices if c['key'] == selected_import), None)
    if selected_import and not chosen:
        abort(404)
    ids = chosen['ids'] if chosen else {p.id for p in policies if not branch or (p.branch or 'Unassigned') == branch}
    applications = [a for a in applications if
        (a.lapsed_policy_id in ids if selected_import else
         (not branch or ((by_id[a.lapsed_policy_id].branch if a.lapsed_policy_id in by_id else a.branch) or 'Unassigned') == branch))]
    completed = {policy_id for policy_id, in application_scope.filter(new_joining()).with_entities(
        ClientApplication.lapsed_policy_id).yield_per(500) if policy_id in ids}
    sent, removed = progress_sets(base, ids, by_id)
    completed.difference_update(removed)
    app_ids = {a.lapsed_policy_id for a in applications}
    summaries = []
    for item in choices:
        if selected_import and item is not chosen:
            continue
        members = item['ids']
        summaries.append(dict(item, clients=len(members),
            sent=len(members & sent),
            callbacks=sum(by_id[pid].recovery_status == 'Callback' for pid in members),
            applications=len(members & app_ids),
            completed=len(members & completed),
            removed=len(members & removed)))
    return dict(branches=branches, selected_branch=branch, import_choices=choices,
                selected_import=selected_import, import_summaries=summaries,
                schedule_sast=schedule_sast), ids, applications, by_id


def progress_sets(base, ids, by_id):
    if not ids:
        return set(), set()
    scope = base.with_entities(LapsedPolicy.id).subquery()
    campaign_ids = [campaign.id for campaign in visible_campaigns(summaries=True)]
    delivered = db.session.query(CampaignRecipient.lapsed_policy_id).join(
        scope, scope.c.id == CampaignRecipient.lapsed_policy_id).filter(
        CampaignRecipient.campaign_id.in_(campaign_ids), CampaignRecipient.whatsapp_status.in_(SENT)).distinct()
    sent_keys = {contact_key(pid, by_id[pid].cell_number) for pid, in delivered.yield_per(500) if pid in ids}
    sent = {pid for pid in ids if contact_key(pid, by_id[pid].cell_number) in sent_keys}

    preference = ContactCommunicationPreference
    blocked = db.session.query(preference.lapsed_policy_id).join(scope, scope.c.id == preference.lapsed_policy_id).filter(
        db.or_(preference.opted_out_all.is_(True), preference.whatsapp_allowed.is_(False)))
    removed = {pid for pid, in blocked.yield_per(500) if pid in ids}
    phone_hashes, email_hashes = set(), set()
    for phone, email in db.session.query(ContactSuppression.phone_hash, ContactSuppression.email_hash).yield_per(500):
        if phone:
            phone_hashes.add(phone)
        if email:
            email_hashes.add(email)
    for pid in ids - removed:
        policy = by_id[pid]
        if (policy.recovery_status == 'Opted Out' or
            (phone_hashes and contact_hash(normalize_phone(policy.cell_number)) in phone_hashes) or
            (email_hashes and contact_hash(normalize_email(policy.email_address)) in email_hashes)):
            removed.add(pid)
    return sent, removed


def callback_email_statuses(ids):
    if not ids:
        return {}
    callback_emails={}
    for pid,status,address,notice_id in db.session.query(CampaignRecipient.lapsed_policy_id,CallbackEmailNotification.status,
            CallbackEmailNotification.to_email,CallbackEmailNotification.id).join(CallbackEmailNotification,CallbackEmailNotification.recipient_id==CampaignRecipient.id
            ).filter(CampaignRecipient.lapsed_policy_id.in_(ids)).order_by(CallbackEmailNotification.id):
        callback_emails[pid]=dict(status=status,email=address,notice_id=notice_id,copies=[])
    for pid,notice_id,status,address in db.session.query(CampaignRecipient.lapsed_policy_id,CallbackEmailNotification.id,
            CallbackEmailCopy.status,CallbackEmailCopy.to_email).join(CallbackEmailNotification,
            CallbackEmailNotification.recipient_id==CampaignRecipient.id).join(CallbackEmailCopy,
            CallbackEmailCopy.notification_id==CallbackEmailNotification.id).filter(CampaignRecipient.lapsed_policy_id.in_(ids)):
        if pid in callback_emails and callback_emails[pid]['notice_id']==notice_id:
            callback_emails[pid]['copies'].append(dict(status=status,email=address))
    return callback_emails

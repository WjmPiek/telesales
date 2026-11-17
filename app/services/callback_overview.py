"""Branch and saved-file context for the callback worklist."""
from collections import defaultdict
from flask import abort, request, url_for
from app import db
from app.models import ClientImportBatch, ClientImportRow, ClientApplication, LapsedPolicy
from app.services.client_directory import scoped_policies, scoped_applications, process_expressions
from app.services.whatsapp_clients import directory_delivery
from app.services.campaign_schedule import schedule_sast


def overview_context():
    base = scoped_policies()
    policies = base.all()
    by_id = {p.id: p for p in policies}
    applications = scoped_applications().all()
    branches = sorted({p.branch or 'Unassigned' for p in policies} |
                      {a.branch or 'Unassigned' for a in applications})
    branch = request.args.get('branch', '').strip()
    selected_import = request.args.get('import', '')
    if branch and branch not in branches:
        abort(403)
    memberships = defaultdict(set)
    for batch_id, policy_id in db.session.query(ClientImportRow.batch_id, ClientImportRow.policy_id).filter(
            ClientImportRow.policy_id.in_(by_id)):
        memberships[batch_id].add(policy_id)
    batches = ClientImportBatch.query.filter(ClientImportBatch.id.in_(memberships)).order_by(ClientImportBatch.id.desc()).all()
    choices = []
    covered = set()
    for batch in batches:
        ids = memberships[batch.id]
        covered.update(ids)
        ids = {pid for pid in ids if not branch or (by_id[pid].branch or 'Unassigned') == branch}
        if ids:
            choices.append(dict(key=f'batch:{batch.id}', label=batch.description, filename=batch.filename,
                branch=batch.company.branch, date=batch.created_at, ids=ids, url=url_for('sales.clients', batch_id=batch.id)))
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
    states = dict(base.with_entities(LapsedPolicy.id, process_expressions(base)).all())
    delivery = directory_delivery([by_id[pid] for pid in ids])
    app_ids = {a.lapsed_policy_id for a in applications}
    summaries = []
    for item in choices:
        if selected_import and item is not chosen:
            continue
        members = item['ids']
        summaries.append(dict(item, clients=len(members),
            sent=sum(bool(delivery.get(pid, {}).get('sent')) for pid in members),
            callbacks=sum(by_id[pid].recovery_status == 'Callback' for pid in members),
            applications=len(members & app_ids),
            completed=sum(states.get(pid) == 'completed' for pid in members),
            removed=sum(states.get(pid) == 'removed' for pid in members)))
    return dict(branches=branches, selected_branch=branch, import_choices=choices,
                selected_import=selected_import, import_summaries=summaries,
                schedule_sast=schedule_sast), ids, applications, by_id

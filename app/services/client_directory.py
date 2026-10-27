"""Shared franchise, date and process filters for browsing and campaigns."""
import calendar
from datetime import date, datetime, time, timedelta

from flask import abort
from sqlalchemy import and_, or_, not_, case, func, extract, text

from app import db
from app.models import (LapsedPolicy, ClientApplication, TelesalesScriptSession,
                        ContactCommunicationPreference, ContactSuppression)
from app.services.branch_access import scope_by_branch
from app.services.communication_service import contact_hash, normalize_phone, normalize_email

PROCESS_LABELS = {
    'completed': 'Completed new joining', 'removed': 'Requested removal / opted out',
    'in_progress': 'Application in progress', 'not_started': 'Not started', 'other': 'Closed / other',
}
DATE_LABELS = {'receipt': 'Last receipt date', 'imported': 'Import date',
               'application': 'Application started', 'joined': 'New policy start date'}


def scoped_policies():
    return scope_by_branch(LapsedPolicy.query, LapsedPolicy, agent_col=LapsedPolicy.assigned_agent_id)


def franchise_column():
    return func.coalesce(func.nullif(LapsedPolicy.franchise, ''), func.nullif(LapsedPolicy.company_name, ''), LapsedPolicy.branch)


def scoped_applications():
    return scope_by_branch(ClientApplication.query, ClientApplication, agent_col=ClientApplication.agent_id)


def new_joining():
    return and_(ClientApplication.status == 'Active', ClientApplication.application_type.ilike('%new%'))


def process_expressions(base):
    linked_apps = scoped_applications().filter(ClientApplication.lapsed_policy_id == LapsedPolicy.id)
    completed = linked_apps.filter(new_joining()).exists()
    active_app = linked_apps.filter(ClientApplication.status.notin_(
        ['Active', 'Rejected', 'QA Rejected', 'Compliance Rejected', 'Cancelled', 'Closed', 'Archived'])).exists()
    scripts = scope_by_branch(TelesalesScriptSession.query, TelesalesScriptSession,
                              agent_col=TelesalesScriptSession.agent_id)
    active_script = scripts.filter(TelesalesScriptSession.lapsed_policy_id == LapsedPolicy.id,
                                   TelesalesScriptSession.status == 'In Progress').exists()
    preferences = ContactCommunicationPreference.query.filter(
        ContactCommunicationPreference.lapsed_policy_id == LapsedPolicy.id,
        or_(ContactCommunicationPreference.opted_out_all.is_(True),
            ContactCommunicationPreference.whatsapp_allowed.is_(False))).exists()
    # Match durable opt-outs even after a reset/re-import changed the policy ID.
    hashes = ContactSuppression.query.with_entities(ContactSuppression.phone_hash, ContactSuppression.email_hash).all()
    phones = {row[0] for row in hashes if row[0]}
    emails = {row[1] for row in hashes if row[1]}
    suppressed_ids = [row.id for row in base.with_entities(LapsedPolicy.id, LapsedPolicy.cell_number, LapsedPolicy.email_address).all()
                      if contact_hash(normalize_phone(row.cell_number)) in phones
                      or contact_hash(normalize_email(row.email_address)) in emails] if hashes else []
    removed = or_(LapsedPolicy.recovery_status == 'Opted Out', preferences, LapsedPolicy.id.in_(suppressed_ids))
    in_progress = or_(active_app, active_script)
    not_started = and_(not_(linked_apps.exists()), not_(active_script),
                       LapsedPolicy.recovery_status.in_(['New', 'Imported', 'Called', 'No Answer', 'Callback', 'Interested', 'Suspense']))
    # Removal takes precedence so an opted-out client is never presented as an active prospect.
    state = case((removed, 'removed'), (completed, 'completed'), (in_progress, 'in_progress'),
                 (not_started, 'not_started'), else_='other')
    return state


def filter_values(values):
    selected = {key: (values.get(key) or '').strip() for key in
                ('q', 'franchise', 'year', 'month', 'date_from', 'date_to', 'process', 'branch', 'status', 'lead_type', 'registry', 'screening')}
    from app.services.marketing_screening import REGISTRIES, RESULTS
    if (selected['registry'] and selected['registry'] not in REGISTRIES) or (selected['screening'] and selected['screening'] not in RESULTS):
        abort(400, 'Choose a valid screening register and result.')
    selected['date_basis'] = values.get('date_basis') or 'receipt'
    if selected['date_basis'] not in DATE_LABELS or (selected['process'] and selected['process'] not in PROCESS_LABELS):
        abort(400, 'Choose a valid date basis and process status.')
    for key, low, high in [('year', 1901, 9998), ('month', 1, 12)]:
        if selected[key]:
            try:
                number = int(selected[key])
                if not low <= number <= high:
                    raise ValueError()
            except ValueError:
                abort(400, 'Choose a valid year and month.')
    try:
        start = date.fromisoformat(selected['date_from']) if selected['date_from'] else None
        end = date.fromisoformat(selected['date_to']) if selected['date_to'] else None
        if start and end and start > end:
            raise ValueError()
    except ValueError:
        abort(400, 'Enter a valid date range with the start before the end.')
    return selected


def date_conditions(column, selected, timestamp=False):
    local = column
    if timestamp:
        local = func.datetime(column, '+2 hours') if db.engine.dialect.name == 'sqlite' else column + text("INTERVAL '2 hours'")
    conditions = []
    for part in ('year', 'month'):
        if selected[part]:
            conditions.append(extract(part, local) == int(selected[part]))
    if selected['date_from']:
        start = date.fromisoformat(selected['date_from'])
        conditions.append(column >= (datetime.combine(start, time.min) - timedelta(hours=2) if timestamp else start))
    if selected['date_to']:
        end = date.fromisoformat(selected['date_to'])
        if timestamp:
            conditions.append(column < datetime.combine(end + timedelta(days=1), time.min) - timedelta(hours=2))
        else:
            conditions.append(column <= end)
    return conditions


def filter_policy_query(base, values, include_process=True):
    selected = filter_values(values)
    query = base
    if selected['q']:
        escaped = selected['q'].replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
        like = '%' + escaped + '%'
        full_name = func.coalesce(LapsedPolicy.initials, '') + ' ' + func.coalesce(LapsedPolicy.surname, '')
        query = query.filter(or_(*[column.ilike(like, escape='\\') for column in
                                  (full_name, LapsedPolicy.policy_number, LapsedPolicy.cell_number,
                                   LapsedPolicy.id_number, LapsedPolicy.email_address, LapsedPolicy.address)]))
    if selected['franchise']:
        query = query.filter(franchise_column() == selected['franchise'])
    for key, column in [('branch', LapsedPolicy.branch), ('status', LapsedPolicy.recovery_status), ('lead_type', LapsedPolicy.lead_type)]:
        if selected[key]:
            query = query.filter(column == selected[key])
    if any(selected[key] for key in ('year', 'month', 'date_from', 'date_to')):
        basis = selected['date_basis']
        if basis in {'application', 'joined'}:
            column = ClientApplication.created_at if basis == 'application' else ClientApplication.inception_date
            aq = scoped_applications().filter(ClientApplication.lapsed_policy_id == LapsedPolicy.id)
            if basis == 'joined':
                aq = aq.filter(new_joining())
            query = query.filter(aq.filter(*date_conditions(column, selected, timestamp=basis == 'application')).exists())
        else:
            column = LapsedPolicy.imported_at if basis == 'imported' else LapsedPolicy.last_date_paid
            query = query.filter(*date_conditions(column, selected, timestamp=basis == 'imported'))
    if include_process and selected['process']:
        query = query.filter(process_expressions(base) == selected['process'])
    if selected['screening']:
        from app.services.marketing_screening import screening_condition
        query = query.filter(screening_condition(selected['registry'], selected['screening']))
    return query


def filter_context(base, values):
    selected = filter_values(values)
    franchises = [row[0] for row in base.with_entities(franchise_column()).filter(
        franchise_column().isnot(None), franchise_column() != '').distinct().order_by(franchise_column()).all()]
    basis = selected['date_basis']
    if basis in {'application', 'joined'}:
        column = ClientApplication.created_at if basis == 'application' else ClientApplication.inception_date
        aq = scoped_applications().filter(ClientApplication.lapsed_policy_id.in_(base.with_entities(LapsedPolicy.id)))
        if basis == 'joined':
            aq = aq.filter(new_joining())
        dates = [row[0] for row in aq.with_entities(column).distinct().all()]
    else:
        column = LapsedPolicy.imported_at if basis == 'imported' else LapsedPolicy.last_date_paid
        dates = [row[0] for row in base.with_entities(column).distinct().all()]
    if basis in {'imported', 'application'}:
        dates = [value + timedelta(hours=2) for value in dates if value]
    years = sorted({value.year for value in dates if value and value.year > 1900}, reverse=True)
    if selected['year']:
        years = sorted(set(years) | {int(selected['year'])}, reverse=True)
    from app.services.marketing_screening import REGISTRIES, RESULTS
    return dict(screening_registries=REGISTRIES, screening_results=RESULTS,
                directory_filters=selected, directory_franchises=franchises, directory_years=years,
                directory_months=[(number, calendar.month_name[number]) for number in range(1, 13)],
                process_labels=PROCESS_LABELS, date_labels=DATE_LABELS)


def directory_context(values):
    base = scoped_policies()
    context = filter_context(base, values)
    state = process_expressions(base)
    counts_query = filter_policy_query(base, values, include_process=False)
    counts = dict(counts_query.with_entities(state, func.count(LapsedPolicy.id)).group_by(state).all())
    query = filter_policy_query(base, values)
    total = query.count()
    try:
        page = max(1, int(values.get('page', 1)))
    except ValueError:
        abort(400, 'Invalid page number.')
    pages = max(1, (total + 49) // 50)
    page = min(page, pages)
    records = query.add_columns(state.label('process')).order_by(LapsedPolicy.imported_at.desc(), LapsedPolicy.id.desc()).offset((page-1)*50).limit(50).all()
    context.update(directory_rows=records, directory_counts=counts, directory_total=total,
                   directory_page=page, directory_pages=pages)
    from app.services.marketing_screening import REGISTRIES, latest_results, result_status
    summaries = {}
    for policy, _ in records:
        latest = latest_results(policy.cell_number)
        summaries[policy.id] = [(label, result_status(latest.get(key)),
                                 latest[key].checked_at if key in latest else None)
                                for key, label in REGISTRIES.items()]
    context['screening_summaries'] = summaries
    return context

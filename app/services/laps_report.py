"""Import franchise laps reports without requiring a member-cover template."""
import json
import re
from datetime import date, datetime
from pathlib import PurePosixPath

from app import db
from app.models import AuditLog, LapsedPolicy

REPORT_HEADERS = {'policy', 'initialsurname', 'address', 'cell', 'lastreceipt'}


def header(value):
    return re.sub(r'[^a-z0-9]', '', str(value or '').casefold())


def text(value):
    if value is None:
        return ''
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return str(value).strip()


def franchise_from_filename(filename):
    name = PurePosixPath(filename.replace('\\', '/')).stem
    name = re.split(r'\s+laps(?:ed|e)?\b', name, maxsplit=1, flags=re.I)[0].strip(' _-')
    if not name or len(name) > 120:
        raise ValueError('Use a filename beginning with the franchise name, for example Northcliff Laps Report.xlsx.')
    return name


def receipt_date(value, epoch):
    from openpyxl.utils.datetime import from_excel
    if value is None or text(value).lower() in {'', 'none', 'none yet', '0', '-'}:
        return None
    if isinstance(value, (int, float)):
        value = from_excel(value, epoch)
    if isinstance(value, datetime):
        result = value.date()
    elif isinstance(value, date):
        result = value
    else:
        result = None
        for pattern in ('%Y-%m-%d', '%d/%m/%Y', '%d-%m-%Y', '%Y/%m/%d'):
            try:
                result = datetime.strptime(text(value)[:10], pattern).date()
                break
            except ValueError:
                continue
        if result is None:
            raise ValueError('Last Receipt must be an Excel date or a date such as 2023-06-22 or 22/06/2023.')
    # Old accounting exports use January 1900 as a missing-date sentinel.
    return result if result.year > 1900 else None


def parse_report(workbook, filename):
    from app.routes.prospects import prospect_phone
    franchise = franchise_from_filename(filename)
    sheet = workbook.active
    iterator = sheet.iter_rows(values_only=True)
    original_headers = next(iterator, ())
    headers = [header(value) for value in original_headers]
    named_headers = [name for name in headers if name]
    if len(set(named_headers)) != len(named_headers) or not REPORT_HEADERS.issubset(named_headers):
        raise ValueError('Expected Policy #, Initial. Surname, Address, Cell# and Last Receipt columns.')
    rows = []
    seen = set()
    for number, values in enumerate(iterator, 2):
        if number > 10001:
            raise ValueError('Import at most 10,000 clients per file.')
        if not any(text(value) for value in values):
            continue
        data = dict(zip(headers, values))
        policy = text(data.get('policy'))
        full_name = text(data.get('initialsurname'))
        if not policy or not full_name or len(policy) > 80:
            raise ValueError(f'Row {number}: enter a policy number and client initials/surname.')
        initials, _, surname = full_name.partition(' ')
        if not surname or len(initials) > 50 or len(surname) > 120 or len(text(data.get('receipt'))) > 80:
            raise ValueError(f'Row {number}: check the initials, surname and receipt number lengths.')
        if policy.casefold() in seen:
            raise ValueError(f'Row {number}: the policy number is duplicated in this report.')
        seen.add(policy.casefold())
        try:
            last_paid = receipt_date(data.get('lastreceipt'), workbook.epoch)
        except (ValueError, TypeError, OverflowError) as exc:
            raise ValueError(f'Row {number}: invalid Last Receipt date.') from exc
        source = {str(key): value.isoformat() if isinstance(value, (date, datetime)) else value
                  for key, value in zip(original_headers, values)}
        rows.append(dict(policy_number=policy, initials=initials, surname=surname,
                         cell_number=prospect_phone(data.get('cell')) or text(data.get('cell'))[:50],
                         valid_mobile=bool(prospect_phone(data.get('cell'))),
                         home_tel=text(data.get('phone'))[:50], address=text(data.get('address')),
                         last_date_paid=last_paid, receipt_number=text(data.get('receipt')),
                         comments=text(data.get('comment')), source_row_json=json.dumps(source, ensure_ascii=False)))
    if not rows:
        raise ValueError('The report has no client rows.')
    return franchise, rows


def report_company(franchise):
    """Northcliff reports go to Northcliff; every other franchise goes to Brokers."""
    from app.services.franchise_routing import destination_company, is_northcliff
    return destination_company(is_northcliff(franchise))


def report_group_repairs():
    """Preview only unambiguous, unused report groups created in the uploader's branch."""
    from app.models import CompanyGroupState, ClientApplication, CampaignRecipient
    plans = []
    for source in CompanyGroupState.query.all():
        rows = LapsedPolicy.query.filter_by(company_id=source.id).all()
        if not rows or any(p.source_type != 'laps_report' or header(p.franchise) != header(source.company_name) for p in rows):
            continue
        key = header(source.company_name)
        targets = [c for c in CompanyGroupState.query.filter_by(status='Active').all()
                   if c.id != source.id and header(c.branch) == key and
                   header(c.company_name) in {key, 'martinsfunerals' + key, 'martins' + key}]
        if len(targets) != 1:
            continue
        ids = [p.id for p in rows]
        if ClientApplication.query.filter(ClientApplication.lapsed_policy_id.in_(ids)).first() or CampaignRecipient.query.filter(CampaignRecipient.lapsed_policy_id.in_(ids)).first():
            continue
        target = targets[0]
        existing = {p.policy_number.casefold() for p in LapsedPolicy.query.filter_by(company_id=target.id).all() if p.policy_number}
        if any(p.policy_number and p.policy_number.casefold() in existing for p in rows):
            continue
        plans.append((source, target, rows))
    return plans


def import_report(workbook, filename, user):
    from app.services.communication_service import is_suppressed, preference_for
    franchise, rows = parse_report(workbook, filename)
    company = report_company(franchise)
    branch = company.branch
    role = user.role.name.lower().replace('_', ' ') if user.role else ''
    if role not in {'admin', 'super admin'} and branch != (user.branch or '').strip():
        raise ValueError('This report belongs to another branch. Ask an administrator to import it.')
    if company.status != 'Active':
        raise ValueError('This franchise is suspended or deleted. Reactivate it before importing.')
    existing = {p.policy_number.casefold(): p for p in LapsedPolicy.query.filter_by(company_id=company.id).filter(
        db.func.lower(LapsedPolicy.franchise) == franchise.lower()).all() if p.policy_number}
    created = updated = missing_mobile = 0
    for data in rows:
        valid_mobile = data.pop('valid_mobile')
        policy = existing.get(data['policy_number'].casefold())
        if policy is None:
            policy = LapsedPolicy(company_id=company.id, company_name=company.company_name, franchise=franchise,
                                  branch=branch, lead_type='policy', assigned_agent_id=user.id,
                                  recovery_status='Imported', next_action_date=None)
            db.session.add(policy)
            existing[data['policy_number'].casefold()] = policy
            created += 1
        else:
            updated += 1
        # Keep workflow state, IDs, assignments, consent and application links on re-import.
        for field, value in data.items():
            if field != 'comments':
                setattr(policy, field, value)
        if data['comments'] and data['comments'] not in (policy.comments or '').splitlines():
            policy.comments = '\n'.join(filter(None, [policy.comments, data['comments']]))
        policy.source_type = 'laps_report'
        policy.source_filename = PurePosixPath(filename.replace('\\', '/')).name[:255]
        db.session.flush()
        # Re-import refreshes source history without reverting submitted form details.
        from app.models import ClientApplication, ApplicationJourney
        from app.services.client_merge import merge_application_details
        submitted = ClientApplication.query.join(ApplicationJourney).filter(
            ClientApplication.lapsed_policy_id == policy.id, ApplicationJourney.ready.is_(True),
        ).order_by(ClientApplication.updated_at.desc(), ClientApplication.id.desc()).first()
        if submitted:
            merge_application_details(submitted)
        if is_suppressed(policy) or preference_for(policy).opted_out_all:
            pref = preference_for(policy)
            pref.opted_out_all = True
            pref.whatsapp_allowed = False
            pref.telephone_allowed = False
            pref.email_allowed = False
            policy.recovery_status = 'Opted Out'
            policy.next_action_date = None
        elif not valid_mobile:
            missing_mobile += 1
            if policy.recovery_status in {'New', 'Imported', 'Suspense'}:
                policy.recovery_status = 'Suspense'
                policy.suspense_reason = 'Valid mobile number'
        elif policy.recovery_status == 'Suspense' and policy.suspense_reason in {'Valid mobile number', 'ID number', 'ID number, contact number'}:
            policy.recovery_status = 'Imported'
            policy.suspense_reason = None
    db.session.add(AuditLog(user_id=user.id, action='LAPS_REPORT_IMPORT', entity_type='CompanyGroupState',
                           entity_id=str(company.id), details=f'{created} created; {updated} updated; {missing_mobile} missing valid mobile numbers.'))
    db.session.commit()
    return franchise, created, updated, missing_mobile


def ensure_report_columns():
    from sqlalchemy import inspect, text as sql
    inspector = inspect(db.engine)
    if not inspector.has_table('lapsed_policies'):
        return
    columns = {column['name'] for column in inspector.get_columns('lapsed_policies')}
    clause = 'IF NOT EXISTS ' if db.engine.dialect.name == 'postgresql' else ''
    with db.engine.begin() as conn:
        for name, kind in [('source_type', 'VARCHAR(40)'), ('source_filename', 'VARCHAR(255)'),
                           ('receipt_number', 'VARCHAR(80)'), ('source_row_json', 'TEXT')]:
            if name not in columns:
                conn.execute(sql(f'ALTER TABLE lapsed_policies ADD COLUMN {clause}{name} {kind}'))

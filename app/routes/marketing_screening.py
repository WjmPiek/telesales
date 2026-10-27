import csv
import io
import json
import secrets
from datetime import datetime, timedelta

from flask import Blueprint, abort, flash, redirect, render_template, request, session, url_for
from flask_login import current_user, login_required

from app import db
from app.models import AuditLog, MarketingScreening, SystemSetting
from app.security import permission_required, is_admin_user, is_branch_manager_user
from app.services.client_directory import scoped_policies, franchise_column
from app.services.communication_service import normalize_phone
from app.services.marketing_screening import (REGISTRIES, RESULTS, WEBSITES, registry_policy,
    latest_results, result_status, clearance, make_evidence, parse_sast)

screening_bp = Blueprint('marketing_screening', __name__, url_prefix='/marketing-screening')


@screening_bp.before_request
def protect_changes():
    if current_user.is_authenticated and request.method == 'POST':
        if not (is_admin_user() or is_branch_manager_user()):
            abort(403)
        if request.content_length and request.content_length > 2 * 1024 * 1024:
            abort(413)
        expected, supplied = session.get('screening_csrf', ''), request.form.get('screening_csrf', '')
        if not expected or not secrets.compare_digest(expected, supplied):
            abort(400, 'Refresh Marketing screening before submitting changes.')


def audit(action, entity_id=None, details=None):
    db.session.add(AuditLog(user_id=current_user.id, action=action, entity_type='MarketingScreening',
                            entity_id=str(entity_id) if entity_id else None, details=details))


def page(policy=None):
    session.setdefault('screening_csrf', secrets.token_urlsafe(32))
    latest = latest_results(policy.cell_number) if policy else {}
    history = MarketingScreening.query.filter_by(lapsed_policy_id=policy.id).order_by(
        MarketingScreening.recorded_at.desc(), MarketingScreening.id.desc()).limit(100).all() if policy else []
    franchises = [r[0] for r in scoped_policies().with_entities(franchise_column()).distinct().all() if r[0]]
    return render_template('clients/screening.html', policy=policy, registries=REGISTRIES,
        results=RESULTS, websites=WEBSITES, configuration=registry_policy(), latest=latest,
        result_status=result_status, history=history, franchises=sorted(franchises),
        clearance=clearance(policy) if policy else None, can_edit=is_admin_user() or is_branch_manager_user(),
        is_admin=is_admin_user(), now_sast=(datetime.utcnow() + timedelta(hours=2)).strftime('%Y-%m-%dT%H:%M'),
        sast=lambda value: (value + timedelta(hours=2)).strftime('%Y-%m-%d %H:%M') if value else '—'), 200, {'Cache-Control': 'no-store'}


@screening_bp.route('/')
@login_required
@permission_required('recovery.view')
def index():
    return page()


@screening_bp.route('/client/<int:policy_id>', methods=['GET', 'POST'])
@login_required
@permission_required('recovery.view')
def client(policy_id):
    policy = scoped_policies().filter_by(id=policy_id).first_or_404()
    if request.method == 'POST':
        try:
            row = make_evidence(policy, request.form.get('registry'), request.form.get('result'),
                parse_sast(request.form.get('checked_at')), parse_sast(request.form.get('valid_until'), False),
                request.form.get('reference'), current_user.id)
            db.session.add(row)
            db.session.flush()
            audit('MARKETING_EVIDENCE_RECORDED', row.id, f'{row.registry}: {row.result}; client #{policy.id}')
            db.session.commit()
            flash('Evidence recorded. This records a result you obtained; it does not perform an external lookup.', 'success')
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), 'danger')
        return redirect(url_for('.client', policy_id=policy.id))
    return page(policy)


@screening_bp.route('/policy', methods=['POST'])
@login_required
def save_policy():
    if not is_admin_user():
        abort(403)
    required = request.form.getlist('required')
    reference = request.form.get('reference', '').strip()
    if any(item not in REGISTRIES for item in required) or not reference or len(reference) > 1000:
        abort(400, 'Select valid registers and provide the basis for the applicability review.')
    if not required and request.form.get('confirm_none') != 'yes':
        abort(400, 'Explicitly confirm the review found no external register applicable.')
    setting = SystemSetting.query.filter_by(category='MarketingCompliance', key='registry_policy').first()
    if not setting:
        setting = SystemSetting(category='MarketingCompliance', key='registry_policy')
        db.session.add(setting)
    setting.value = json.dumps({'required': sorted(set(required)), 'reference': reference,
                               'reviewed_at': datetime.utcnow().isoformat()})
    setting.active = True
    setting.updated_by_id = current_user.id
    audit('MARKETING_REGISTRY_POLICY_UPDATED', details=setting.value)
    db.session.commit()
    flash('Applicability review saved. Known listings and client opt-outs always block marketing.', 'success')
    return redirect(url_for('.index'))


@screening_bp.route('/import', methods=['POST'])
@login_required
@permission_required('recovery.view')
def import_results():
    provider = request.form.get('registry')
    if provider not in REGISTRIES:
        abort(400, 'Choose an external register.')
    upload = request.files.get('file')
    if not upload:
        abort(400, 'Choose a results CSV.')
    try:
        content = upload.stream.read(2 * 1024 * 1024 + 1)
        if len(content) > 2 * 1024 * 1024:
            abort(413)
        reader = csv.DictReader(io.StringIO(content.decode('utf-8-sig')))
        if not {'phone_number', 'result', 'checked_at', 'valid_until', 'reference'}.issubset(reader.fieldnames or []):
            raise ValueError('CSV must contain phone_number, result, checked_at, valid_until and reference columns.')
        query = scoped_policies()
        franchise = request.form.get('franchise', '')
        if franchise:
            query = query.filter(franchise_column() == franchise)
        contacts = {}
        for policy in query.all():
            contacts.setdefault(normalize_phone(policy.cell_number), []).append(policy)
        rows, seen = [], set()
        for number, record in enumerate(reader, 2):
            if number > 5001:
                raise ValueError('Import at most 5,000 result rows at a time.')
            phone = normalize_phone(record.get('phone_number'))
            if phone not in contacts or phone in seen:
                raise ValueError(f'Row {number}: number is duplicated or has no accessible client in the selected franchise.')
            seen.add(phone)
            checked = parse_sast(record.get('checked_at'))
            expiry = parse_sast(record.get('valid_until'), False)
            for policy in contacts[phone]:
                rows.append(make_evidence(policy, provider, (record.get('result') or '').strip().lower(),
                    checked, expiry, record.get('reference'), current_user.id, source='csv'))
        if not rows:
            raise ValueError('The CSV contains no result rows.')
        db.session.add_all(rows)
        audit('MARKETING_RESULTS_IMPORTED', details=f'{provider}: {len(rows)} client records, {len(seen)} numbers')
        db.session.commit()
        flash(f'{len(rows)} client check records imported. No external lookups or messages were sent.', 'success')
    except (ValueError, UnicodeError, csv.Error) as exc:
        db.session.rollback()
        flash(str(exc), 'danger')
    return redirect(url_for('.index'))

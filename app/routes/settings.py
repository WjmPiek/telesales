from flask import Blueprint, render_template, request, redirect, url_for, flash, abort, send_file
from flask_login import login_required, current_user
from app import db
from app.models import SystemSetting, AuditLog
settings_bp=Blueprint('settings',__name__,url_prefix='/settings')

def is_admin():
    role=(current_user.role.name if current_user.is_authenticated and current_user.role else '').lower().replace('_',' ')
    return role in {'admin','super admin','superadmin'}

def is_super_admin():
    role=(current_user.role.name if current_user.is_authenticated and current_user.role else '').lower().replace('_',' ')
    return role in {'super admin','superadmin'}

@settings_bp.route('/', methods=['GET','POST'])
@login_required
def index():
    if not is_admin(): return redirect(url_for('main.dashboard'))
    if request.method=='POST':
        cat=request.form.get('category','General').strip() or 'General'; key=request.form.get('key','').strip(); value=request.form.get('value',''); desc=request.form.get('description','')
        if not key: flash('Setting key is required','danger'); return redirect(url_for('settings.index'))
        s=SystemSetting.query.filter_by(category=cat,key=key).first() or SystemSetting(category=cat,key=key)
        s.value=value; s.description=desc; s.active=bool(request.form.get('active')); s.updated_by_id=current_user.id
        db.session.add(s); db.session.add(AuditLog(user_id=current_user.id, action='Update system setting', entity_type='SystemSetting', entity_id=f'{cat}:{key}', details=value[:500])); db.session.commit(); flash('Setting saved','success')
        return redirect(url_for('settings.index', category=cat))
    category=request.args.get('category') or 'WhatsApp'
    settings=SystemSetting.query.filter_by(category=category).order_by(SystemSetting.key).all()
    categories=[r[0] for r in db.session.query(SystemSetting.category).distinct().order_by(SystemSetting.category).all()] or ['WhatsApp','Email','Call Script','Products','Documents','Outcomes','Targets','Branches']
    return render_template('settings/index.html', settings=settings, categories=categories, category=category)

@settings_bp.route('/seed')
@login_required
def seed():
    if not is_admin(): return redirect(url_for('main.dashboard'))
    defaults=[('WhatsApp','signature_reminder','Good day {client_name}, please complete your Martin\'s Funerals signing link: {link}'),('Email','missing_documents','Please send the outstanding documents for your application.'),('Outcomes','required_outcomes','No Answer\nInterested\nNot Interested\nCallback\nApplication Started\nSignature Sent'),('Documents','required_fica','id_copy\nproof_of_address\nbank_statement'),('Targets','daily_calls_per_agent','40')]
    for cat,key,val in defaults:
        if not SystemSetting.query.filter_by(category=cat,key=key).first(): db.session.add(SystemSetting(category=cat,key=key,value=val,description='Default Phase 7 setting',updated_by_id=current_user.id))
    db.session.commit(); flash('Default settings created','success'); return redirect(url_for('settings.index'))


@settings_bp.route('/email-templates', methods=['GET', 'POST'])
@login_required
def email_templates():
    import json
    from app.services.email_service import client_email_templates, validate_client_email_templates, CLIENT_EMAIL_DEFAULTS
    if not is_admin():
        abort(403)
    templates = client_email_templates()
    error = None
    if request.method == 'POST':
        templates = {key: {"label": default["label"],
                          "subject": request.form.get(key + '_subject', '').strip(),
                          "body": request.form.get(key + '_body', '').strip()}
                     for key, default in CLIENT_EMAIL_DEFAULTS.items()}
        try:
            validate_client_email_templates(templates)
        except ValueError as exc:
            error = str(exc)
        if not error:
            row = SystemSetting.query.filter_by(category='Email', key='client_templates_v1').first()
            if row is None:
                row = SystemSetting(category='Email', key='client_templates_v1')
                db.session.add(row)
            row.value = json.dumps(templates)
            row.active = True
            row.updated_by_id = current_user.id
            row.description = 'Client invitation, supporting-document request and policy activation wording'
            db.session.add(AuditLog(user_id=current_user.id, action='Email templates updated',
                entity_type='SystemSetting', entity_id='Email:client_templates_v1',
                details='Updated client email wording for future messages.'))
            db.session.commit()
            flash('Email templates saved. Future emails will use this wording.', 'success')
            return redirect(url_for('settings.email_templates'))
    return render_template('settings/email_templates.html', templates=templates, error=error)


@settings_bp.route('/bank-confirmation-letters', methods=['GET', 'POST'])
@login_required
def bank_confirmation_letters():
    import hashlib
    import io
    from pypdf import PdfReader
    from app.models import BankConfirmationLetter
    if not is_admin():
        abort(403)
    if request.method == 'POST':
        upload = request.files.get('bank_confirmation_letter')
        content = upload.read() if upload and upload.filename else b''
        if not content:
            flash('Choose the official business bank confirmation PDF.', 'danger')
        elif len(content) > 5 * 1024 * 1024:
            flash('The business bank confirmation PDF may not exceed 5 MB.', 'danger')
        elif not content.startswith(b'%PDF'):
            flash('The bank confirmation letter must be a PDF.', 'danger')
        else:
            try:
                if not PdfReader(io.BytesIO(content)).pages:
                    raise ValueError('empty PDF')
            except Exception:
                flash('The selected file is not a readable PDF.', 'danger')
            else:
                checksum = hashlib.sha256(content).hexdigest()
                BankConfirmationLetter.query.update({'active': False}, synchronize_session=False)
                letter = BankConfirmationLetter(
                    original_filename=(upload.filename or 'bank-confirmation.pdf')[:255],
                    file_data=content, file_size=len(content), checksum_sha256=checksum,
                    active=True, uploaded_by_id=current_user.id)
                db.session.add(letter)
                db.session.flush()
                db.session.add(AuditLog(user_id=current_user.id, action='Bank confirmation letter uploaded',
                    entity_type='BankConfirmationLetter', entity_id=str(letter.id),
                    details='Uploaded a new current business bank confirmation PDF.'))
                db.session.commit()
                flash('Newest bank confirmation letter uploaded and set as current.', 'success')
                return redirect(url_for('settings.bank_confirmation_letters'))
    letters = BankConfirmationLetter.query.order_by(BankConfirmationLetter.uploaded_at.desc(), BankConfirmationLetter.id.desc()).all()
    return render_template('settings/bank_confirmation_letters.html', letters=letters)


@settings_bp.route('/bank-confirmation-letters/<int:letter_id>/download')
@login_required
def download_bank_confirmation_letter(letter_id):
    import io
    from app.models import BankConfirmationLetter
    if not is_admin():
        abort(403)
    letter = BankConfirmationLetter.query.get_or_404(letter_id)
    return send_file(io.BytesIO(letter.file_data), mimetype='application/pdf',
                     as_attachment=True, download_name=letter.original_filename)


@settings_bp.route('/bank-confirmation-letters/<int:letter_id>/activate', methods=['POST'])
@login_required
def activate_bank_confirmation_letter(letter_id):
    from app.models import BankConfirmationLetter
    if not is_admin():
        abort(403)
    letter = BankConfirmationLetter.query.get_or_404(letter_id)
    BankConfirmationLetter.query.update({'active': False}, synchronize_session=False)
    letter.active = True
    db.session.add(AuditLog(user_id=current_user.id, action='Bank confirmation letter activated',
        entity_type='BankConfirmationLetter', entity_id=str(letter.id),
        details='Selected an existing bank confirmation PDF as the current cash-payment attachment.'))
    db.session.commit()
    flash('Selected bank confirmation letter is now current.', 'success')
    return redirect(url_for('settings.bank_confirmation_letters'))


@settings_bp.route('/company-documents', methods=['GET', 'POST'])
@login_required
def company_documents():
    import hashlib
    import io
    from pypdf import PdfReader
    from app.models import CompanyDocument, CompanyGroupState
    from app.routes.auth import _is_user_management_owner
    if not _is_user_management_owner(current_user):
        abort(403)
    companies = CompanyGroupState.query.order_by(CompanyGroupState.company_name, CompanyGroupState.branch).all()
    if request.method == 'POST':
        company = db.session.get(CompanyGroupState, request.form.get('company_id', type=int))
        category = request.form.get('category')
        upload = request.files.get('document')
        content = upload.read(5 * 1024 * 1024 + 1) if upload and upload.filename else b''
        if not company or category not in {'bank_confirmation', 'additional'}:
            abort(400)
        if not content or len(content) > 5 * 1024 * 1024 or not content.startswith(b'%PDF'):
            flash('Choose a valid PDF of no more than 5 MB.', 'danger')
        else:
            try:
                if not PdfReader(io.BytesIO(content)).pages:
                    raise ValueError('empty PDF')
            except Exception:
                flash('The PDF could not be read.', 'danger')
            else:
                if category == 'bank_confirmation':
                    CompanyDocument.query.filter_by(company_id=company.id, category=category).update({'active': False})
                row = CompanyDocument(company_id=company.id, category=category,
                    original_filename=(upload.filename or 'document.pdf')[:255], file_data=content,
                    file_size=len(content), checksum_sha256=hashlib.sha256(content).hexdigest(),
                    active=True, uploaded_by_id=current_user.id)
                db.session.add(row); db.session.flush()
                db.session.add(AuditLog(user_id=current_user.id, action='Company document uploaded',
                    entity_type='CompanyDocument', entity_id=str(row.id),
                    details=f'Company {company.id}; category {category}; SHA-256 {row.checksum_sha256}.'))
                db.session.commit()
                flash('Company document saved for future client emails.', 'success')
                return redirect(url_for('settings.company_documents'))
    documents = CompanyDocument.query.order_by(CompanyDocument.company_id, CompanyDocument.id.desc()).all()
    return render_template('settings/company_documents.html', companies=companies, documents=documents)


@settings_bp.route('/company-documents/<int:document_id>/download')
@login_required
def download_company_document(document_id):
    import io
    from app.models import CompanyDocument
    from app.routes.auth import _is_user_management_owner
    if not _is_user_management_owner(current_user): abort(403)
    row = CompanyDocument.query.get_or_404(document_id)
    return send_file(io.BytesIO(row.file_data), mimetype='application/pdf', as_attachment=True,
                     download_name=row.original_filename)


@settings_bp.route('/company-documents/<int:document_id>/toggle', methods=['POST'])
@login_required
def toggle_company_document(document_id):
    from app.models import CompanyDocument
    from app.routes.auth import _is_user_management_owner
    if not _is_user_management_owner(current_user): abort(403)
    row = CompanyDocument.query.get_or_404(document_id)
    row.active = not row.active
    if row.active and row.category == 'bank_confirmation':
        CompanyDocument.query.filter(CompanyDocument.company_id == row.company_id,
                                     CompanyDocument.category == 'bank_confirmation',
                                     CompanyDocument.id != row.id).update({'active': False})
    db.session.add(AuditLog(user_id=current_user.id, action='Company document status changed',
        entity_type='CompanyDocument', entity_id=str(row.id),
        details=f'Company {row.company_id}; active={row.active}.'))
    db.session.commit()
    flash('Company document status updated.', 'success')
    return redirect(url_for('settings.company_documents'))


@settings_bp.route('/cover-checks', methods=['GET', 'POST'])
@login_required
def cover_checks():
    from types import SimpleNamespace
    from app.routes.auth import _is_user_management_owner
    from app.services.compliance_service import is_valid_sa_id, only_digits
    from app.services.cover_eligibility import coverage_report
    if not _is_user_management_owner(current_user): abort(403)
    identifier = only_digits(request.form.get('id_number', '')) if request.method == 'POST' else ''
    report = None
    if identifier:
        if not is_valid_sa_id(identifier):
            flash('Enter a valid 13-digit South African ID number.', 'danger')
        else:
            probe = SimpleNamespace(id=None, id_number=identifier, first_names='', surname='',
                                    product=None, cover_amount=0, date_of_birth='',
                                    product_dependents_json=None, spouse_first_names=None,
                                    spouse_surname=None, spouse_id_number=None,
                                    spouse_date_of_birth=None, dependents_json=None,
                                    extended_family_json=None)
            report = coverage_report(probe)
            import hashlib
            db.session.add(AuditLog(user_id=current_user.id, action='Member cover report viewed',
                entity_type='CoverCheck', entity_id=hashlib.sha256(identifier.encode()).hexdigest()[:16],
                details=f"Matched {len(report['members'][0]['existing_policies'])} applications; imported lead matches {report['imported_matches_without_cover']}."))
            db.session.commit()
    from app.models import CompanyGroupState
    companies = CompanyGroupState.query.order_by(CompanyGroupState.company_name, CompanyGroupState.branch).all()
    return render_template('settings/cover_checks.html', identifier=identifier, report=report, companies=companies)


@settings_bp.route('/cover-checks/import', methods=['POST'])
@login_required
def import_historical_member_cover():
    import csv
    import io
    from decimal import Decimal, InvalidOperation
    from app.models import CompanyGroupState, HistoricalMemberCover
    from app.routes.auth import _is_user_management_owner
    from app.services.compliance_service import is_valid_sa_id, only_digits
    if not _is_user_management_owner(current_user): abort(403)
    company = db.session.get(CompanyGroupState, request.form.get('company_id', type=int))
    upload = request.files.get('file')
    if not company or not upload or not upload.filename:
        abort(400)
    raw = upload.read(3 * 1024 * 1024 + 1)
    if len(raw) > 3 * 1024 * 1024:
        flash('The cover export must be 3 MB or smaller.', 'danger')
        return redirect(url_for('settings.cover_checks'))
    try:
        content = raw.decode('utf-8-sig')
        reader = csv.DictReader(io.StringIO(content))
        required = {'policy_number', 'id_number', 'cover_amount', 'status'}
        if not reader.fieldnames or not required.issubset({name.strip() for name in reader.fieldnames}):
            raise ValueError('CSV columns must be policy_number, id_number, cover_amount, status.')
        rows = []
        seen = set()
        for number, original in enumerate(reader, 2):
            values = {str(key or '').strip(): str(value or '').strip() for key, value in original.items()}
            policy = values['policy_number'][:80]
            identifier = only_digits(values['id_number'])
            status = values['status'].title()
            try:
                cover = Decimal(values['cover_amount'].replace(',', '').replace('R', '').strip())
            except (InvalidOperation, ValueError):
                raise ValueError(f'Row {number}: invalid cover amount.')
            if not policy or not is_valid_sa_id(identifier) or cover <= 0 or status not in {'Active', 'Lapsed', 'Cancelled'}:
                raise ValueError(f'Row {number}: enter a policy number, valid South African ID, positive cover and valid status.')
            key = (policy, identifier)
            if key in seen:
                raise ValueError(f'Row {number}: duplicate policy/member pair in this file.')
            seen.add(key)
            rows.append((policy, identifier, cover, status))
        if not rows:
            raise ValueError('The file has no policy/member rows.')
    except (UnicodeError, csv.Error, ValueError) as exc:
        flash(str(exc), 'danger')
        return redirect(url_for('settings.cover_checks'))
    for policy, identifier, cover, status in rows:
        row = HistoricalMemberCover.query.filter_by(company_id=company.id,
            policy_number=policy, id_number=identifier).first()
        if row is None:
            row = HistoricalMemberCover(company_id=company.id, policy_number=policy,
                                        id_number=identifier, imported_by_id=current_user.id)
            db.session.add(row)
        row.cover_amount = cover
        row.status = status
    db.session.add(AuditLog(user_id=current_user.id, action='Historical member cover imported',
        entity_type='CompanyGroup', entity_id=str(company.id),
        details=f'{len(rows)} member benefit rows imported or updated; SHA-256 file digest: {__import__("hashlib").sha256(raw).hexdigest()}.'))
    db.session.commit()
    flash(f'{len(rows)} member cover rows saved for {company.company_name}. Only Active rows count toward eligibility.', 'success')
    return redirect(url_for('settings.cover_checks'))


@settings_bp.route('/data-reset', methods=['GET', 'POST'])
@login_required
def data_reset():
    from flask import abort
    from app.services.data_reset import reset_operational_data, reset_preview
    if not is_super_admin():
        abort(403)
    if request.method == 'POST':
        if request.form.get('confirmation', '').strip() != 'CLEAR ALL TEST DATA':
            flash('Reset cancelled. Enter the exact confirmation phrase.', 'danger')
            return redirect(url_for('settings.data_reset'))
        try:
            counts = reset_operational_data()
        except Exception:
            db.session.rollback()
            flash('The reset failed and no partial database changes were saved.', 'danger')
            raise
        flash(
            'Operational data cleared: '
            f"{counts['applications']} applications, {counts['imported_clients']} imported clients, "
            f"{counts['whatsapp_messages']} WhatsApp messages and "
            f"{counts['campaign_recipients']} campaign recipients removed. "
            f"{counts['suppression_records_kept']} opt-out suppression records were preserved.",
            'success')
        return redirect(url_for('settings.data_reset'))
    return render_template('settings/data_reset.html', counts=reset_preview())

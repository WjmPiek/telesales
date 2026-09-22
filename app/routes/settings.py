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

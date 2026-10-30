import secrets
from datetime import datetime, timedelta

from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, session, url_for
from flask_login import current_user, login_required

from app import db
from app.models import ApplicationMemberAddition, AuditLog, ClientApplication
from app.security import permission_required
from app.services.branch_access import ensure_branch_access
from app.services import member_additions as service

member_additions_bp = Blueprint('member_additions', __name__, url_prefix='/member-additions')


@member_additions_bp.after_request
def private_response(response):
    response.headers['Cache-Control'] = 'no-store'
    response.headers['Referrer-Policy'] = 'no-referrer'
    return response


@member_additions_bp.route('/request/<int:app_id>', methods=['POST'])
@login_required
@permission_required('applications.send_signing')
def send_link(app_id):
    application = ClientApplication.query.filter_by(id=app_id).with_for_update().first_or_404()
    ensure_branch_access(application, agent_attr='agent_id')
    csrf = session.get('member_addition_csrf')
    if not csrf or not secrets.compare_digest(csrf, request.form.get('member_addition_csrf', '')):
        abort(400)
    pending = service.pending_addition(application)
    if not pending and (not service.can_add(application) or not service.available_kinds(application)):
        flash('This application has no available add-member correction. Complete any open correction first; approved policies require staff amendment handling.', 'warning')
        return redirect(url_for('applications.view_application', app_id=app_id))
    channel = request.form.get('channel', 'email')
    if channel not in {'email', 'whatsapp'}:
        abort(400)
    raw_token = secrets.token_urlsafe(32)
    now = datetime.utcnow()
    ApplicationMemberAddition.query.filter_by(application_id=app_id, revoked_at=None, completed_at=None).update({'revoked_at':now})
    addition = ApplicationMemberAddition(application_id=app_id, requested_by_id=current_user.id,
        token_hash=service.token_hash(raw_token), snapshot=service.snapshot(application), expires_at=now+timedelta(days=7),
        used_at=pending.used_at if pending else None)
    db.session.add(addition)
    db.session.add(AuditLog(user_id=current_user.id, action='Add-member link requested', entity_type='ClientApplication',
        entity_id=str(app_id), details=f'Staff requested a seven-day single-use add-member link by {channel}.'))
    db.session.commit()
    link = current_app.config['BASE_URL'].rstrip('/') + url_for('member_additions.client_form', token=raw_token)
    instruction = ('Continue the member addition for application ' if pending else 'You can add one missing member to application ')
    next_step = ('The member’s details are already saved. Continue with the updated application signature and submit it. '
                 if pending else 'Enter the missing member’s details, review and sign the updated application, then upload the new member’s ID. ')
    body = (f'Dear {application.first_names or "client"},\n\n{instruction}'
            f'{application.application_ref} using this secure link:\n\n{link}\n\n'
            f'Your existing details and uploaded documents are saved. {next_step}'
            'You do not need to complete the full application again. This link expires in seven days.')
    sent = False
    try:
        if channel == 'whatsapp':
            from app.services.whatsapp_service import send_application_link
            sent = send_application_link(application, link, body).ok
        else:
            from app.services.delivery_preferences import valid_email
            from app.services.email_service import send_email, signing_email_html
            email = (application.document_email or application.email or '').strip()
            if valid_email(email):
                sent = send_email(email, f'Add a member: {application.application_ref}', body, [],
                    html_body=signing_email_html(application, link, body, 'Add a missing member'), application_id=app_id)
    except Exception:
        current_app.logger.exception('Add-member invitation delivery failed for application %s', app_id)
    addition.delivery_status = 'Accepted by provider' if sent else 'Delivery failed'
    db.session.add(AuditLog(user_id=current_user.id, action='Add-member link delivery', entity_type='ClientApplication',
        entity_id=str(app_id), details=f'{channel}: {addition.delivery_status}'))
    db.session.commit()
    flash('Add-member link accepted for delivery.' if sent else 'The add-member link could not be delivered. Check the client contact details and try again.',
          'success' if sent else 'danger')
    return redirect(url_for('applications.view_application', app_id=app_id))


@member_additions_bp.route('/<token>', methods=['GET', 'POST'])
def client_form(token):
    addition = ApplicationMemberAddition.query.filter_by(token_hash=service.token_hash(token)).first_or_404()
    application = addition.application
    if addition.revoked_at or addition.expires_at <= datetime.utcnow():
        return render_template('member_additions/expired.html'), 410
    nonce_key = f'add_member_nonce_{addition.id}'
    session.setdefault(nonce_key, secrets.token_urlsafe(24))
    unlocked = session.get(f'add_member_verified_{addition.id}') == addition.token_hash
    error = None
    if request.method == 'POST':
        if not secrets.compare_digest(session[nonce_key], request.form.get('nonce', '')):
            abort(400)
        if request.form.get('action') == 'unlock':
            from app.services.compliance_service import only_digits
            entered = only_digits(request.form.get('id_number', ''))
            if entered and entered == only_digits(application.id_number):
                session[f'add_member_verified_{addition.id}'] = addition.token_hash
                return redirect(url_for('member_additions.client_form', token=token))
            error = 'The principal member ID does not match this application.'
    if not unlocked:
        return render_template('member_additions/form.html', stage='unlock', error=error, nonce=session[nonce_key])
    from app.routes.signing import _unlocked_key
    session[_unlocked_key(application.id)] = True
    if addition.completed_at:
        return render_template('member_additions/form.html', stage='complete', app=application,
            upload_link=url_for('signing.supporting_documents', token=application.sign_token))
    if not addition.used_at and (not service.can_add(application) or service.snapshot(application) != addition.snapshot):
        return render_template('member_additions/expired.html'), 410
    if addition.used_at and (application.status in {'Active','QA Approved','Compliance Approved','Cancelled','Declined'}
                             or (application.whatsapp_journey and application.whatsapp_journey.activated_at)
                             or application.sign_token_revoked or application.sign_token_used_at):
        return render_template('member_additions/expired.html'), 410
    if request.method == 'POST' and request.form.get('action') != 'unlock':
        try:
            # Lock the application before the request in both staff and client flows.
            application = ClientApplication.query.filter_by(id=application.id).with_for_update().populate_existing().one()
            addition = ApplicationMemberAddition.query.filter_by(id=addition.id).with_for_update().populate_existing().one()
            if addition.revoked_at or addition.expires_at <= datetime.utcnow():
                abort(410)
            if addition.completed_at:
                return redirect(url_for('member_additions.client_form', token=token))
            if (application.status in {'Active','QA Approved','Compliance Approved','Cancelled','Declined'}
                    or (application.whatsapp_journey and application.whatsapp_journey.activated_at)):
                abort(410)
            action = request.form.get('action')
            if action == 'add' and not addition.used_at:
                if service.snapshot(application) != addition.snapshot:
                    raise ValueError('The application has changed. Please ask your agent for a fresh add-member link.')
                name = service.append_member(application, request.form)
                from app.routes.signing import _reopen_changed_application_for_signature
                _reopen_changed_application_for_signature(application, [name], reason='member addition')
                addition.used_at = datetime.utcnow()
                session[f'member_addition_{application.id}'] = {'id':addition.id, 'token':token}
                db.session.add(AuditLog(action='Member added by client', entity_type='ClientApplication', entity_id=str(application.id),
                    details=f'{name} added through staff request {addition.id}; existing details and supporting documents retained; updated application signature required.'))
                db.session.commit()
                return redirect(url_for('member_additions.client_form', token=token))
            if action == 'submit' and addition.used_at and not addition.completed_at:
                from app.services.signature_fields import signed_documents
                if 'application' not in signed_documents(application):
                    raise ValueError('Please sign the updated application first.')
                from app.routes.signing import finish_application
                addition.completed_at = datetime.utcnow()
                if application.whatsapp_journey:
                    application.whatsapp_journey.signed_bundle_at = datetime.utcnow()
                finish_application(application, application.sign_token)
                session.pop(f'member_addition_{application.id}', None)
                return redirect(url_for('member_additions.client_form', token=token))
            raise ValueError('This member has already been added. Continue with the updated application below.')
        except ValueError as exc:
            db.session.rollback()
            error = str(exc)
    if addition.used_at:
        session[f'member_addition_{application.id}'] = {'id':addition.id, 'token':token}
        from app.services.signature_fields import signed_documents
        return render_template('member_additions/form.html', stage='review', app=application, error=error,
            nonce=session[nonce_key], application_signed='application' in signed_documents(application))
    return render_template('member_additions/form.html', stage='add', app=application, error=error,
        nonce=session[nonce_key], choices=service.available_kinds(application))

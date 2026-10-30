from app.services.screening_service import ensure_screened
from app.services.client_storage import application_folder, store_document
import os
import secrets
from datetime import datetime
from flask import Blueprint, render_template, request, redirect, url_for, flash, current_app, send_file, abort, session
from flask_login import login_required, current_user
from werkzeug.utils import secure_filename
from app import db
from app.models import ClientApplication, ClientFicaDocument, AuditLog
from app.services.document_status_service import (document_summary, FICA_LABELS, STAFF_UPLOAD_LABELS,
                                                   required_fica_documents, fica_label)
from app.services.fica_validation_service import validate_fica_upload
from app.services.email_service import send_email
from app.services.whatsapp_service import send_whatsapp_message
from app.services.branch_access import scope_by_branch, ensure_branch_access, selected_branch_arg, branch_choices_from_model


documents_bp = Blueprint("documents", __name__, url_prefix="/documents")
ALLOWED_UPLOADS = {"pdf", "png", "jpg", "jpeg", "webp"}


def _role_name():
    return str(getattr(getattr(current_user, "role", None), "name", "") or "").lower().replace("_", " ")


def _can_manage_documents():
    return _role_name() in {"admin", "super admin", "super_admin", "manager", "branch manager", "branch_manager", "compliance", "qa"}


def _can_review_application(application):
    return _can_manage_documents() or bool(application.whatsapp_journey and application.agent_id == current_user.id)


def _editable(application):
    from app.services.application_workflow import FINAL_STATUSES
    if application.status in FINAL_STATUSES or (application.whatsapp_journey and application.whatsapp_journey.activated_at):
        abort(409, 'This application has a final decision. Open the application to view its delivery status.')


def _client_name(app):
    return " ".join([x for x in [app.first_names, app.surname] if x]) or app.application_ref


def _ensure_active_signing_link(app):
    """Create or reopen a secure signing link so the client can re-upload rejected FICA."""
    if not app.sign_token:
        app.sign_token = secrets.token_urlsafe(32)
    app.sign_token_created_at = datetime.utcnow()
    if not app.signed_at:
        app.sign_token_used_at = None
        app.sign_token_revoked = False
    return app.sign_token


def _send_rejected_document_email(app, rejected_labels, reason=None):
    email = (app.document_email or app.email or '').strip()
    if not email:
        return False, "Application has no client email address."

    screened, errors=ensure_screened(app)
    if not screened:return False, "; ".join(errors)
    token = _ensure_active_signing_link(app)
    base_url = current_app.config.get("BASE_URL") or request.url_root.rstrip("/")
    endpoint = 'signing.supporting_documents' if app.signed_at else 'signing.sign_application'
    link = f"{base_url}{url_for(endpoint, token=token)}"
    doc_lines = "\n".join(f"- {label}" for label in rejected_labels)
    reason_text = f"\n\nReason: {reason}" if reason else ""
    body = (
        f"Dear {_client_name(app)},\n\n"
        "Martin's Funerals reviewed the document(s) you uploaded for your application and could not accept the following document(s):\n"
        f"{doc_lines}"
        f"{reason_text}\n\n"
        "Please upload the correct replacement document(s) using this secure link:\n"
        f"{link}\n\n"
        "Only the document(s) listed above need to be resent.\n\n"
        "Thank you.\nMartin's Funerals"
    )
    sent = send_email(email, "Martin's Funerals document rejected - please resend", body, [], application_id=app.id)
    return sent, None


def _upload_folder():
    folder = os.path.abspath(current_app.config["UPLOAD_FOLDER"])
    os.makedirs(folder, exist_ok=True)
    return folder


def _resolve_existing(path):
    if not path:
        return None
    base = os.path.basename(path)
    candidates = [path, os.path.abspath(path), os.path.join(_upload_folder(), base), os.path.join(current_app.root_path, "static", "uploads", base)]
    for candidate in candidates:
        if candidate and os.path.exists(candidate):
            return os.path.abspath(candidate)
    return None


def _allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_UPLOADS


@documents_bp.route("/")
@login_required
def document_dashboard():
    if not _can_manage_documents() and _role_name() != 'agent':
        flash("Only managers/compliance users can access document tracking.", "danger")
        return redirect(url_for("main.dashboard"))
    branch = selected_branch_arg()
    status_filter = request.args.get("status") or "outstanding"
    q = scope_by_branch(ClientApplication.query, ClientApplication, agent_col=ClientApplication.agent_id, selected_branch=branch)
    apps = q.order_by(ClientApplication.updated_at.desc()).limit(300).all()

    records = []
    for app in apps:
        summary = document_summary(app)
        if status_filter == "missing" and not summary["missing"]:
            continue
        if status_filter == "review" and not summary["pending_review"]:
            continue
        if status_filter == "complete" and not summary["complete"]:
            continue
        if status_filter == "outstanding" and summary["complete"]:
            continue
        records.append({"app": app, "summary": summary})

    branches = branch_choices_from_model(db, ClientApplication)
    stats = {
        "outstanding": sum(1 for app in apps if not document_summary(app)["complete"]),
        "missing": sum(1 for app in apps if document_summary(app)["missing"]),
        "review": sum(1 for app in apps if document_summary(app)["pending_review"]),
        "complete": sum(1 for app in apps if document_summary(app)["complete"]),
    }
    return render_template("documents/dashboard.html", records=records, branches=branches, active_branch=branch, status_filter=status_filter, stats=stats)


@documents_bp.route("/application/<int:app_id>", methods=["GET", "POST"])
@login_required
def application_documents(app_id):
    app = ClientApplication.query.get_or_404(app_id)
    if not _can_review_application(app):
        abort(403)
    ensure_branch_access(app, agent_attr="agent_id")
    upload_labels = {**STAFF_UPLOAD_LABELS,
                     **{row["key"]: row["label"] for row in required_fica_documents(app)}}
    session.setdefault('document_request_csrf', secrets.token_urlsafe(32))
    if request.method == "POST":
        _editable(app)
        doc_type = request.form.get("document_type")
        uploaded_file = request.files.get("file")
        if doc_type not in upload_labels:
            flash("Invalid document type.", "danger")
            return redirect(url_for("documents.application_documents", app_id=app.id))
        if not uploaded_file or not uploaded_file.filename:
            flash("Choose a file to upload.", "danger")
            return redirect(url_for("documents.application_documents", app_id=app.id))
        if not _allowed_file(uploaded_file.filename):
            flash("Only PDF, JPG, PNG or WEBP files are allowed.", "danger")
            return redirect(url_for("documents.application_documents", app_id=app.id))
        from app.services.upload_guard import reject_duplicate
        try:
            reject_duplicate(app, uploaded_file, doc_type)
        except ValueError as exc:
            flash(str(exc), "warning")
            return redirect(url_for("documents.application_documents", app_id=app.id))
        safe = secure_filename(uploaded_file.filename)
        folder = os.path.join(application_folder(app), "fica")
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, f"{doc_type}_{datetime.utcnow().strftime('%Y%m%d%H%M%S%f')}_{safe}")
        uploaded_file.save(path)
        store_document(app, path)
        fica_types = set(FICA_LABELS) | {row["key"] for row in required_fica_documents(app)}
        validation_status, validation_notes = (validate_fica_upload(path, safe, doc_type, app)
            if doc_type in fica_types else ("Needs Review", "Staff must verify the complete document and all required signatures."))
        for previous in ClientFicaDocument.query.filter_by(application_id=app.id, document_type=doc_type).all():
            previous.status = "Replaced"
        doc = ClientFicaDocument(application_id=app.id, document_type=doc_type, original_filename=safe, file_path=path, status=validation_status, uploaded_ip=request.remote_addr, user_agent=request.headers.get("User-Agent"))
        db.session.add(doc)
        if app.status not in {'Active', 'QA Approved', 'Compliance Approved', 'QA Rejected', 'Compliance Rejected', 'Signed'}:
            app.status = 'FICA Review'
        db.session.add(AuditLog(user_id=current_user.id, action="FICA Uploaded", entity_type="ClientApplication", entity_id=str(app.id), details=f"{upload_labels.get(doc_type, doc_type)} uploaded by staff: {safe}; Status: {validation_status}; {validation_notes}"))
        db.session.commit()
        flash(f"Document uploaded. Status: {validation_status}. {validation_notes}", "warning" if validation_status == "Needs Review" else "danger")
        return redirect(url_for("documents.application_documents", app_id=app.id))
    from app.services.application_workflow import workflow_state
    flow = workflow_state(app)
    return render_template("documents/application.html", app=app, flow=flow, summary=flow['summary'], fica_labels=upload_labels,
        last_request=AuditLog.query.filter_by(entity_type='ClientApplication', entity_id=str(app.id), action='Agent requested outstanding items').order_by(AuditLog.id.desc()).first(),
        upload_audit=AuditLog.query.filter_by(entity_type="ClientApplication", entity_id=str(app.id)).filter(AuditLog.action.in_(["FICA Uploaded", "Duplicate upload rejected"])).order_by(AuditLog.id.desc()).limit(100).all())


@documents_bp.route("/fica/<int:doc_id>/download")
@login_required
def download_fica(doc_id):
    doc = ClientFicaDocument.query.get_or_404(doc_id)
    if not _can_review_application(doc.application):
        abort(403)
    ensure_branch_access(doc.application, agent_attr="agent_id")
    application_folder(doc.application)
    path = _resolve_existing(doc.file_path)
    if not path:
        abort(404)
    return send_file(path, as_attachment=False)


@documents_bp.route("/fica/<int:doc_id>/<action>", methods=["POST"])
@login_required
def review_fica(doc_id, action):
    doc = ClientFicaDocument.query.get_or_404(doc_id)
    if not _can_review_application(doc.application):
        abort(403)
    ensure_branch_access(doc.application, agent_attr="agent_id")
    if action not in {"approve", "reject"}:
        abort(404)
    app = ClientApplication.query.filter_by(id=doc.application_id).with_for_update().populate_existing().one()
    doc = ClientFicaDocument.query.filter_by(id=doc.id).with_for_update().populate_existing().one()
    _editable(app)
    latest = ClientFicaDocument.query.filter_by(application_id=app.id, document_type=doc.document_type).order_by(ClientFicaDocument.uploaded_at.desc(), ClientFicaDocument.id.desc()).first()
    if doc.status == 'Replaced' or latest.id != doc.id:
        abort(409, 'A newer upload exists. Review the latest file.')
    old_status = doc.status
    reason = request.form.get("reason", "").strip()
    label = fica_label(doc.application, doc.document_type)

    if action == "approve":
        doc.status = "Reviewed"
        db.session.flush()
        from app.services.application_workflow import advance_to_quality
        ready = advance_to_quality(app, actor_id=current_user.id)
        audit_details = f"{label} changed from {old_status} to {doc.status}. {reason}"
        db.session.add(AuditLog(
            user_id=current_user.id,
            action="FICA Reviewed",
            entity_type="ClientFicaDocument",
            entity_id=str(doc.id),
            details=audit_details
        ))
        db.session.commit()
        flash(f"{label} accepted. " + ('Agent check complete. Continue with Quality Review.' if ready else 'Continue checking the remaining items.'), "success")
        return redirect(url_for('qa.review_application' if ready else 'documents.application_documents', app_id=app.id))

    if not reason:
        flash('Enter what the client needs to correct before requesting a replacement.', 'danger')
        return redirect(url_for('documents.application_documents', app_id=app.id))
    doc.status = "Rejected"
    doc.application.status = "FICA Outstanding"
    sent, mail_error = _send_rejected_document_email(doc.application, [label], reason)
    audit_details = f"{label} changed from {old_status} to {doc.status}. {reason}"
    if sent:
        audit_details += " Rejection email sent to client."
    elif mail_error:
        audit_details += f" Rejection email not sent: {mail_error}"
    else:
        audit_details += " Rejection email delivery failed. Check provider settings/logs."

    db.session.add(AuditLog(
        user_id=current_user.id,
        action="FICA Rejected",
        entity_type="ClientFicaDocument",
        entity_id=str(doc.id),
        details=audit_details
    ))
    db.session.commit()

    if sent:
        flash(f"{label} rejected. A resend email was sent to the client.", "warning")
    else:
        flash(f"{label} rejected, but the email was not sent. {mail_error or 'Check SMTP/provider settings.'}", "danger")
    return redirect(url_for("documents.application_documents", app_id=doc.application_id))


@documents_bp.route('/application/<int:app_id>/request-outstanding', methods=['POST'])
@login_required
def request_outstanding(app_id):
    app = ClientApplication.query.filter_by(id=app_id).with_for_update().first_or_404()
    if not _can_review_application(app):
        abort(403)
    ensure_branch_access(app, agent_attr='agent_id')
    supplied = request.form.get('document_request_csrf', '')
    if not supplied or not secrets.compare_digest(session.get('document_request_csrf', ''), supplied):
        abort(400, 'Refresh the document review page before sending.')
    _editable(app)
    if not app.signed_at or not app.sign_token:
        flash('The client must submit the signed application before a supporting-document request can be sent.', 'danger')
        return redirect(url_for('documents.application_documents', app_id=app.id))
    from app.services.application_workflow import workflow_state
    from app.services.delivery_preferences import valid_email
    from app.services.email_service import signing_email_html
    flow = workflow_state(app)
    choices = {row['key']: row for row in flow['client_requests']}
    selected = list(dict.fromkeys(request.form.getlist('items')))
    if any(key not in choices for key in selected):
        abort(409, 'The document list changed. Refresh and review the latest uploads.')
    notes = request.form.get('notes', '').strip()
    if len(notes) > 2000 or (not selected and not notes and not flow['missing_ids']):
        flash('Select the outstanding documents or enter what the client needs to provide (up to 2000 characters).', 'danger')
        return redirect(url_for('documents.application_documents', app_id=app.id))
    if any(choices[key]['status'] == 'Needs Review' for key in selected) and not notes:
        flash('Explain what needs correcting before requesting a replacement for an uploaded document.', 'danger')
        return redirect(url_for('documents.application_documents', app_id=app.id))
    email = (app.document_email or app.email or '').strip()
    if not valid_email(email):
        flash('Add a valid client document email address before sending.', 'danger')
        return redirect(url_for('documents.application_documents', app_id=app.id))
    for key in selected:
        doc = choices[key].get('document')
        if doc:
            doc.status = 'Rejected'
    labels = [choices[key]['label'] for key in selected]
    token = _ensure_active_signing_link(app)
    link = current_app.config['BASE_URL'].rstrip('/') + url_for('signing.supporting_documents', token=token)
    body = f'Dear {_client_name(app)},\n\nWe checked your application. Please provide the following outstanding items:\n'
    body += '\n'.join('- ' + label for label in labels)
    if flow['missing_ids']:
        body += '\n- Valid ID numbers for: ' + ', '.join(flow['missing_ids'])
    if notes:
        body += '\n\nAgent instructions:\n' + notes
    body += f'\n\nUpload or photograph the requested documents using this secure link:\n{link}\n\nYour existing application and accepted documents remain saved. You can return to this link when the outstanding documents are available.\n\nMartin\'s Funerals'
    app.status = 'FICA Outstanding'
    db.session.commit()
    try:
        sent = send_email(email, 'Outstanding items for your application: ' + app.application_ref, body, [],
            html_body=signing_email_html(app, link, body, 'Upload outstanding documents'), application_id=app.id)
    except Exception:
        current_app.logger.exception('Outstanding-document email failed for application %s', app.id)
        sent = False
    db.session.add(AuditLog(user_id=current_user.id, action='Agent requested outstanding items', entity_type='ClientApplication',
        entity_id=str(app.id), details=f"{'Accepted by mail server' if sent else 'Delivery failed'}; recipient {email}; items: {', '.join(labels)}; instructions: {notes}"))
    db.session.commit()
    flash('Request email accepted by the mail server. The application is waiting for the client.' if sent else
          'Request saved, but the email failed. Review the address and use Send request email to retry.', 'success' if sent else 'danger')
    return redirect(url_for('documents.application_documents', app_id=app.id))


@documents_bp.route("/application/<int:app_id>/resend-missing/<channel>", methods=["POST"])
@login_required
def resend_missing(app_id, channel):
    if not _can_manage_documents():
        flash("Only managers/compliance users can resend document requests.", "danger")
        return redirect(url_for("main.dashboard"))
    app = ClientApplication.query.get_or_404(app_id)
    ensure_branch_access(app, agent_attr="agent_id")
    summary = document_summary(app)
    missing_labels = [row["label"] for row in summary["missing"]]
    if not missing_labels:
        flash("No missing documents to request.", "success")
        return redirect(url_for("documents.application_documents", app_id=app.id))

    screened, errors=ensure_screened(app)
    if not screened:
        flash("; ".join(errors),"danger")
        return redirect(url_for("documents.application_documents",app_id=app.id))
    _ensure_active_signing_link(app)

    base_url = current_app.config.get("BASE_URL") or request.url_root.rstrip("/")
    endpoint = 'signing.supporting_documents' if app.signed_at else 'signing.sign_application'
    link = f"{base_url}{url_for(endpoint, token=app.sign_token)}"
    body = (
        f"Dear {_client_name(app)},\n\n"
        "Martin's Funerals still needs the following documents/signatures to complete your application:\n"
        + "\n".join(f"- {label}" for label in missing_labels)
        + f"\n\nPlease use this secure link to complete them:\n{link}\n\nThank you."
    )

    sent = False
    if channel == "email":
        if not app.email:
            flash("This application has no email address.", "danger")
            return redirect(url_for("documents.application_documents", app_id=app.id))
        sent = send_email(app.email, "Martin's Funerals missing documents", body, [], application_id=app.id)
    elif channel == "whatsapp":
        if not app.cell_number:
            flash("This application has no cellphone number.", "danger")
            return redirect(url_for("documents.application_documents", app_id=app.id))
        from app.services.whatsapp_service import send_application_link
        sent = send_application_link(app, link, body).ok

    else:
        abort(404)

    db.session.add(AuditLog(user_id=current_user.id, action="Missing Documents Requested", entity_type="ClientApplication", entity_id=str(app.id), details=f"Channel: {channel}; Missing: {', '.join(missing_labels)}"))
    db.session.commit()
    if sent:
        flash(f"Missing document request sent by {channel}.", "success")
    else:
        flash(f"Request saved but {channel} delivery failed. Check provider settings/logs.", "warning")
    return redirect(url_for("documents.application_documents", app_id=app.id))

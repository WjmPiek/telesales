import json
from datetime import date
from flask import Blueprint, render_template, redirect, url_for, request, flash
from flask_login import login_required, current_user
from app import db
from app.services.document_status_service import document_summary
from app.services.screening_service import ensure_screened
from app.models import ClientApplication, ClientFicaDocument, ComplianceReview, LapsedPolicy, TelesalesScriptSession, AuditLog
from app.services.branch_access import scope_by_branch, ensure_branch_access, selected_branch_arg, branch_choices_from_model

qa_bp = Blueprint('qa', __name__, url_prefix='/qa')

QA_CHECKLIST = [
    ('popia_confirmed', 'POPIA confirmed'),
    ('product_explained', 'Product and benefits explained'),
    ('premium_confirmed', 'Premium, joining fee and debit day confirmed'),
    ('waiting_periods', 'Waiting periods and exclusions explained'),
    ('debit_order', 'Debit order authority confirmed'),
    ('beneficiary', 'Beneficiary captured/confirmed'),
    ('signature_complete', 'Client signed required documents'),
    ('fica_complete', 'Required FICA documents received'),
    ('contact_details', 'Client contact details verified'),
    ('no_red_flags', 'No unresolved red flags or complaints'),
]


def _cash_payment(app):
    return (app.payment_method or '').strip().casefold() == 'cash'

def _role_name():
    return str(getattr(getattr(current_user, 'role', None), 'name', '') or '').lower().replace('_', ' ')

def _is_qa_user():
    return _role_name() in {'admin', 'super admin', 'superadmin', 'manager', 'branch manager', 'compliance', 'qa'}

def _branch_scope(query, model):
    branch = request.args.get('branch') or ''
    if branch and hasattr(model, 'branch'):
        query = query.filter(model.branch == branch)
    return query, branch

def _app_client_name(app):
    return ' '.join([x for x in [app.first_names, app.surname] if x]) or app.application_ref

def _latest_script(app):
    q = TelesalesScriptSession.query
    if app.id:
        q = q.filter(TelesalesScriptSession.application_id == app.id)
    if app.lapsed_policy_id:
        q = q.union(TelesalesScriptSession.query.filter(TelesalesScriptSession.lapsed_policy_id == app.lapsed_policy_id))
    try:
        return q.order_by(TelesalesScriptSession.completed_at.desc().nullslast(), TelesalesScriptSession.created_at.desc()).first()
    except Exception:
        return TelesalesScriptSession.query.filter_by(application_id=app.id).order_by(TelesalesScriptSession.created_at.desc()).first()

def _fica_summary(app):
    docs = ClientFicaDocument.query.filter_by(application_id=app.id).order_by(ClientFicaDocument.uploaded_at.desc()).all()
    docs = [d for d in docs if d.status != 'Replaced']
    received = [d for d in docs if d.status in {'Reviewed', 'Approved'}]
    return docs, len(received)

@qa_bp.route('/')
@login_required
def qa_dashboard():
    if not _is_qa_user():
        flash('Only managers/compliance users can access QA.', 'danger')
        return redirect(url_for('main.dashboard'))

    branch = selected_branch_arg()
    app_q = scope_by_branch(ClientApplication.query, ClientApplication, agent_col=ClientApplication.agent_id, selected_branch=branch)
    lead_q = scope_by_branch(LapsedPolicy.query, LapsedPolicy, agent_col=LapsedPolicy.assigned_agent_id, selected_branch=branch)

    qa_statuses = ['Signed', 'QA Review', 'QA Pending', 'Application Started', 'Signing Link Sent', 'Signing Link Prepared', 'Submitted', 'FICA Review', 'FICA Outstanding', 'QA Approved']
    qa_apps = app_q.filter(ClientApplication.status.in_(qa_statuses)).order_by(ClientApplication.updated_at.desc()).limit(50).all()
    signed_apps = app_q.filter(ClientApplication.signed_at.isnot(None), ClientApplication.status.notin_(['Active', 'Compliance Approved', 'Compliance Rejected', 'QA Rejected'])).order_by(ClientApplication.signed_at.asc()).limit(50).all()
    fica_docs = scope_by_branch(ClientFicaDocument.query.join(ClientApplication, ClientFicaDocument.application_id == ClientApplication.id), ClientApplication, branch_col=ClientApplication.branch, agent_col=ClientApplication.agent_id, selected_branch=branch).filter(ClientFicaDocument.status.in_(['Received', 'Needs Review']))
    fica_docs = fica_docs.order_by(ClientFicaDocument.uploaded_at.asc()).limit(50).all()
    review_q = ComplianceReview.query.filter(ComplianceReview.application_id.in_(app_q.with_entities(ClientApplication.id)))
    recent_reviews = review_q.order_by(ComplianceReview.created_at.desc()).limit(20).all()

    stats = {
        'qa_pending': len(qa_apps),
        'signed_pending': len(signed_apps),
        'fica_to_review': len(fica_docs),
        'approved_today': review_q.filter(ComplianceReview.decision.in_(['QA Approved', 'Compliance Approved']), db.func.date(ComplianceReview.created_at) == date.today()).count(),
        'rejected_today': review_q.filter(ComplianceReview.decision.in_(['QA Rejected', 'Compliance Rejected']), db.func.date(ComplianceReview.created_at) == date.today()).count(),
    }
    branches = branch_choices_from_model(db, ClientApplication)
    return render_template('qa/dashboard.html', qa_apps=qa_apps, signed_apps=signed_apps, fica_docs=fica_docs, recent_reviews=recent_reviews, stats=stats, branches=branches, active_branch=branch)

@qa_bp.route('/application/<int:app_id>', methods=['GET', 'POST'])
@login_required
def review_application(app_id):
    app = ClientApplication.query.get_or_404(app_id)
    if not _is_qa_user() and not (app.whatsapp_journey and app.agent_id == current_user.id):
        flash('Only authorised staff can verify this application.', 'danger')
        return redirect(url_for('main.dashboard'))
    ensure_branch_access(app, agent_attr='agent_id')
    script = _latest_script(app)
    docs, received_count = _fica_summary(app)
    reviews = ComplianceReview.query.filter_by(application_id=app.id).order_by(ComplianceReview.created_at.desc()).all()

    if request.method == 'POST':
        app = ClientApplication.query.filter_by(id=app.id).with_for_update().populate_existing().one()
        if app.whatsapp_journey and app.whatsapp_journey.activated_at:
            flash('This policy has already been verified and activated.', 'info')
            return redirect(url_for('applications.view_application', app_id=app.id))
        decision = request.form.get('decision') or 'QA Approved'
        if decision not in {'QA Approved', 'QA Rejected', 'Compliance Approved', 'Compliance Rejected'}:
            flash('Invalid review decision.', 'danger')
            return redirect(url_for('qa.review_application', app_id=app.id))
        checked = {key: (request.form.get(key) == 'on') for key, _ in QA_CHECKLIST}
        if _cash_payment(app):
            checked['debit_order'] = True  # Not applicable: no debit-order authority exists for cash.
        score = round(sum(1 for ok in checked.values() if ok) / len(QA_CHECKLIST) * 100)
        notes = request.form.get('notes') or ''
        if decision in {'QA Approved', 'Compliance Approved'} and score < 100:
            outstanding = [label for key, label in QA_CHECKLIST if not checked[key]]
            flash('Approval blocked: confirm the remaining QA checklist items: ' + '; '.join(outstanding) + '.', 'danger')
            return redirect(url_for('qa.review_application', app_id=app.id))

        if decision in {'QA Approved', 'Compliance Approved'}:
            summary = document_summary(app)
            screened, errors = ensure_screened(app)
            if not summary['complete'] or not screened:
                flash('Approval blocked: complete and approve all required documents and the employee FIC check first. ' + '; '.join(errors), 'danger')
                return redirect(url_for('qa.review_application', app_id=app.id))

        if app.whatsapp_journey and decision in {'QA Approved', 'Compliance Approved'}:
            from datetime import datetime
            if not app.signed_at or not app.whatsapp_journey.signed_bundle_at:
                flash('The client must finish and submit the signed application first.', 'danger')
                return redirect(url_for('qa.review_application', app_id=app.id))
            policy_number=request.form.get('policy_number','').strip()
            try:
                start_date=datetime.strptime(request.form.get('start_date',''),'%Y-%m-%d').date()
                if start_date > date.today():raise ValueError()
            except ValueError:
                flash('Enter the actual policy start date (today or earlier) before marking it active.', 'danger')
                return redirect(url_for('qa.review_application', app_id=app.id))
            if not policy_number or len(policy_number)>80:
                flash('Enter the issued policy number before completing verification.', 'danger')
                return redirect(url_for('qa.review_application', app_id=app.id))
            app.policy_number=policy_number;app.inception_date=start_date
            app.whatsapp_journey.activated_at=datetime.utcnow()

        review = ComplianceReview(
            application_id=app.id,
            lapsed_policy_id=app.lapsed_policy_id,
            reviewer_id=current_user.id,
            decision=decision,
            checklist_json=json.dumps(checked),
            score=score,
            notes=notes,
        )
        db.session.add(review)
        app.status = "Active" if app.whatsapp_journey and app.whatsapp_journey.activated_at else decision
        if app.lapsed_policy:
            app.lapsed_policy.recovery_status = 'Approved' if decision in {'QA Approved', 'Compliance Approved'} else 'Rejected'
        db.session.add(AuditLog(user_id=current_user.id, action=decision, entity_type='ClientApplication', entity_id=str(app.id), details=f'QA score {score}%. {notes}'))
        db.session.commit()
        if app.whatsapp_journey and app.whatsapp_journey.activated_at:
            from app.services.online_application import notify_activation
            sent=notify_activation(app)
            flash('Verification completed. Policy is active. '+('Confirmation email sent.' if sent else 'Confirmation email failed; retry from the application.'), 'success' if sent else 'warning')
            return redirect(url_for('applications.view_application', app_id=app.id, office_prompt=1 if sent else None))
        flash(f'{decision} saved with QA score {score}%.', 'success')
        return redirect(url_for('qa.qa_dashboard'))

    checklist_defaults = {key: False for key, _ in QA_CHECKLIST}
    if _cash_payment(app):
        checklist_defaults['debit_order'] = True
    if app.signed_at:
        checklist_defaults['signature_complete'] = True
    if all(row['status'] == 'Approved' for row in document_summary(app)['rows'] if row['key'] in document_summary(app)['required_fica_types']):
        checklist_defaults['fica_complete'] = True
    if script and script.status == 'Completed':
        for key in ['popia_confirmed', 'product_explained', 'premium_confirmed', 'waiting_periods', 'debit_order', 'contact_details']:
            checklist_defaults[key] = True
    return render_template('qa/review_application.html', app=app, script=script, docs=docs, reviews=reviews, checklist=QA_CHECKLIST, checklist_defaults=checklist_defaults, cash_payment=_cash_payment(app))

@qa_bp.route('/fica/<int:doc_id>/<decision>', methods=['POST'])
@login_required
def review_fica(doc_id, decision):
    doc = ClientFicaDocument.query.get_or_404(doc_id)
    if not _is_qa_user() and not (doc.application.whatsapp_journey and doc.application.agent_id == current_user.id):
        flash('Only authorised staff can review these documents.', 'danger')
        return redirect(url_for('main.dashboard'))
    if decision not in {'approve', 'reject'}:
        from flask import abort
        abort(400)
    ensure_branch_access(doc.application, agent_attr='agent_id')
    doc.status = 'Reviewed' if decision == 'approve' else 'Rejected'
    db.session.flush()
    if document_summary(doc.application)['complete'] and doc.application.status not in {'Active', 'QA Approved', 'Compliance Approved'}:
        doc.application.status = 'QA Pending'
    db.session.add(AuditLog(user_id=current_user.id, action=f'FICA {doc.status}', entity_type='ClientFicaDocument', entity_id=str(doc.id), details=doc.original_filename or doc.document_type))
    db.session.commit()
    flash(f'FICA document marked {doc.status}.', 'success')
    return redirect(request.referrer or url_for('qa.qa_dashboard'))


@qa_bp.route('/application/<int:app_id>/retry-confirmation',methods=['POST'])
@login_required
def retry_confirmation(app_id):
    from flask import abort
    app=ClientApplication.query.get_or_404(app_id)
    ensure_branch_access(app,agent_attr='agent_id')
    if not _is_qa_user() and app.agent_id!=current_user.id:abort(403)
    if not app.whatsapp_journey or app.whatsapp_journey.notice_status!='Failed':abort(409)
    from app.services.online_application import notify_activation
    sent=notify_activation(app)
    flash('Confirmation email sent.' if sent else 'Email delivery failed. Check email settings and try again.','success' if sent else 'danger')
    return redirect(url_for('applications.view_application',app_id=app.id,office_prompt=1 if sent else None))

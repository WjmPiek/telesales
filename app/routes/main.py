from datetime import date, datetime, time
from flask import Blueprint, render_template, redirect, url_for, request, flash, abort
from flask_login import login_required, current_user
from app import db
from app.models import ClientApplication, LapsedPolicy, RecoveryCallLog, PolicyProduct, User, TelesalesScriptSession, ClientFicaDocument, ComplianceReview, CompanyGroupState, AuditLog, CommunicationCampaign
from app.services.branch_access import scope_by_branch, selected_branch_arg, branch_choices_from_model, can_view_all_branches, user_branch

main_bp = Blueprint("main", __name__)

@main_bp.route("/")
@login_required
def home():
    return redirect(url_for("recovery.callbacks"))


@main_bp.route("/dashboard")
@login_required
def dashboard():
    today = date.today()
    open_statuses = ["New", "Imported", "Called", "No Answer", "Callback", "Interested", "Application Started", "Signature Sent", "FICA Outstanding", "QA Review"]
    lead_q = scope_by_branch(LapsedPolicy.query, LapsedPolicy, agent_col=LapsedPolicy.assigned_agent_id)
    app_q = scope_by_branch(ClientApplication.query, ClientApplication, agent_col=ClientApplication.agent_id)
    call_q = RecoveryCallLog.query.filter(RecoveryCallLog.agent_id == current_user.id)
    stats = {
        "applications": app_q.count(),
        "products": PolicyProduct.query.count(),
        "lapsed": lead_q.count(),
        "calls": call_q.count(),
        "calls_today": call_q.filter(RecoveryCallLog.created_at >= today).count(),
        "callbacks_today": lead_q.filter(LapsedPolicy.recovery_status == "Callback", LapsedPolicy.next_action_date == today).count(),
        "callbacks_overdue": lead_q.filter(LapsedPolicy.recovery_status == "Callback", LapsedPolicy.next_action_date < today).count(),
        "due_now": lead_q.filter(LapsedPolicy.recovery_status.in_(open_statuses), LapsedPolicy.next_action_date <= today).count(),
        "interested": lead_q.filter(LapsedPolicy.recovery_status == "Interested").count(),
        "applications_started": lead_q.filter(LapsedPolicy.recovery_status == "Application Started").count(),
    }
    todays_calls = RecoveryCallLog.query.filter(RecoveryCallLog.agent_id == current_user.id, RecoveryCallLog.created_at >= today).order_by(RecoveryCallLog.created_at.desc()).limit(10).all()
    return render_template("dashboard/index.html", stats=stats, todays_calls=todays_calls)


@main_bp.route("/login")
def login_alias():
    return redirect(url_for("auth.login"))


@main_bp.route("/healthz")
def healthz():
    return {"ok": True, "service": "Insurance Sales"}


def _is_manager_user():
    role_name = (current_user.role.name if current_user.is_authenticated and current_user.role else "").lower().replace("_", " ")
    return role_name in {"admin", "manager", "branch manager"}


def _today_bounds():
    today = date.today()
    return datetime.combine(today, time.min), datetime.combine(today, time.max)


def _manager_scope(query):
    if not _is_manager_user():
        return query.filter(RecoveryCallLog.agent_id == current_user.id)
    return query


@main_bp.route("/manager")
@login_required
def manager_dashboard():
    """Phase 3 manager dashboard built from existing tables only. No schema changes required."""
    if not _is_manager_user():
        return redirect(url_for("main.dashboard"))

    start, end = _today_bounds()
    branch = selected_branch_arg()

    lead_query = scope_by_branch(LapsedPolicy.query, LapsedPolicy, agent_col=LapsedPolicy.assigned_agent_id, selected_branch=branch)
    app_query = scope_by_branch(ClientApplication.query, ClientApplication, agent_col=ClientApplication.agent_id, selected_branch=branch)
    call_query = RecoveryCallLog.query
    script_query = scope_by_branch(TelesalesScriptSession.query, TelesalesScriptSession, agent_col=TelesalesScriptSession.agent_id, selected_branch=branch)
    fica_query = scope_by_branch(ClientFicaDocument.query.join(ClientApplication, ClientFicaDocument.application_id == ClientApplication.id), ClientApplication, branch_col=ClientApplication.branch, agent_col=ClientApplication.agent_id, selected_branch=branch)

    open_statuses = ["New", "Imported", "Called", "No Answer", "Callback", "Interested", "Application Started", "Signature Sent", "FICA Outstanding", "QA Review"]
    pending_signature_statuses = ["Draft", "Pending Signature", "Signature Sent"]
    pending_qa_statuses = ["QA Review", "Application Started"]

    stats = {
        "total_leads": lead_query.count(),
        "open_leads": lead_query.filter(LapsedPolicy.recovery_status.in_(open_statuses)).count(),
        "calls_today": call_query.filter(RecoveryCallLog.created_at >= start, RecoveryCallLog.created_at <= end).count(),
        "callbacks_today": lead_query.filter(LapsedPolicy.recovery_status == "Callback", LapsedPolicy.next_action_date == date.today()).count(),
        "callbacks_overdue": lead_query.filter(LapsedPolicy.recovery_status == "Callback", LapsedPolicy.next_action_date < date.today()).count(),
        "interested": lead_query.filter(LapsedPolicy.recovery_status == "Interested").count(),
        "applications_started": lead_query.filter(LapsedPolicy.recovery_status == "Application Started").count(),
        "signature_pending": app_query.filter(ClientApplication.status.in_(pending_signature_statuses), ClientApplication.signed_at.is_(None)).count(),
        "qa_pending": lead_query.filter(LapsedPolicy.recovery_status.in_(pending_qa_statuses)).count() + script_query.filter(TelesalesScriptSession.status == "Completed", TelesalesScriptSession.qa_result.is_(None)).count(),
        "compliance_reviews": ComplianceReview.query.count(),
        "fica_received": fica_query.filter(ClientFicaDocument.status == "Received").count(),
        "approved": lead_query.filter(LapsedPolicy.recovery_status.in_(["Approved", "Reinstated"])).count(),
        "rejected": lead_query.filter(LapsedPolicy.recovery_status == "Rejected").count(),
    }
    stats["conversion_rate"] = round((stats["approved"] / stats["total_leads"] * 100), 1) if stats["total_leads"] else 0

    agent_rows = db.session.query(
        User.id, User.name, User.branch,
        db.func.count(RecoveryCallLog.id).label("calls"),
        db.func.sum(db.case((RecoveryCallLog.outcome.in_(["Wants Reinstatement", "Wants New Policy", "Application Started", "Signature Sent"]), 1), else_=0)).label("sales_actions"),
        db.func.sum(db.case((RecoveryCallLog.outcome.in_(["No Answer", "Voicemail"]), 1), else_=0)).label("no_answers"),
    ).outerjoin(RecoveryCallLog, db.and_(RecoveryCallLog.agent_id == User.id, RecoveryCallLog.created_at >= start, RecoveryCallLog.created_at <= end))
    if can_view_all_branches() and branch:
        agent_rows = agent_rows.filter(User.branch == branch)
    elif not can_view_all_branches() and user_branch():
        agent_rows = agent_rows.filter(User.branch == user_branch())
    agent_rows = agent_rows.group_by(User.id, User.name, User.branch).order_by(db.desc("calls"), User.name.asc()).all()

    agents = []
    for row in agent_rows:
        conversion = round((int(row.sales_actions or 0) / int(row.calls or 0) * 100), 1) if row.calls else 0
        agents.append({"id": row.id, "name": row.name, "branch": row.branch, "calls": int(row.calls or 0), "sales_actions": int(row.sales_actions or 0), "no_answers": int(row.no_answers or 0), "conversion": conversion})

    status_counts = db.session.query(LapsedPolicy.recovery_status, db.func.count(LapsedPolicy.id)).group_by(LapsedPolicy.recovery_status).order_by(db.func.count(LapsedPolicy.id).desc()).all()
    if branch:
        status_counts = db.session.query(LapsedPolicy.recovery_status, db.func.count(LapsedPolicy.id)).filter(LapsedPolicy.branch == branch).group_by(LapsedPolicy.recovery_status).order_by(db.func.count(LapsedPolicy.id).desc()).all()

    pending_work = {
        "overdue_callbacks": lead_query.filter(LapsedPolicy.recovery_status == "Callback", LapsedPolicy.next_action_date < date.today()).order_by(LapsedPolicy.next_action_date.asc()).limit(10).all(),
        "today_callbacks": lead_query.filter(LapsedPolicy.recovery_status == "Callback", LapsedPolicy.next_action_date == date.today()).order_by(LapsedPolicy.imported_at.asc()).limit(10).all(),
        "signature_pending": app_query.filter(ClientApplication.signed_at.is_(None)).order_by(ClientApplication.created_at.asc()).limit(10).all(),
        "fica_received": fica_query.filter(ClientFicaDocument.status == "Received").order_by(ClientFicaDocument.uploaded_at.asc()).limit(10).all(),
        "qa_pending": lead_query.filter(LapsedPolicy.recovery_status == "QA Review").order_by(LapsedPolicy.imported_at.asc()).limit(10).all(),
        "recent_reviews": ComplianceReview.query.order_by(ComplianceReview.created_at.desc()).limit(5).all(),
    }

    branches = branch_choices_from_model(db, LapsedPolicy)

    return render_template("dashboard/manager.html", stats=stats, agents=agents, status_counts=status_counts, pending_work=pending_work, branches=branches, active_branch=branch)


# Grouped Franchise / Monthly Figures navigation placeholders
# These keep the main tabs and sub-tabs available for Admin and Franchise/Branch users.
def _manager_or_admin():
    role_name = (current_user.role.name if current_user.is_authenticated and current_user.role else "").lower().replace("_", " ")
    return role_name in {"admin", "super admin", "branch manager", "manager", "supervisor"}

def _portal_guard():
    if not _manager_or_admin():
        return redirect(url_for("role_portals.home"))
    return None

@main_bp.route("/franchise/details")
@login_required
def franchise_details():
    blocked = _portal_guard()
    if blocked: return blocked
    from app.routes.auth import _is_user_management_owner
    branch = selected_branch_arg() or ("All branches" if can_view_all_branches() else user_branch())
    company_expr = db.func.coalesce(CompanyGroupState.company_name, _company_name_expr())
    branch_expr = db.func.coalesce(CompanyGroupState.branch, LapsedPolicy.branch)
    rows_q = db.session.query(
        company_expr.label('company'),
        branch_expr.label('branch'),
        db.func.count(LapsedPolicy.id).label('policies'),
        db.func.sum(db.case((LapsedPolicy.recovery_status == 'Suspense', 1), else_=0)).label('suspense'),
        db.func.sum(db.case((LapsedPolicy.recovery_status.notin_(['Suspense','Closed','Rejected']), 1), else_=0)).label('active_leads'),
    ).outerjoin(CompanyGroupState, LapsedPolicy.company_id == CompanyGroupState.id)
    # Apply the same branch scope to company details.
    if can_view_all_branches() and selected_branch_arg():
        rows_q = rows_q.filter(LapsedPolicy.branch == selected_branch_arg())
    elif not can_view_all_branches() and user_branch():
        rows_q = rows_q.filter(LapsedPolicy.branch == user_branch())
    rows_q = rows_q.group_by(company_expr, branch_expr).order_by(company_expr.asc()).all()
    states = {(s.company_name, s.branch): s.status for s in CompanyGroupState.query.all()}
    show_deleted = request.args.get("show_deleted") == "1"
    rows = [{"company": r.company, "branch": r.branch or "", "policies": int(r.policies or 0),
             "active_leads": int(r.active_leads or 0), "suspense": int(r.suspense or 0),
             "status": states.get((r.company, r.branch or ""), "Active")}
            for r in rows_q]
    if not show_deleted:
        rows = [r for r in rows if r["status"] != "Deleted"]
    cards = [
        {"label":"Scope","value":branch},
        {"label":"Companies","value":len(rows)},
        {"label":"Policies","value":sum(r["policies"] for r in rows)},
        {"label":"Suspense","value":sum(r["suspense"] for r in rows)},
    ]
    return render_template("franchise_companies.html", rows=rows, cards=cards,
                           owner=_is_user_management_owner(current_user), show_deleted=show_deleted)


def _company_name_expr():
    return db.func.coalesce(db.func.nullif(LapsedPolicy.company_name, ''),
                            db.func.nullif(LapsedPolicy.franchise, ''),
                            db.func.nullif(LapsedPolicy.branch, ''), 'Unknown Company')


def _company_policies(name, branch):
    legacy_match = db.and_(_company_name_expr() == name,
                           db.func.coalesce(LapsedPolicy.branch, '') == branch,
                           LapsedPolicy.company_id.is_(None))
    company = CompanyGroupState.query.filter_by(company_name=name, branch=branch).first()
    return LapsedPolicy.query.filter(db.or_(LapsedPolicy.company_id == company.id, legacy_match)) if company else LapsedPolicy.query.filter(legacy_match)


@main_bp.route("/franchise/details/company")
@login_required
def franchise_company_view():
    blocked = _portal_guard()
    if blocked: return blocked
    from app.routes.auth import _is_user_management_owner
    name = (request.args.get("name") or "").strip()
    branch = (request.args.get("branch") or "").strip()
    if not can_view_all_branches() and branch != user_branch():
        abort(403)
    policies = _company_policies(name, branch).order_by(LapsedPolicy.id).all()
    if not name or not policies:
        abort(404)
    state = CompanyGroupState.query.filter_by(company_name=name, branch=branch).first()
    agents = []
    if _is_user_management_owner(current_user):
        agents = [u for u in User.query.filter_by(active=True).order_by(User.name).all()
                  if u.role and u.role.name.lower().replace("_", " ") in {"agent", "sales agent", "staff", "user"}]
    policy_ids = [policy.id for policy in policies]
    recent_calls = RecoveryCallLog.query.filter(RecoveryCallLog.lapsed_policy_id.in_(policy_ids)).order_by(RecoveryCallLog.id.desc()).limit(20).all()
    campaigns = CommunicationCampaign.query.filter_by(company_id=state.id).order_by(CommunicationCampaign.id.desc()).limit(20).all() if state else []
    return render_template("franchise_company_detail.html", name=name, branch=branch,
                           policies=policies, status=state.status if state else "Active",
                           owner=_is_user_management_owner(current_user), agents=agents,
                           assigned_agent_ids={u.id for u in state.agents} if state else set(),
                           recent_calls=recent_calls, campaigns=campaigns, company_id=state.id if state else None)


@main_bp.route("/franchise/details/company/agents", methods=["POST"])
@login_required
def franchise_company_agents():
    from app.routes.auth import _is_user_management_owner
    from app.services.company_groups import get_or_create_company
    if not _is_user_management_owner(current_user):
        abort(403)
    name = (request.form.get("name") or "").strip()
    branch = (request.form.get("branch") or "").strip()
    if not name or not _company_policies(name, branch).first():
        abort(404)
    company = get_or_create_company(name, branch)
    for policy in _company_policies(name, branch).all():
        if policy.company_id is None:
            policy.company_id = company.id
    ids = {int(value) for value in request.form.getlist("agent_ids") if value.isdigit()}
    agents = User.query.filter(User.id.in_(ids), User.active.is_(True)).all() if ids else []
    if len(agents) != len(ids) or any(not user.role or user.role.name.lower().replace("_", " ") not in
                                      {"agent", "sales agent", "staff", "user"} for user in agents):
        abort(400)
    company.agents = agents
    db.session.add(AuditLog(user_id=current_user.id, action="COMPANY_AGENTS_ASSIGNED",
                            entity_type="CompanyGroup", entity_id=str(company.id),
                            details=f"{name} ({branch}) assigned agent ids: {sorted(ids)}"))
    db.session.commit()
    flash("Company agent assignments saved.", "success")
    return redirect(url_for("main.franchise_company_view", name=name, branch=branch))


@main_bp.route("/franchise/details/company/manage", methods=["POST"])
@login_required
def franchise_company_manage():
    from app.routes.auth import _is_user_management_owner
    from app.services.company_groups import get_or_create_company
    if not _is_user_management_owner(current_user):
        abort(403)
    name = (request.form.get("name") or "").strip()
    branch = (request.form.get("branch") or "").strip()
    action = (request.form.get("action") or "").strip()
    policies = _company_policies(name, branch).all()
    if not name or not policies:
        abort(404)
    state = CompanyGroupState.query.filter_by(company_name=name, branch=branch).first()
    if action == "rename":
        new_name = (request.form.get("new_name") or "").strip()
        if not new_name or len(new_name) > 160:
            flash("Enter a company name of up to 160 characters.", "danger")
            return redirect(url_for("main.franchise_company_view", name=name, branch=branch))
        if new_name != name and (_company_policies(new_name, branch).first() or
                                 CompanyGroupState.query.filter_by(company_name=new_name, branch=branch).first()):
            flash("That company already exists in this branch. Nothing was changed.", "danger")
            return redirect(url_for("main.franchise_company_view", name=name, branch=branch))
        state = state or get_or_create_company(name, branch)
        for policy in policies:
            policy.company_id = state.id
            policy.company_name = new_name
            policy.franchise = new_name
        state.company_name = new_name
        target_name = new_name
        audit_action = "COMPANY_RENAMED"
    elif action in {"suspend", "activate", "delete", "restore"}:
        state = state or get_or_create_company(name, branch)
        for policy in policies:
            if policy.company_id is None:
                policy.company_id = state.id
        state.status = {"suspend": "Suspended", "activate": "Active",
                        "delete": "Deleted", "restore": "Active"}[action]
        target_name = name
        audit_action = "COMPANY_" + action.upper()
    else:
        abort(400)
    db.session.add(AuditLog(user_id=current_user.id, action=audit_action,
                            entity_type="CompanyGroup", entity_id=f"{branch}/{name}",
                            details=f"{name} ({branch}) action={action}; linked policies={len(policies)}; resulting name={target_name}. Linked policies retained."))
    db.session.commit()
    flash("Company updated. Linked policies and client records were retained.", "success")
    return redirect(url_for("main.franchise_company_view", name=target_name, branch=branch))

@main_bp.route("/franchise/employees")
@login_required
def franchise_employees():
    blocked = _portal_guard()
    if blocked: return blocked
    return redirect(url_for("auth.users_employees"))

@main_bp.route("/monthly/performance")
@login_required
def monthly_performance():
    blocked = _portal_guard()
    if blocked: return blocked
    apps = scope_by_branch(ClientApplication.query, ClientApplication).count()
    leads = scope_by_branch(LapsedPolicy.query, LapsedPolicy).count()
    calls = RecoveryCallLog.query.count() if can_view_all_branches() else RecoveryCallLog.query.filter(RecoveryCallLog.agent_id == current_user.id).count()
    cards = [{"label":"Leads","value":leads},{"label":"Applications","value":apps},{"label":"Calls","value":calls},{"label":"Conversion","value":f"{round((apps/leads*100),1) if leads else 0}%"}]
    return render_template("franchise_page.html", title="Performance", subtitle="Monthly performance summary for the selected scope.", headers=[], rows=[], cards=cards)

@main_bp.route("/monthly/figures")
@login_required
def monthly_figures():
    blocked = _portal_guard()
    if blocked: return blocked
    return redirect(url_for("main.monthly_performance"))

@main_bp.route("/monthly/royalties")
@login_required
def monthly_royalties():
    blocked = _portal_guard()
    if blocked: return blocked
    return redirect(url_for("main.monthly_performance"))

@main_bp.route("/monthly/finance")
@login_required
def monthly_finance():
    blocked = _portal_guard()
    if blocked: return blocked
    return redirect(url_for("main.monthly_performance"))

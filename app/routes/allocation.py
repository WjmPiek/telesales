
from flask import Blueprint, render_template, request, redirect, url_for, flash, abort
from flask_login import login_required, current_user
from app import db
from app.models import User, LapsedPolicy, AuditLog, CompanyGroupState
from app.security import require_manager_or_admin, is_admin_user
from app.services.branch_access import user_branch, branch_choices_from_model, selected_branch_arg

allocation_bp = Blueprint("allocation", __name__, url_prefix="/allocation")

def _agents_for_branch(branch):
    q = User.query.join(User.role).filter(User.active.is_(True))
    if branch:
        q = q.filter(User.branch == branch)
    return [u for u in q.order_by(User.name.asc()).all() if u.role and u.role.name.lower().replace('_',' ') in {'agent','sales agent','staff','user'}]

@allocation_bp.route("/", methods=["GET", "POST"])
@login_required
def index():
    blocked = require_manager_or_admin()
    if blocked: return blocked
    branch = selected_branch_arg() if is_admin_user() else user_branch()
    company_id = request.form.get("company_id", type=int) if request.method == "POST" else request.args.get("company_id", type=int)
    company = db.session.get(CompanyGroupState, company_id) if company_id else None
    if company_id and not company:
        abort(404)
    if company and not is_admin_user() and company.branch != user_branch():
        abort(403)
    if company:
        branch = company.branch
    companies_q = CompanyGroupState.query.filter(CompanyGroupState.status == "Active")
    if not is_admin_user():
        companies_q = companies_q.filter(CompanyGroupState.branch == user_branch())
    companies = companies_q.order_by(CompanyGroupState.company_name).all()
    def scoped_leads(query):
        if company:
            from app.routes.main import _company_name_expr
            legacy_match = db.and_(LapsedPolicy.company_id.is_(None),
                                   _company_name_expr() == company.company_name,
                                   db.func.coalesce(LapsedPolicy.branch, '') == company.branch)
            return query.filter(db.or_(LapsedPolicy.company_id == company.id, legacy_match))
        if branch:
            return query.filter(LapsedPolicy.branch == branch)
        return query
    if request.method == "POST":
        if not company:
            branch = (request.form.get("branch") or branch) if is_admin_user() else user_branch()
        agent_ids = [int(x) for x in request.form.getlist("agent_ids") if x.isdigit()]
        limit = request.form.get("limit", type=int) or 50
        leads = scoped_leads(LapsedPolicy.query.filter(LapsedPolicy.assigned_agent_id.is_(None)))
        leads = leads.order_by(LapsedPolicy.imported_at.asc()).limit(limit).all()
        if company:
            eligible = [u for u in company.agents if u.active]
            agents = [u for u in eligible if u.id in agent_ids] if agent_ids else eligible
            if agent_ids and len(agents) != len(set(agent_ids)):
                abort(403)
        else:
            eligible = _agents_for_branch(branch)
            agents = [u for u in eligible if u.id in agent_ids] if agent_ids else eligible
            if agent_ids and len(agents) != len(set(agent_ids)):
                abort(403)
        if not agents:
            flash("No active agents found for this branch.", "warning")
            return redirect(url_for("allocation.index", branch=branch or ""))
        count = 0
        for idx, lead in enumerate(leads):
            lead.assigned_agent_id = agents[idx % len(agents)].id
            count += 1
        db.session.add(AuditLog(user_id=current_user.id, action="LEADS_ALLOCATED", entity_type="allocation", entity_id=str(company.id) if company else branch or "all", details=f"Allocated {count} leads across {len(agents)} agents; company={company.company_name if company else 'all'}"))
        db.session.commit()
        flash(f"Allocated {count} leads.", "success")
        return redirect(url_for("allocation.index", branch=branch or "", company_id=company.id if company else ""))
    agents = [u for u in company.agents if u.active] if company else _agents_for_branch(branch)
    unassigned = scoped_leads(LapsedPolicy.query.filter(LapsedPolicy.assigned_agent_id.is_(None)))
    branches = branch_choices_from_model(db, LapsedPolicy)
    return render_template("allocation/index.html", branch=branch, branches=branches, companies=companies,
                           company=company, agents=agents, unassigned_count=unassigned.count())

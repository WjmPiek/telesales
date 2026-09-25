from datetime import date, datetime, time, timedelta
from io import StringIO
import csv
from flask import Blueprint, render_template, request, Response, redirect, url_for
from flask_login import login_required, current_user
from app import db
from app.models import ClientApplication, LapsedPolicy, RecoveryCallLog, User, ClientFicaDocument, DocumentSignature, CompanyGroupState, CommunicationCampaign
from app.services.branch_access import scope_by_branch, selected_branch_arg, can_view_all_branches, user_branch

reports_bp = Blueprint('reports', __name__, url_prefix='/reports')

def is_manager():
    role = (current_user.role.name if current_user.is_authenticated and current_user.role else '').lower().replace('_',' ')
    return role in {'admin','super admin','superadmin','manager','branch manager'}

def parse_dates():
    today=date.today(); start=request.args.get('start') or today.replace(day=1).isoformat(); end=request.args.get('end') or today.isoformat()
    try: sd=datetime.strptime(start,'%Y-%m-%d').date()
    except Exception: sd=today.replace(day=1)
    try: ed=datetime.strptime(end,'%Y-%m-%d').date()
    except Exception: ed=today
    return sd,ed

def date_bounds(sd,ed): return datetime.combine(sd,time.min), datetime.combine(ed,time.max)

def _report_companies():
    q = CompanyGroupState.query
    if not can_view_all_branches() and user_branch():
        q = q.filter(CompanyGroupState.branch == user_branch())
    return q.order_by(CompanyGroupState.company_name, CompanyGroupState.branch).all()

def _selected_company():
    company_id = request.args.get('company_id', type=int)
    if not company_id:
        return None
    company = db.session.get(CompanyGroupState, company_id)
    if company is None or (not can_view_all_branches() and company.branch != user_branch()):
        from flask import abort
        abort(403)
    return company

def _company_policy_ids(company):
    from app.routes.main import _company_policies
    return _company_policies(company.company_name, company.branch).with_entities(LapsedPolicy.id)

@reports_bp.route('/')
@login_required
def index():
    if not is_manager(): return redirect(url_for('main.dashboard'))
    sd,ed=parse_dates(); start,end=date_bounds(sd,ed); branch=selected_branch_arg(); company=_selected_company()
    leads=scope_by_branch(LapsedPolicy.query, LapsedPolicy, agent_col=LapsedPolicy.assigned_agent_id, selected_branch=branch); apps=scope_by_branch(ClientApplication.query, ClientApplication, agent_col=ClientApplication.agent_id, selected_branch=branch); calls=RecoveryCallLog.query; docs=scope_by_branch(ClientFicaDocument.query.join(ClientApplication), ClientApplication, branch_col=ClientApplication.branch, agent_col=ClientApplication.agent_id, selected_branch=branch)
    if company:
        policy_ids = _company_policy_ids(company)
        leads = leads.filter(LapsedPolicy.id.in_(policy_ids))
        apps = apps.filter(ClientApplication.lapsed_policy_id.in_(policy_ids))
        calls = calls.filter(RecoveryCallLog.lapsed_policy_id.in_(policy_ids))
        docs = docs.filter(ClientApplication.lapsed_policy_id.in_(policy_ids))
    stats={
      'calls': calls.filter(RecoveryCallLog.created_at>=start, RecoveryCallLog.created_at<=end).count(),
      'applications': apps.filter(ClientApplication.created_at>=start, ClientApplication.created_at<=end).count(),
      'signed': apps.filter(ClientApplication.signed_at>=start, ClientApplication.signed_at<=end).count(),
      'open_leads': leads.filter(LapsedPolicy.recovery_status.in_(['Imported','New','Called','No Answer','Callback','Interested','Application Started','Signature Sent','FICA Outstanding','QA Review'])).count(),
      'approved': leads.filter(LapsedPolicy.recovery_status.in_(['Approved','Reinstated'])).count(),
      'rejected': leads.filter(LapsedPolicy.recovery_status=='Rejected').count(),
      'outstanding_fica': docs.filter(ClientFicaDocument.status.in_(['Received','Rejected'])).count(),
      'pending_signatures': apps.filter(ClientApplication.signed_at.is_(None)).count(),
    }
    stats['conversion_rate']=round((stats['signed']/stats['calls']*100),1) if stats['calls'] else 0
    agent_query=db.session.query(User.name, User.branch, db.func.count(RecoveryCallLog.id).label('calls'), db.func.sum(db.case((RecoveryCallLog.outcome.in_(['Wants Reinstatement','Wants New Policy','Application Started','Signature Sent']),1), else_=0)).label('positive')).outerjoin(RecoveryCallLog, db.and_(RecoveryCallLog.agent_id==User.id, RecoveryCallLog.created_at>=start, RecoveryCallLog.created_at<=end))
    if company: agent_query=agent_query.filter(db.or_(RecoveryCallLog.lapsed_policy_id.in_(policy_ids), RecoveryCallLog.id.is_(None)))
    if can_view_all_branches() and branch: agent_query=agent_query.filter(User.branch==branch)
    elif not can_view_all_branches() and user_branch(): agent_query=agent_query.filter(User.branch==user_branch())
    agent_rows=agent_query.group_by(User.id,User.name,User.branch).order_by(db.desc('calls')).all()
    branch_rows=leads.with_entities(LapsedPolicy.branch, db.func.count(LapsedPolicy.id)).group_by(LapsedPolicy.branch).order_by(db.func.count(LapsedPolicy.id).desc()).limit(30).all()
    status_rows=leads.with_entities(LapsedPolicy.recovery_status, db.func.count(LapsedPolicy.id)).group_by(LapsedPolicy.recovery_status).order_by(db.func.count(LapsedPolicy.id).desc()).all()
    campaign_count = CommunicationCampaign.query.filter_by(company_id=company.id).count() if company else CommunicationCampaign.query.count()
    return render_template('reports/index.html', stats=stats, agents=agent_rows, branches=branch_rows, statuses=status_rows, start_date=sd, end_date=ed, selected_branch=branch, companies=_report_companies(), selected_company=company, campaign_count=campaign_count)

@reports_bp.route('/export.csv')
@login_required
def export_csv():
    if not is_manager(): return redirect(url_for('main.dashboard'))
    sd,ed=parse_dates(); start,end=date_bounds(sd,ed); company=_selected_company()
    output=StringIO(); w=csv.writer(output)
    w.writerow(['Agent','Branch','Calls','Positive Outcomes'])
    query=db.session.query(User.name, User.branch, db.func.count(RecoveryCallLog.id), db.func.sum(db.case((RecoveryCallLog.outcome.in_(['Wants Reinstatement','Wants New Policy','Application Started','Signature Sent']),1), else_=0))).outerjoin(RecoveryCallLog, db.and_(RecoveryCallLog.agent_id==User.id, RecoveryCallLog.created_at>=start, RecoveryCallLog.created_at<=end))
    if company: query=query.filter(db.or_(RecoveryCallLog.lapsed_policy_id.in_(_company_policy_ids(company)), RecoveryCallLog.id.is_(None)))
    if not can_view_all_branches() and user_branch(): query=query.filter(User.branch==user_branch())
    rows=query.group_by(User.id,User.name,User.branch).all()
    for r in rows: w.writerow([r[0],r[1] or '', int(r[2] or 0), int(r[3] or 0)])
    return Response(output.getvalue(), mimetype='text/csv', headers={'Content-Disposition':'attachment; filename=telesales_report.csv'})

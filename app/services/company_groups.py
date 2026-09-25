from app import db
from app.models import CompanyGroupState


def company_group_name(policy):
    return (policy.company.company_name if policy.company else None) or policy.company_name or policy.franchise or policy.branch or "Unknown Company"


def company_is_suspended(policy):
    if policy.company_id:
        return CompanyGroupState.query.filter_by(id=policy.company_id).filter(
            CompanyGroupState.status.in_(["Suspended", "Deleted"])).first() is not None
    return CompanyGroupState.query.filter_by(company_name=company_group_name(policy), branch=policy.branch or "").filter(
        CompanyGroupState.status.in_(["Suspended", "Deleted"])).first() is not None


def get_or_create_company(name, branch):
    name = (name or branch or "Unknown Company").strip()
    branch = (branch or "").strip()
    company = CompanyGroupState.query.filter_by(company_name=name, branch=branch).first()
    if company is None:
        company = CompanyGroupState(company_name=name, branch=branch, status="Active")
        db.session.add(company)
        db.session.flush()
    return company


def company_for_policy(policy):
    if policy.company_id:
        return db.session.get(CompanyGroupState, policy.company_id)
    return CompanyGroupState.query.filter_by(
        company_name=company_group_name(policy), branch=policy.branch or ""
    ).first()

"""Consolidate company links without deleting clients, documents or history."""
import json
from app import db
from app.models import (CompanyGroupState, LapsedPolicy, ClientApplication, CommunicationCampaign,
                        CompanyDocument, HistoricalMemberCover, SystemSetting, AuditLog)
from app.services.franchise_routing import destination_company, is_northcliff, key


def enabled():
    return SystemSetting.query.filter_by(category='Companies', key='two_companies_only', value='1', active=True).first() is not None


def north_company(company):
    return bool(company and (is_northcliff(company.company_name) or is_northcliff(company.branch)))


def preview():
    rows=[]
    for c in CompanyGroupState.query.order_by(CompanyGroupState.id).all():
        rows.append(dict(id=c.id, name=c.company_name, branch=c.branch, status=c.status,
            destination="Martin's Northcliff" if north_company(c) else "Martin's Brokers",
            clients=LapsedPolicy.query.filter_by(company_id=c.id).count(),
            applications=ClientApplication.query.filter_by(company_id=c.id).count(),
            campaigns=CommunicationCampaign.query.filter_by(company_id=c.id).count(),
            documents=CompanyDocument.query.filter_by(company_id=c.id).count(),
            covers=HistoricalMemberCover.query.filter_by(company_id=c.id).count()))
    return rows


def consolidate(actor_id):
    before=preview()
    brokers=destination_company(); north=destination_company(True)
    targets={False:brokers, True:north}
    companies=CompanyGroupState.query.with_for_update().all()
    mapping={c.id:targets[north_company(c)] for c in companies}
    # Refuse conflicting historical cover records rather than discarding either.
    seen=set()
    for row in HistoricalMemberCover.query.all():
        unique=(mapping[row.company_id].id,row.policy_number,row.id_number)
        if unique in seen: raise ValueError('Historical member cover records conflict. No company data was changed.')
        seen.add(unique)
    for policy in LapsedPolicy.query.all():
        target=mapping.get(policy.company_id) or targets[is_northcliff(policy.branch) or is_northcliff(policy.franchise)]
        policy.company_id=target.id;policy.company_name=target.company_name;policy.branch=target.branch
        # Preserve franchise, policy IDs, source data, preferences and assignments.
    db.session.flush()
    for application in ClientApplication.query.all():
        target=mapping.get(application.company_id) or targets[is_northcliff(application.branch)]
        if key(application.payment_method)=='debitorder': target=brokers
        application.company_id=target.id;application.branch=target.branch
    for campaign in CommunicationCampaign.query.with_for_update().all():
        if campaign.queue_status=='processing': raise ValueError('A campaign is sending. Wait for it to finish before consolidating.')
        recipient_targets={r.policy.company_id for r in campaign.recipients if r.policy}
        if len(recipient_targets)==1:
            target=next(c for c in targets.values() if c.id in recipient_targets)
        elif len(recipient_targets)>1:
            raise ValueError(f'Campaign #{campaign.id} contains both companies. Separate it before consolidating.')
        else:
            target=mapping.get(campaign.company_id) or targets[is_northcliff(campaign.branch)]
        campaign.company_id=target.id;campaign.branch=target.branch
    for model in (CompanyDocument,HistoricalMemberCover):
        for row in model.query.all(): row.company_id=mapping[row.company_id].id
    for company in companies:
        target=mapping[company.id]
        if company.id not in {brokers.id,north.id}:
            for agent in list(company.agents):
                if agent not in target.agents: target.agents.append(agent)
            company.agents=[]
            # Use the product's reversible company-deletion state.
            company.status='Deleted'
    brokers.company_name="Martin's Brokers";north.company_name="Martin's Northcliff"
    brokers.status=north.status='Active'
    # Names were normalized after mapping; reflect them in client company labels.
    for policy in LapsedPolicy.query.all(): policy.company_name=targets[policy.company_id==north.id].company_name
    setting=SystemSetting.query.filter_by(category='Companies',key='two_companies_only').first()
    if setting is None:
        setting=SystemSetting(category='Companies',key='two_companies_only');db.session.add(setting)
    setting.value='1';setting.active=True;setting.updated_by_id=actor_id
    db.session.add(AuditLog(user_id=actor_id,action='COMPANIES_CONSOLIDATED',entity_type='CompanyGroup',
        entity_id=str(brokers.id)+','+str(north.id),details=json.dumps({'before':before,'brokers':brokers.id,'northcliff':north.id})))
    return brokers,north

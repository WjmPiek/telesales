"""Business routing for franchise imports and Northcliff debit-order applications."""
import re
from app import db
from app.models import CompanyGroupState, AuditLog


def key(value):
    return re.sub(r'[^a-z0-9]', '', str(value or '').casefold())


def is_northcliff(value):
    return key(value) in {'northcliff', 'martinsnorthcliff', 'martinsfuneralsnorthcliff'}


def destination_company(northcliff=False):
    branch = 'NORTHCLIFF' if northcliff else 'Brokers'
    names = {'northcliff','martinsnorthcliff','martinsfuneralsnorthcliff'} if northcliff else {'brokers','martinsbrokers'}
    matches = [c for c in CompanyGroupState.query.all() if key(c.branch) == key(branch) and key(c.company_name) in names]
    if len(matches) > 1:
        raise ValueError('More than one company matches this franchise. Correct the company groups before importing.')
    if matches:
        company = matches[0]
    else:
        company = CompanyGroupState(company_name="Martin's Funerals NORTHCLIFF" if northcliff else "Martin's Brokers",branch=branch,status='Active')
        db.session.add(company); db.session.flush()
    if company.status != 'Active':
        raise ValueError('This franchise is suspended or deleted. Reactivate it before continuing.')
    return company


def route_debit_order_application(application):
    if key(application.payment_method) != 'debitorder' or not is_northcliff(application.branch):
        return False
    company = destination_company()
    application.company_id = company.id
    application.branch = company.branch
    # Keep the imported client and campaign provenance in Northcliff.
    db.session.add(AuditLog(action='Debit order application routed to Brokers',entity_type='ClientApplication',
        entity_id=str(application.id or application.application_ref),
        details='Northcliff application moved to Brokers after Debit Order was selected. Original imported client and campaign links retained.'))
    return True

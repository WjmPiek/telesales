"""The agent document check and the hand-off to final quality review."""
from app import db
from app.models import AuditLog
from app.services.document_status_service import document_summary


FINAL_STATUSES = {'Active', 'QA Approved', 'Compliance Approved', 'QA Rejected', 'Compliance Rejected', 'Cancelled', 'Declined'}


def workflow_state(application):
    from app.services.member_count_review import count_summary
    from app.services.cover_eligibility import application_members
    from app.services.screening_service import ensure_screened
    summary = document_summary(application)
    active = bool(application.status == 'Active' or (application.whatsapp_journey and application.whatsapp_journey.activated_at))
    closed = active or application.status in FINAL_STATUSES
    blockers = []
    if not application.signed_at or (application.whatsapp_journey and not application.whatsapp_journey.signed_bundle_at):
        blockers.append('Client must finish and submit the signed application.')
    count = count_summary(application)
    if count['missing']:
        blockers.append(f"Confirm {count['missing']} missing member(s) with the client.")
    ids = [member['name'] or ('Principal member' if index == 0 else f'Additional member {index}')
           for index, member in enumerate(application_members(application)) if not member['id_number']]
    if ids:
        blockers.append('Capture valid member ID numbers: ' + ', '.join(ids) + '.')
    for row in summary['rows']:
        if (row.get('required') or row['status'] == 'Needs Review') and row['group'] != 'Employee FIC' and row['status'] not in {'Signed', 'Approved'}:
            blockers.append(row['label'] + ': ' + row['status'] + '.')
    screened, _ = ensure_screened(application)
    if not screened:
        blockers.append('Complete the employee FIC check and resolve any possible match.')
    requests = [row for row in summary['rows'] if row['key'] in summary['required_fica_types'] and row['group'] == 'FICA'
                and row['status'] in {'Missing', 'Rejected', 'Needs Review'}]
    return {'summary': summary, 'blockers': blockers, 'ready': not blockers and not closed,
            'active': active, 'closed': closed, 'client_requests': requests, 'missing_ids': ids,
            'stage': 4 if active else 3 if not blockers else 2 if application.signed_at else 1}


def advance_to_quality(application, *, actor_id=None):
    """Call inside a staff mutation transaction. Readiness never activates a policy."""
    state = workflow_state(application)
    if state['ready'] and application.status not in {'QA Pending', 'QA Review'}:
        application.status = 'QA Pending'
        db.session.add(AuditLog(user_id=actor_id, action='Ready for quality review', entity_type='ClientApplication',
            entity_id=str(application.id), details='Required documents accepted, member details confirmed, signatures and FIC check complete. Awaiting a final staff decision.'))
    elif not state['closed'] and not state['ready'] and application.status in {'QA Pending', 'QA Review'}:
        application.status = 'FICA Review'
    return state['ready']

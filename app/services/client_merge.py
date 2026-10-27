"""Merge submitted details into the explicitly linked imported client."""
from app import db
from app.models import LapsedPolicy, AuditLog


def merge_application_details(application):
    if not application.lapsed_policy_id:
        return None
    lead = db.session.get(LapsedPolicy, application.lapsed_policy_id)
    if lead is None:
        return None
    # Link by the persisted application/recipient relationship, never by a shared phone.
    fields = {'first_names': 'initials', 'surname': 'surname', 'id_number': 'id_number',
              'cell_number': 'cell_number', 'email': 'email_address', 'home_tel': 'home_tel'}
    changed = []
    for source, target in fields.items():
        value = str(getattr(application, source, None) or '').strip()
        if value:
            length = getattr(LapsedPolicy.__table__.columns[target].type, 'length', None)
            value = value[:length] if length else value
            if getattr(lead, target) != value:
                setattr(lead, target, value)
                changed.append(target)
    address = application.residential_address or application.address
    if address and address != lead.address:
        lead.address = address
        changed.append('address')
    if changed:
        db.session.add(AuditLog(action='CLIENT_DETAILS_MERGED', entity_type='LapsedPolicy', entity_id=str(lead.id),
                               details=f'Application {application.id}: updated {", ".join(changed)}. Original report and receipt history retained.'))
    return lead

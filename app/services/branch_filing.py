"""Approved policy filing with an immutable recipient snapshot for each attempt."""
import json
from datetime import datetime, timedelta
from pathlib import Path
from app import db
from app.models import ClientApplication, BranchFilingOffice, ApplicationFilingDelivery, AuditLog
from app.services.client_storage import application_folder
from app.services.email_service import send_email

APPROVED_STATUSES = {'Active', 'QA Approved', 'Compliance Approved'}


def filing_branch(application):
    selected = (application.filing_branch or '').strip()
    if selected:
        return selected
    default = BranchFilingOffice.query.filter_by(is_default=True, active=True).first()
    return default.branch_name if default else ''


def filing_office(application):
    return BranchFilingOffice.query.filter_by(branch_key=filing_branch(application).lower(), active=True).first()


def latest_delivery(application):
    return ApplicationFilingDelivery.query.filter_by(application_id=application.id).order_by(ApplicationFilingDelivery.id.desc()).first()


def send_branch_pack(application_id, actor_id=None, resend=False):
    application = ClientApplication.query.filter_by(id=application_id).with_for_update().populate_existing().one()
    if application.status not in APPROVED_STATUSES or not application.signed_at:
        raise ValueError('Approve and sign the application before sending a branch filing copy.')
    branch = filing_branch(application)
    office = filing_office(application)
    recipient = office.email if office else None
    previous = ApplicationFilingDelivery.query.filter_by(application_id=application_id).order_by(ApplicationFilingDelivery.id.desc()).first()
    # Serialize attempts and prevent duplicate automatic sends, including concurrent approvals.
    if previous and previous.status == 'Sending':
        if not resend or previous.attempted_at > datetime.utcnow() - timedelta(minutes=5):
            db.session.commit()
            return previous
        previous.status = 'Interrupted / unknown'
        previous.details = 'Staff explicitly retried a send interrupted over five minutes ago; previous delivery is unknown.'
    accepted = ApplicationFilingDelivery.query.filter_by(application_id=application_id, branch_name=branch, recipient_email=recipient, status='Accepted by mail server').first()
    if accepted and not resend:
        db.session.commit()
        return accepted
    attempt = ApplicationFilingDelivery(application_id=application_id, branch_name=branch, recipient_email=recipient, actor_id=actor_id, contact_name=' '.join(filter(None, [office.contact_first_name, office.contact_surname])) if office else '', status='Sending')
    db.session.add(attempt)
    db.session.flush()
    def audit():
        db.session.add(AuditLog(user_id=actor_id, action='BRANCH_FILING_DELIVERY', entity_type='ClientApplication', entity_id=str(application_id), details=f'Application={application.application_ref}; contact={attempt.contact_name}; branch={branch or "Missing"}; recipient={recipient or "Not configured"}; status={attempt.status}; {attempt.details or ""}'))
    if not office:
        attempt.status = 'Not configured'
        attempt.details = 'No active filing email is configured for this branch. No documents were sent.'
        audit(); db.session.commit()
        return attempt
    folder = Path(application_folder(application))
    filenames = [f'signed_application_{application_id}.pdf', f'welcome_pack_{application_id}.pdf', f'popia_consent_{application_id}.pdf', f'policy_disclosure_{application_id}.pdf', f'annexure_j1_{application_id}.pdf']
    paths = [folder / filename for filename in filenames]
    def valid_pdf(path):
        if not path.is_file():
            return False
        with path.open('rb') as stream:
            return stream.read(4) == b'%PDF'
    if not all(valid_pdf(path) for path in paths):
        attempt.status = 'Failed'
        attempt.details = 'Complete five signed policy PDFs are unavailable. No email was sent.'
        audit(); db.session.commit()
        return attempt
    attempt.attachments_json = json.dumps(filenames)
    attempt.details = 'Sending five signed policy PDFs.'
    audit(); db.session.commit()
    body = (f'Dear {attempt.contact_name or branch},\n\nApproved policy {application.policy_number or application.application_ref} for {application.first_names or ""} {application.surname or ""}.\n\n'
            f'Filing branch: {branch}. Please update the client file and retain the attached signed application, welcome pack, POPIA consent, policy disclosure and Annexure J.1 securely for branch records.')
    try:
        sent = send_email(recipient, f'Approved application for {branch} filing: {application.application_ref}', body, [str(path) for path in paths], application_id=application_id)
    except Exception:
        sent = False
    attempt.status = 'Accepted by mail server' if sent else 'Failed'
    attempt.accepted_at = datetime.utcnow() if sent else None
    attempt.details = 'Five signed policy PDFs accepted by the mail server; branch receipt and filing are not confirmed.' if sent else 'Email send failed. Check the branch email and mail service, then retry.'
    audit(); db.session.commit()
    return attempt


def seed_default_office():
    """Seed the user-provided default once; never overwrite edits or undo deletion."""
    from sqlalchemy import text
    from app.models import SystemSetting
    if db.engine.dialect.name == 'postgresql':
        db.session.execute(text('SELECT pg_advisory_xact_lock(74290138)'))
    marker = SystemSetting.query.filter_by(category='branch_filing', key='default_seeded_v1').first()
    if not marker:
        office = BranchFilingOffice.query.filter_by(branch_key="martin's brokers").first()
        if not office:
            office = BranchFilingOffice(branch_key="martin's brokers", branch_name="Martin's Brokers", contact_first_name='Lowhann', contact_surname='Barkhuizen', email='lowhann@martinsdirect.com', is_default=True)
            db.session.add(office)
        db.session.add(SystemSetting(category='branch_filing', key='default_seeded_v1', value='1', active=True))
    db.session.commit()

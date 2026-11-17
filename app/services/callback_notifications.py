"""Durable branch callback emails, queued after a new client callback request."""
from datetime import datetime, timedelta
from flask import current_app
from app import db
from app.models import CallbackEmailNotification, BranchFilingOffice, ClientImportBatch, ClientImportCampaign
from app.services.campaign_schedule import schedule_sast


def queue_callback_email(recipient):
    if CallbackEmailNotification.query.filter_by(recipient_id=recipient.id).first():
        return
    campaign=recipient.campaign
    office=BranchFilingOffice.query.filter_by(branch_key=(campaign.filing_branch or '').lower(),active=True).first()
    batch=ClientImportBatch.query.join(ClientImportCampaign).filter(ClientImportCampaign.campaign_id==campaign.id).first()
    from app.services.response_reconciliation import insert_once
    insert_once(CallbackEmailNotification,dict(recipient_id=recipient.id,branch=campaign.branch,
        import_description=batch.description if batch else recipient.policy.source_filename,
        to_email=office.email if office else None,requested_at=recipient.responded_at or datetime.utcnow(),
        status='Pending' if office and office.email else 'Not configured',attempts=0),['recipient_id'])


def process_callback_emails(limit=10):
    from app.services.email_service import send_email
    from app.services.marketing_consent import telephone_blocked
    from app.services.response_reconciliation import due_jobs
    ids=[row.id for row in due_jobs(CallbackEmailNotification).order_by(
        CallbackEmailNotification.id).limit(limit).all()]
    processed=0
    for notification_id in ids:
        claimed=due_jobs(CallbackEmailNotification).filter_by(id=notification_id).update(
            {'status':'Sending','attempted_at':datetime.utcnow(),'attempts':CallbackEmailNotification.attempts+1},synchronize_session=False)
        db.session.commit()
        if not claimed:
            continue
        item=db.session.get(CallbackEmailNotification,notification_id)
        policy=item.recipient.policy
        if telephone_blocked(policy):
            item.status='Cancelled';db.session.commit();continue
        link=current_app.config['BASE_URL'].rstrip('/')+f'/client-files/?policy_id={policy.id}'
        body=(f'A client requested a callback.\n\nBranch: {item.branch or "Unknown"}\n'
              f'Import: {item.import_description or "Not linked to an import"}\n'
              f'Requested: {schedule_sast(item.requested_at)} SAST\n'
              f'Client: {policy.initials or ""} {policy.surname or ""}\n'
              f'Cell number: {policy.cell_number or "Not supplied"}\n'
              f'Policy: {policy.policy_number or "Not supplied"}\n\n'
              f'Current client status: {policy.recovery_status or "Not recorded"}\n\n'
              f'Open the client file (staff login required):\n{link}')
        try:
            sent=send_email(item.to_email,f'Callback requested — {item.branch or "Branch"}',body,policy_id=policy.id)
        except Exception:
            sent=False
        item.status='Accepted by mail server' if sent else 'Failed'
        item.accepted_at=datetime.utcnow() if sent else None
        item.next_attempt_at=None if sent else datetime.utcnow()+timedelta(minutes=5*item.attempts)
        db.session.commit();processed+=1
    return processed

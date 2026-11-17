"""Reconcile historical and new staff notifications through durable mail jobs."""
import json
from datetime import datetime, timedelta
from flask import current_app
from sqlalchemy import inspect, text, or_
from app import db
from app.models import (ApplicationSubmissionNotification, CallbackEmailNotification, CallbackEmailCopy, ClientApplication, CommunicationCampaign,
                        CampaignRecipient, AuditLog)


def ensure_notification_tables():
    CallbackEmailCopy.__table__.create(db.engine,checkfirst=True)
    ApplicationSubmissionNotification.__table__.create(db.engine,checkfirst=True)
    columns={c['name'] for c in inspect(db.engine).get_columns('callback_email_notifications')}
    with db.engine.begin() as conn:
        if 'attempts' not in columns:
            conn.execute(text('ALTER TABLE callback_email_notifications ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0'))
        if 'next_attempt_at' not in columns:
            conn.execute(text('ALTER TABLE callback_email_notifications ADD COLUMN next_attempt_at TIMESTAMP'))


def insert_once(model, values, keys):
    if db.engine.dialect.name=='postgresql':
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    return db.session.execute(insert(model).values(**values).on_conflict_do_nothing(index_elements=keys))


def queue_submission_notifications(application):
    from app.services.email_service import submission_notification_recipients
    if not application.signed_at:
        return
    accepted={}
    for item in AuditLog.query.filter_by(action='APPLICATION_SUBMISSION_EMAIL',entity_type='ClientApplication',
            entity_id=str(application.id)).order_by(AuditLog.created_at,AuditLog.id):
        try:
            value=json.loads(item.details or '{}')
            accepted[value['to'].lower()]=(value.get('status')=='Accepted by mail server',item.created_at)
        except (ValueError,KeyError,TypeError):
            continue
    # Staff notifications must still reach the branch if a test applicant uses
    # the same address as the branch. Client document receipts are separate.
    from types import SimpleNamespace
    from app.services.application_sources import application_sources
    filing=application.filing_branch or application_sources([application])[application.id]['filing_branch']
    routing=SimpleNamespace(filing_branch=filing)
    recipients=submission_notification_recipients('',routing)
    if not recipients:
        insert_once(ApplicationSubmissionNotification,dict(application_id=application.id,to_email='',
            status='Not configured',created_at=datetime.utcnow(),attempts=0),['application_id','to_email'])
    for email in recipients:
        delivered,at=accepted.get(email.lower(),(False,None))
        insert_once(ApplicationSubmissionNotification,dict(application_id=application.id,to_email=email.lower(),
            status='Accepted by mail server' if delivered else 'Pending',created_at=datetime.utcnow(),
            accepted_at=at if delivered else None,attempts=0),['application_id','to_email'])


def reconcile_branch_notifications():
    from app.services.callback_notifications import queue_callback_email
    for recipient in CampaignRecipient.query.filter(CampaignRecipient.response_type=='callback',
            ~CampaignRecipient.id.in_(db.session.query(CallbackEmailNotification.recipient_id))).all():
        queue_callback_email(recipient)
    from app.models import BranchFilingOffice
    for notice in CallbackEmailNotification.query.filter_by(status='Not configured'):
        office=BranchFilingOffice.query.filter_by(branch_key=(notice.recipient.campaign.filing_branch or '').lower(),active=True).first()
        if office and office.email:
            notice.to_email=office.email;notice.status='Pending'
    from app.services.email_service import office_notification_copies
    for email in office_notification_copies("Martin's Brokers"):
        missing=CallbackEmailNotification.query.join(CampaignRecipient).join(CommunicationCampaign).filter(
            db.func.lower(CommunicationCampaign.filing_branch).in_(["martin's brokers",'brokers']),
            or_(CallbackEmailNotification.to_email!=email,CallbackEmailNotification.to_email.is_(None)),
            ~CallbackEmailCopy.query.filter(CallbackEmailCopy.notification_id==CallbackEmailNotification.id,
                CallbackEmailCopy.to_email==email).exists())
        for notice in missing:
            insert_once(CallbackEmailCopy,dict(notification_id=notice.id,to_email=email,status='Pending',attempts=0),
                ['notification_id','to_email'])
        missing_app=ClientApplication.query.filter(ClientApplication.signed_at.isnot(None),
            db.func.lower(ClientApplication.filing_branch).in_(["martin's brokers",'brokers']),
            ~ApplicationSubmissionNotification.query.filter(ApplicationSubmissionNotification.application_id==ClientApplication.id,
                ApplicationSubmissionNotification.to_email==email).exists())
        for application in missing_app:
            queue_submission_notifications(application)
    for application in ClientApplication.query.filter(ClientApplication.signed_at.isnot(None),
            ~ClientApplication.id.in_(db.session.query(ApplicationSubmissionNotification.application_id))).all():
        queue_submission_notifications(application)
    for notice in ApplicationSubmissionNotification.query.filter_by(status='Not configured',to_email=''):
        queue_submission_notifications(notice.application)
        if ApplicationSubmissionNotification.query.filter(ApplicationSubmissionNotification.application_id==notice.application_id,
                ApplicationSubmissionNotification.to_email!='').first():
            notice.status='Routing updated'
    for model in (CallbackEmailNotification,CallbackEmailCopy,ApplicationSubmissionNotification):
        # The caller holds the notification-worker advisory lock. A stale send
        # therefore belongs to a dead worker, never an active SMTP attempt.
        for notice in model.query.filter(model.status=='Sending',
                model.attempted_at<datetime.utcnow()-timedelta(minutes=10)):
            notice.status='Failed'
            notice.next_attempt_at=datetime.utcnow() if notice.attempts<3 else None
        for notice in model.query.filter(model.status=='Failed',model.next_attempt_at.is_(None),model.attempts<3):
            notice.next_attempt_at=datetime.utcnow()
            notice.attempts=max(notice.attempts,1)
    db.session.commit()


def due_jobs(model):
    return model.query.filter(or_(model.status=='Pending',db.and_(model.status=='Failed',model.attempts<3,
        model.next_attempt_at<=datetime.utcnow())))


def process_submission_emails(limit=10):
    from app.services.email_service import send_email, signing_email_html
    from app.services.application_sources import application_sources
    from app.services.campaign_schedule import schedule_sast
    ids=[item.id for item in due_jobs(ApplicationSubmissionNotification).order_by(ApplicationSubmissionNotification.id).limit(limit)]
    for job_id in ids:
        claimed=due_jobs(ApplicationSubmissionNotification).filter_by(id=job_id).update(dict(status='Sending',
            attempted_at=datetime.utcnow(),attempts=ApplicationSubmissionNotification.attempts+1),synchronize_session=False)
        db.session.commit()
        if not claimed:
            continue
        job=db.session.get(ApplicationSubmissionNotification,job_id)
        app=job.application
        if not app.signed_at:
            job.status='Cancelled';db.session.commit();continue
        source=application_sources([app])[app.id]
        link=current_app.config['BASE_URL'].rstrip('/')+f'/client-files/?application_id={app.id}'
        body=(f'The client has submitted the signed application.\n\nApplication: {app.application_ref}\n'
              f'Client: {app.first_names or ""} {app.surname or ""}\nCell number: {app.cell_number or "Not supplied"}\n'
              f'Branch: {app.branch or "Not recorded"}\nDocuments go to: {app.filing_branch or "Configured office"}\n'
              f'Import: {source["import_description"] or source["filename"] or "Not linked to an import"}\n'
              f'Submitted: {schedule_sast(app.signed_at)} SAST\n\n'
              f'Open the client file (staff login required):\n{link}')
        try:
            ok=send_email(job.to_email,'Signed documents received: '+app.application_ref,body,
                html_body=signing_email_html(app,link,body),application_id=app.id)
        except Exception:
            ok=False
        job.status='Accepted by mail server' if ok else 'Failed'
        job.accepted_at=datetime.utcnow() if ok else None
        job.next_attempt_at=None if ok else datetime.utcnow()+timedelta(minutes=5*job.attempts)
        db.session.add(AuditLog(action='APPLICATION_SUBMISSION_EMAIL',entity_type='ClientApplication',entity_id=str(app.id),
            details=json.dumps({'to':job.to_email,'status':job.status,'attempt':job.attempts})))
        db.session.commit()
    return len(ids)

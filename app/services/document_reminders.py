"""Two post-submission supporting-document reminders, never for legacy applications."""
from datetime import datetime, timedelta

from app import db
from app.models import AuditLog, ClientApplication, SupportingDocumentReminder


def schedule_document_reminders(application):
    """Enroll only a newly submitted, signed application."""
    if not application.signed_at:
        return
    if db.session.get(SupportingDocumentReminder, application.id):
        return
    db.session.add(SupportingDocumentReminder(
        application_id=application.id, started_at=application.signed_at))
    db.session.add(AuditLog(action="Supporting reminders scheduled", entity_type="ClientApplication",
                            entity_id=str(application.id), details="First reminder after 24 hours; final reminder 24 hours later if documents remain outstanding."))
    db.session.commit()


def cancel_document_reminders(application_id, *, now=None):
    """Cancel within the caller's transaction once every required upload is received."""
    row = db.session.get(SupportingDocumentReminder, application_id)
    if row and row.cancelled_at is None:
        row.cancelled_at = now or datetime.utcnow()
        db.session.add(AuditLog(action="Supporting reminders cancelled", entity_type="ClientApplication",
                                entity_id=str(application_id), details="All required supporting documents received."))


def process_document_reminders(*, now=None, limit=50):
    """Run from the existing single-worker scheduler or the CLI; return counters."""
    from app.routes.signing import _fica_status, send_supporting_upload_email

    now = now or datetime.utcnow()
    stats = {"sent": 0, "cancelled": 0, "failed": 0}
    candidates = (SupportingDocumentReminder.query
                  .filter(SupportingDocumentReminder.cancelled_at.is_(None),
                          SupportingDocumentReminder.first_sent_at.is_(None),
                          SupportingDocumentReminder.last_sent_at.is_(None),
                          SupportingDocumentReminder.started_at <= now - timedelta(hours=24))
                  .order_by(SupportingDocumentReminder.started_at)
                  .limit(limit).all())
    # The final reminder is eligible only 24 hours after the first was actually sent.
    if len(candidates) < limit:
        candidates += (SupportingDocumentReminder.query
                       .filter(SupportingDocumentReminder.cancelled_at.is_(None),
                               SupportingDocumentReminder.first_sent_at.is_not(None),
                               SupportingDocumentReminder.last_sent_at.is_(None),
                               SupportingDocumentReminder.first_sent_at <= now - timedelta(hours=24))
                       .order_by(SupportingDocumentReminder.first_sent_at)
                       .limit(limit - len(candidates)).all())
    seen = set()
    for candidate in candidates:
        if candidate.application_id in seen:
            continue
        seen.add(candidate.application_id)
        try:
            # PostgreSQL serializes concurrent ticks for the same application.
            row = (SupportingDocumentReminder.query
                   .filter_by(application_id=candidate.application_id)
                   .with_for_update().one())
            application = db.session.get(ClientApplication, row.application_id)
            if not application or row.cancelled_at or row.last_sent_at:
                db.session.rollback()
                continue
            if not application.signed_at or not application.sign_token or application.status in {"Active", "Cancelled", "Declined", "Rejected"}:
                cancel_document_reminders(row.application_id, now=now)
                db.session.commit()
                stats["cancelled"] += 1
                continue
            required, received, outstanding, docs = _fica_status(application)
            if not outstanding:
                cancel_document_reminders(row.application_id, now=now)
                db.session.commit()
                stats["cancelled"] += 1
                continue
            stage = 2 if row.first_sent_at else 1
            due = (row.first_sent_at if stage == 2 else row.started_at) + timedelta(hours=24)
            if now < due or (row.last_attempt_at and now < row.last_attempt_at + timedelta(hours=1)):
                db.session.rollback()
                continue
            row.last_attempt_at = now
            sent = send_supporting_upload_email(application, reminder=True, reminder_stage=stage, commit=False)
            if sent:
                if stage == 1:
                    row.first_sent_at = now
                else:
                    row.last_sent_at = now
                stats["sent"] += 1
            else:
                stats["failed"] += 1
            db.session.commit()
        except Exception:
            db.session.rollback()
            import logging
            logging.getLogger(__name__).exception("Document reminder failed for application %s", candidate.application_id)
            stats["failed"] += 1
    return stats

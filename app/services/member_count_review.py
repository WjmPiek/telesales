"""Confirm the client's requested headcount without treating unused product slots as missing people."""
import html
import secrets
from datetime import datetime, timedelta

from flask import current_app
from sqlalchemy import or_
from app import db
from app.models import ApplicationJourney, ApplicationMemberAddition, ApplicationMemberCountReview, AuditLog, ClientApplication


def count_summary(application):
    from app.services.cover_eligibility import application_members
    actual = len(application_members(application))
    review = db.session.get(ApplicationMemberCountReview, application.id) if application.id else None
    expected = max(int(application.total_members or actual), review.expected_count if review else 0, actual)
    return {'expected':expected, 'captured':actual, 'missing':max(0, expected-actual)}


def review_row(application, summary=None):
    summary = summary or count_summary(application)
    row = db.session.get(ApplicationMemberCountReview, application.id)
    if row is None:
        row = ApplicationMemberCountReview(application_id=application.id, expected_count=summary['expected'])
        db.session.add(row)
    return row


def confirm_current_members(application):
    summary = count_summary(application)
    row = review_row(application, summary)
    row.expected_count = row.confirmed_count = row.captured_count = summary['captured']
    row.confirmed_at = datetime.utcnow()
    row.resolution = 'Client confirmed current members only'
    application.total_members = summary['captured']
    db.session.add(AuditLog(action='Client confirmed member count', entity_type='ClientApplication', entity_id=str(application.id),
        details=f"Client requested no further additions: previously {summary['expected']} requested, {summary['captured']} captured. Existing covered members were retained."))


def invitation_message(application, link, *, pending=False):
    summary = count_summary(application)
    ignore_link = link + '?choice=ignore'
    if pending:
        message = 'The new member is saved. Continue with the updated application signature and submit it.'
    elif summary['missing']:
        message = (f"You requested {summary['expected']} members including yourself, but {summary['captured']} are captured. "
                   f"There are {summary['missing']} member(s) still to confirm. Add the missing person, or confirm that the current member list is correct.")
    else:
        message = 'Add a forgotten member to an available space in your product, or confirm that your current member list is correct.'
    body = (f'Dear {application.first_names or "client"},\n\n{message}\n\nAdd a member / continue:\n{link}\n\n'
            f'Ignore the missing member / confirm the current list:\n{ignore_link}\n\n'
            'Your existing application details and uploads remain saved. Confirming the current list does not remove any captured person. '
            'Adding a person requires the updated application signature and their ID document. This link expires in seven days.')
    escape = html.escape
    add_label = 'Continue member addition' if pending else 'Add a member'
    buttons = (f'<a href="{escape(link, quote=True)}" style="display:inline-block;background:#69499d;color:#fff;padding:14px 18px;border-radius:8px;text-decoration:none;margin:8px 8px 8px 0">{add_label}</a>'
               f'<a href="{escape(ignore_link, quote=True)}" style="display:inline-block;background:#eef0f5;color:#30273e;padding:14px 18px;border-radius:8px;text-decoration:none;margin:8px 0">Ignore missing member</a>') if not pending else (
               f'<a href="{escape(link, quote=True)}" style="display:inline-block;background:#69499d;color:#fff;padding:14px 18px;border-radius:8px;text-decoration:none">Continue member addition</a>')
    html_body = (f'<div style="font:16px/1.6 Arial;max-width:650px;margin:auto;padding:24px"><h2 style="color:#69499d">Martin’s Funerals</h2>'
                 f'<p>Dear {escape(application.first_names or "client")},</p><p>{escape(message)}</p>{buttons}'
                 '<p>Your saved details and documents are retained. “Ignore missing member” opens a secure confirmation page; it does not remove anyone already captured.</p></div>')
    return body, html_body


def record_manual_invitation(application, *, now=None):
    summary = count_summary(application)
    if not summary['missing']:
        return
    row = review_row(application, summary)
    row.expected_count = summary['expected']; row.captured_count = summary['captured']
    row.first_sent_at = row.last_attempt_at = now or datetime.utcnow()
    row.final_sent_at = None


def process_member_count_reminders(*, now=None, limit=25):
    from app.services.member_additions import can_add, available_kinds, snapshot, token_hash
    from app.services.email_service import send_email
    from app.services.delivery_preferences import valid_email
    now = now or datetime.utcnow()
    stats = {'sent':0, 'failed':0}
    # Only self-service applications have an explicitly requested member count.
    candidates = (ClientApplication.query.join(ApplicationJourney).outerjoin(ApplicationMemberCountReview)
        .filter(ClientApplication.signed_at.isnot(None), ClientApplication.total_members > 1,
                ClientApplication.status.notin_(['Active','QA Approved','Compliance Approved','Cancelled','Declined']),
                or_(ApplicationMemberCountReview.confirmed_count.is_(None),
                    ClientApplication.total_members > ApplicationMemberCountReview.confirmed_count))
        .order_by(ClientApplication.signed_at.desc()).limit(max(limit*10, 100)).all())
    for candidate in candidates:
        if stats['sent'] + stats['failed'] >= limit:
            break
        try:
            application = ClientApplication.query.filter_by(id=candidate.id).with_for_update(skip_locked=True).populate_existing().first()
            if not application:
                db.session.rollback(); continue
            summary = count_summary(application)
            if not can_add(application):
                db.session.rollback(); continue
            if not summary['missing']:
                row = review_row(application, summary)
                row.confirmed_count = summary['captured']; row.confirmed_at=now
                row.resolution='All requested members captured'
                db.session.commit(); continue
            if not available_kinds(application):
                db.session.rollback(); continue
            email = (application.document_email or application.email or '').strip()
            if not valid_email(email):
                db.session.rollback(); continue
            row = review_row(application, summary)
            if row.captured_count != summary['captured'] or row.expected_count != summary['expected']:
                row.first_sent_at = row.final_sent_at = None
                row.confirmed_at = None
            row.expected_count = summary['expected']; row.captured_count = summary['captured']
            if row.final_sent_at or (row.last_attempt_at and now < row.last_attempt_at+timedelta(hours=1)):
                db.session.rollback(); continue
            final = bool(row.first_sent_at)
            if final and now < row.first_sent_at+timedelta(hours=24):
                db.session.rollback(); continue
            raw = secrets.token_urlsafe(32)
            addition = ApplicationMemberAddition(application_id=application.id, requested_by_id=None,
                token_hash=token_hash(raw), snapshot=snapshot(application), expires_at=now+timedelta(days=7))
            db.session.add(addition);row.last_attempt_at=now
            db.session.commit()
            adapter = current_app.url_map.bind('localhost')
            link = current_app.config['BASE_URL'].rstrip('/') + adapter.build('member_additions.client_form', {'token':raw})
            body, html_body = invitation_message(application, link)
            sent = send_email(email, f'Please confirm your members: {application.application_ref}', body, [],
                              html_body=html_body, application_id=application.id)
            if sent:
                ApplicationMemberAddition.query.filter(ApplicationMemberAddition.application_id==application.id,
                    ApplicationMemberAddition.id!=addition.id, ApplicationMemberAddition.used_at.is_(None),
                    ApplicationMemberAddition.completed_at.is_(None), ApplicationMemberAddition.revoked_at.is_(None)).update({'revoked_at':now})
                if final: row.final_sent_at=now
                else: row.first_sent_at=now
                stats['sent']+=1
            else:
                addition.revoked_at=now;stats['failed']+=1
            db.session.add(AuditLog(action='Automatic member-count reminder', entity_type='ClientApplication', entity_id=str(application.id),
                details=f"Requested {summary['expected']}; captured {summary['captured']}; {'accepted by mail server' if sent else 'delivery failed'}; stage {2 if final else 1}."))
            db.session.commit()
        except Exception:
            db.session.rollback();stats['failed']+=1
            current_app.logger.exception('Member-count reminder failed for application %s', candidate.id)
    return stats

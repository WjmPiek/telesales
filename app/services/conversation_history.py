"""Client-scoped communication records; never infer identity from an email address."""
import json
import re
from pathlib import Path
from datetime import datetime,timedelta
from app import db
from app.models import ClientCommunication

def safe_body(body):
    # Staff see the message wording, not a reusable client signing credential.
    return re.sub(r'https?://[^\s<>]+/sign/[A-Za-z0-9_-]+', '[Secure application link]',str(body or ''))

def record_communication(channel, body, status, application_id=None, policy_id=None, subject=None, attachments=None, direction='outbound',source='system',occurred_at=None):
    from flask import has_request_context
    from flask_login import current_user
    if not application_id and not policy_id:return
    actor=getattr(current_user,'id',None) if has_request_context() and current_user.is_authenticated else None
    db.session.add(ClientCommunication(application_id=application_id,lapsed_policy_id=policy_id,actor_id=actor,channel=channel,direction=direction,subject=subject,body=safe_body(body),status=status,source=source,occurred_at=occurred_at or datetime.utcnow(),attachments_json=json.dumps([Path(x).name for x in attachments or []])))

def event(at,channel,direction,body,subject='',status='',actor='',source='',attachments=None):
    return dict(at=at or datetime.min,time=((at+timedelta(hours=2)).strftime('%d %b %Y, %H:%M:%S') if at else 'Time unavailable'),channel=channel,direction=direction,body=safe_body(body),subject=subject,status=status,actor=actor,source=source,attachments=attachments or [])

"""Reject byte-identical uploads before creating a second file or document row."""
import hashlib
from flask_login import current_user
from app import db
from app.models import ClientStoredFile, AuditLog, ClientApplication


def reject_duplicate(application, upload, document_type):
    # Serialize uploads for one application on PostgreSQL so concurrent retries cannot duplicate files.
    ClientApplication.query.filter_by(id=application.id).with_for_update().one()
    data = upload.stream.read(25 * 1024 * 1024 + 1)
    upload.stream.seek(0)
    if not data or len(data) > 25 * 1024 * 1024:
        raise ValueError('Choose a non-empty document of 25 MB or less.')
    digest = hashlib.sha256(data).digest()
    for stored in ClientStoredFile.query.filter_by(application_id=application.id).all():
        if hashlib.sha256(stored.content).digest() == digest:
            db.session.add(AuditLog(user_id=current_user.id if current_user.is_authenticated else None,
                action='Duplicate upload rejected', entity_type='ClientApplication', entity_id=str(application.id),
                details=f'{document_type}: {upload.filename}; identical content already stored as file #{stored.id}. Existing document retained.'))
            # Preserve the attempt even when the caller rolls back the rejected upload.
            db.session.commit()
            raise ValueError('This document is already saved. Duplicate upload rejected and recorded in the audit log.')

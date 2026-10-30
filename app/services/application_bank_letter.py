"""A manually selected bank letter belongs to one application and branch."""
import hashlib
import io
import tempfile
from pathlib import Path
from app import db
from app.models import ClientStoredFile, AuditLog


def required(application):
    return str(application.payment_method or '').strip().casefold() in {'cash', 'stop order'}


def relative_path(application):
    branch = str(application.branch or '').strip().casefold()
    digest = hashlib.sha256(f'{application.company_id}:{branch}'.encode()).hexdigest()[:20]
    return f'bank-confirmation/{digest}.pdf'


def saved_letter(application):
    return ClientStoredFile.query.filter_by(application_id=application.id,relative_path=relative_path(application)).first()


def save_letter(application, upload, actor_id):
    from pypdf import PdfReader
    content = upload.read(5 * 1024 * 1024 + 1)
    if not (upload.filename or '').lower().endswith('.pdf') or not content.startswith(b'%PDF') or len(content) > 5 * 1024 * 1024:
        raise ValueError('Attach a PDF bank confirmation letter of 5 MB or smaller.')
    try:
        reader=PdfReader(io.BytesIO(content))
        if reader.is_encrypted or not reader.pages:
            raise ValueError()
    except Exception as exc:
        raise ValueError('Attach a readable, unencrypted bank confirmation PDF.') from exc
    row=saved_letter(application)
    if row is None:
        row=ClientStoredFile(application_id=application.id,relative_path=relative_path(application))
        db.session.add(row)
    row.content=content
    db.session.add(AuditLog(user_id=actor_id,action='Application bank letter attached',entity_type='ClientApplication',
        entity_id=str(application.id),details=f'Manually attached bank confirmation for branch {application.branch or "unspecified"}; company {application.company_id}.'))
    return row


def materialise_letter(application):
    row=saved_letter(application)
    if row is None:
        return None
    folder=Path(tempfile.mkdtemp(prefix='martins_application_bank_'))
    target=folder/'bank_confirmation_letter.pdf'
    target.write_bytes(row.content)
    return str(target)

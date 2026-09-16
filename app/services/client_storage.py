"""Private document folders, grouped by principal member and application."""
import hashlib
from pathlib import Path
from flask import current_app
from sqlalchemy.orm import load_only


def application_folder(application):
    # Hash the identifier so identity numbers are not exposed in filesystem paths.
    identity = ''.join(c for c in str(application.id_number or '') if c.isalnum()).upper()
    key = hashlib.sha256(identity.encode()).hexdigest() if identity else f'application-{application.id}'
    root = Path(current_app.config['UPLOAD_FOLDER']).resolve()
    folder = root / 'clients' / key / f'application-{int(application.id)}'
    folder.mkdir(parents=True, exist_ok=True)
    from app.models import ClientStoredFile
    for stored in ClientStoredFile.query.options(load_only(ClientStoredFile.id, ClientStoredFile.relative_path)).filter_by(application_id=application.id).all():
        target = (folder / stored.relative_path).resolve()
        if folder not in target.parents:
            raise ValueError('Invalid stored document path')
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(stored.content)
    return str(folder)


def store_document(application, path):
    from app import db
    from app.models import ClientStoredFile
    folder = Path(application_folder(application))
    file = Path(path).resolve()
    relative = str(file.relative_to(folder)).replace('\\', '/')
    stored = ClientStoredFile.query.filter_by(application_id=application.id, relative_path=relative).first()
    if stored is None:
        stored = ClientStoredFile(application_id=application.id, relative_path=relative)
        db.session.add(stored)
    if file.stat().st_size > 25 * 1024 * 1024:
        raise ValueError("Documents must be 25 MB or smaller")
    stored.content = file.read_bytes()


def durable_pdf(generator):
    from functools import wraps
    @wraps(generator)
    def wrapped(application, out_path, *args, **kwargs):
        result = generator(application, out_path, *args, **kwargs)
        store_document(application, out_path)
        return result
    return wrapped

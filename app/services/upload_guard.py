"""Reject byte-identical uploads before creating a second file or document row."""
import hashlib
import io
from pathlib import Path
from PIL import Image, ImageOps, UnidentifiedImageError
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
    original_digest = hashlib.sha256(data).digest()
    extension = Path(upload.filename or '').suffix.lower()
    if extension in {'.jpg', '.jpeg', '.png', '.webp'}:
        try:
            with Image.open(io.BytesIO(data)) as image:
                if image.width * image.height > 40_000_000:
                    raise ValueError('This photo is too large. Please select a smaller scan or photo (under 40 megapixels).')
                image = ImageOps.exif_transpose(image)
                image.thumbnail((2000, 2000), Image.Resampling.LANCZOS)
                if image.mode in ('RGBA', 'LA') or 'transparency' in image.info:
                    rgba = image.convert('RGBA')
                    rgb = Image.new('RGB', rgba.size, 'white')
                    rgb.paste(rgba, mask=rgba.getchannel('A'))
                else:
                    rgb = image.convert('RGB')
                output = io.BytesIO()
                rgb.save(output, format='JPEG', quality=85, optimize=True)
                if len(output.getvalue()) < len(data):
                    data = output.getvalue()
                    upload.filename = Path(upload.filename).stem + '.jpg'
                    upload.headers['Content-Type'] = 'image/jpeg'
        except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
            raise ValueError('The photo could not be read. Please upload a JPG, PNG or PDF copy.') from exc
    # Leave PDFs byte-for-byte intact, including any digital signatures.
    if len(data) > 8 * 1024 * 1024:
        raise ValueError('Please use a document of 8 MB or less. Save large scans as a smaller PDF before uploading.')
    upload.stream.close()
    upload.stream = io.BytesIO(data)
    digest = hashlib.sha256(data).digest()
    for stored in ClientStoredFile.query.filter_by(application_id=application.id).all():
        if hashlib.sha256(stored.content).digest() in {digest, original_digest}:
            db.session.add(AuditLog(user_id=current_user.id if current_user and current_user.is_authenticated else None,
                action='Duplicate upload rejected', entity_type='ClientApplication', entity_id=str(application.id),
                details=f'{document_type}: {upload.filename}; identical content already stored as file #{stored.id}. Existing document retained.'))
            # Preserve the attempt even when the caller rolls back the rejected upload.
            db.session.commit()
            raise ValueError('This document is already saved. Duplicate upload rejected and recorded in the audit log.')

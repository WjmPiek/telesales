"""Private, bounded recording storage, including safe reads of legacy files."""
from pathlib import Path
from flask import current_app

MAX_RECORDING_BYTES = 25 * 1024 * 1024


def validate_recording(filename, data):
    if not data or len(data) > MAX_RECORDING_BYTES:
        raise ValueError('Choose a non-empty recording of 25 MB or less.')
    extension = Path(filename).suffix.lower()
    signatures = {
        '.wav': (len(data) >= 12 and data[:4] == b'RIFF' and data[8:12] == b'WAVE', 'audio/wav'),
        '.mp3': (data.startswith(b'ID3') or (len(data) >= 2 and data[0] == 255 and data[1] & 0xE0 == 0xE0), 'audio/mpeg'),
        '.ogg': (data.startswith(b'OggS'), 'audio/ogg'),
        '.flac': (data.startswith(b'fLaC'), 'audio/flac'),
        '.m4a': (len(data) >= 12 and data[4:8] == b'ftyp', 'audio/mp4'),
        '.webm': (data.startswith(b'\x1a\x45\xdf\xa3'), 'audio/webm'),
    }
    valid, mime = signatures.get(extension, (False, None))
    if not valid:
        raise ValueError('Upload a WAV, MP3, OGG, FLAC, M4A or WEBM recording with matching file contents.')
    return mime


def ensure_recording_columns():
    """Upgrade existing deployments before ORM queries select the new columns."""
    from sqlalchemy import inspect, text
    from app import db
    inspector = inspect(db.engine)
    if not inspector.has_table('call_recordings'):
        return
    columns = {column['name'] for column in inspector.get_columns('call_recordings')}
    binary_type = 'BYTEA' if db.engine.dialect.name == 'postgresql' else 'BLOB'
    if_not_exists = 'IF NOT EXISTS ' if db.engine.dialect.name == 'postgresql' else ''
    with db.engine.begin() as conn:
        for name, sql_type in [('file_data', binary_type), ('content_type', 'VARCHAR(100)')]:
            if name not in columns:
                conn.execute(text(f'ALTER TABLE call_recordings ADD COLUMN {if_not_exists}{name} {sql_type}'))


def recording_bytes(recording):
    if recording.file_data is not None:
        return recording.file_data
    # Legacy paths are never served directly or accepted outside recording folders.
    try:
        path = Path(recording.file_path).resolve()
    except (OSError, ValueError):
        return None
    roots = [
        Path(current_app.config['UPLOAD_FOLDER']) / 'call_recordings',
        Path(current_app.root_path) / 'static' / 'uploads' / 'call_recordings',
        Path(current_app.instance_path) / 'uploads' / 'call_recordings',
    ]
    if not any(root.resolve() in path.parents for root in roots):
        return None
    if not path.is_file() or path.stat().st_size > MAX_RECORDING_BYTES:
        return None
    with path.open('rb') as source:
        data = source.read(MAX_RECORDING_BYTES + 1)
    try:
        mime = validate_recording(recording.original_filename or path.name, data)
    except ValueError:
        return None
    from app import db
    recording.file_data = data
    recording.content_type = mime
    db.session.commit()
    return data

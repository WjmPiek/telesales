"""Run in the current Render shell BEFORE redeploying disk-only recordings.

This script is standalone so it can run against the previously deployed app.
It adds nullable storage columns and copies available files without deleting them.
"""
import os
from pathlib import Path
from sqlalchemy import create_engine, inspect, text

MAX_BYTES = 25 * 1024 * 1024


def preserve(engine, roots):
    inspector = inspect(engine)
    if not inspector.has_table('call_recordings'):
        return {'saved': 0, 'unavailable': 0}
    columns = {column['name'] for column in inspector.get_columns('call_recordings')}
    postgres = engine.dialect.name == 'postgresql'
    with engine.begin() as conn:
        for name, kind in [('file_data', 'BYTEA' if postgres else 'BLOB'), ('content_type', 'VARCHAR(100)')]:
            if name not in columns:
                clause = 'IF NOT EXISTS ' if postgres else ''
                conn.execute(text(f'ALTER TABLE call_recordings ADD COLUMN {clause}{name} {kind}'))
    with engine.connect() as conn:
        rows = conn.execute(text('SELECT id, file_path FROM call_recordings WHERE file_data IS NULL')).mappings().all()
    roots = [Path(root).resolve() for root in roots]
    saved = unavailable = 0
    for row in rows:
        try:
            path = Path(row['file_path']).resolve()
            if not any(root in path.parents for root in roots):
                raise ValueError('Outside recording folders')
            with path.open('rb') as source:
                data = source.read(MAX_BYTES + 1)
            if not data or len(data) > MAX_BYTES:
                raise ValueError('Empty or oversized recording')
        except (OSError, ValueError):
            unavailable += 1
            continue
        with engine.begin() as conn:
            result = conn.execute(text('UPDATE call_recordings SET file_data=:data WHERE id=:id AND file_data IS NULL'),
                                  {'id': row['id'], 'data': data})
            saved += result.rowcount
    return {'saved': saved, 'unavailable': unavailable}


if __name__ == '__main__':
    database_url = os.environ['DATABASE_URL']
    if database_url.startswith('postgres://'):
        database_url = database_url.replace('postgres://', 'postgresql://', 1)
    root = Path.cwd()
    folders = [root / 'app/static/uploads/call_recordings', root / 'instance/uploads/call_recordings']
    if os.getenv('UPLOAD_FOLDER'):
        folders.append(Path(os.environ['UPLOAD_FOLDER']) / 'call_recordings')
    engine = create_engine(database_url)
    try:
        counts = preserve(engine, folders)
        print(counts)
        if counts['unavailable']:
            raise SystemExit('Some legacy recordings are missing, outside recording folders, empty or over 25 MB. Review and back them up before redeploying.')
    finally:
        engine.dispose()

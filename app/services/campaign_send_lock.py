"""A sender lock released by PostgreSQL when its process or connection dies."""
from contextlib import contextmanager
from threading import Lock
from sqlalchemy import text
from app import db

_locks = {}
_guard = Lock()


@contextmanager
def campaign_send_lock(campaign_id):
    if db.engine.dialect.name == 'postgresql':
        with db.engine.connect() as conn:
            params = {'namespace': 1952803955, 'campaign': campaign_id}
            acquired = bool(conn.execute(text('SELECT pg_try_advisory_lock(:namespace, :campaign)'), params).scalar())
            conn.commit()
            try:
                yield acquired
            finally:
                if acquired:
                    conn.execute(text('SELECT pg_advisory_unlock(:namespace, :campaign)'), params)
                    conn.commit()
    else:
        with _guard:
            lock = _locks.setdefault(campaign_id, Lock())
        acquired = lock.acquire(blocking=False)
        try:
            yield acquired
        finally:
            if acquired:
                lock.release()


def sender_active(campaign):
    if campaign.queue_status != 'processing':
        return False
    with campaign_send_lock(campaign.id) as acquired:
        return not acquired

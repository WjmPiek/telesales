"""Campaign dates are entered in SAST and stored as naive UTC."""
from datetime import datetime, timedelta, timezone

SAST = timezone(timedelta(hours=2))


def parse_schedule(value):
    value = datetime.fromisoformat(value)
    if value.tzinfo is None:
        value = value.replace(tzinfo=SAST)
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def schedule_sast(value):
    return (value + timedelta(hours=2)).strftime('%d %b %Y %H:%M') if value else ''

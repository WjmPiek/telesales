"""Recorded evidence only: this module never queries a public opt-out form."""
import json
import re
from datetime import datetime, timedelta

from sqlalchemy import case, func, or_, select

from app import db
from app.models import MarketingScreening, LapsedPolicy, SystemSetting
from app.services.communication_service import normalize_phone, preference_for, is_suppressed

REGISTRIES = {'dmasa': 'DMASA', 'waspa': 'WASPA DNC', 'ncc': 'NCC OORS'}
RESULTS = {'listed': 'Listed — do not market', 'clear': 'Not listed',
           'not_checked': 'Not checked', 'expired': 'Expired', 'error': 'Check failed',
           'checked': 'Check recorded (any result)'}
WEBSITES = {'dmasa': 'https://dmasa.org/page/about-opt-out-service',
            'waspa': 'https://dnc.org.za/about', 'ncc': 'https://thencc.org.za/'}


def valid_phone(value):
    number = normalize_phone(value)
    if not re.fullmatch(r'27[1-9][0-9]{8}', number):
        raise ValueError('Enter a valid South African number.')
    return number


def phone_expression():
    """Conservative SQL equivalent for common imported SA phone formats."""
    number = func.trim(func.coalesce(LapsedPolicy.cell_number, ''))
    for char in (' ', '+', '-', '(', ')', '.', '/', '\t', '\r', '\n'):
        number = func.replace(number, char, '')
    number = case((func.substr(number, 1, 2) == '00', func.substr(number, 3)), else_=number)
    return case((func.substr(number, 1, 1) == '0', '27' + func.substr(number, 2)),
                (func.length(number) == 9, '27' + number), else_=number)


def result_expression(registry, now=None):
    now = now or datetime.utcnow()
    def latest(column, conclusive=False):
        query = select(column).where(MarketingScreening.phone_number == phone_expression(),
                                     MarketingScreening.registry == registry,
                                     MarketingScreening.checked_at <= now)
        if conclusive:
            query = query.where(MarketingScreening.result.in_(['listed', 'clear']))
        return (query
                .order_by(MarketingScreening.checked_at.desc(), MarketingScreening.id.desc())
                .limit(1).correlate(LapsedPolicy).scalar_subquery())
    result, expiry = latest(MarketingScreening.result), latest(MarketingScreening.expires_at)
    return case((latest(MarketingScreening.result, conclusive=True) == 'listed', 'listed'),
                (result.is_(None), 'not_checked'),
                ((result == 'clear') & (or_(expiry.is_(None), expiry <= now)), 'expired'),
                else_=result)


def screening_condition(registry, result):
    providers = [registry] if registry else REGISTRIES
    if result == 'checked':
        return or_(*(result_expression(provider) != 'not_checked' for provider in providers))
    return or_(*(result_expression(provider) == result for provider in providers))


def latest_results(phone, now=None):
    now = now or datetime.utcnow()
    rows = MarketingScreening.query.filter_by(phone_number=normalize_phone(phone)).filter(
        MarketingScreening.checked_at <= now).order_by(
        MarketingScreening.checked_at.desc(), MarketingScreening.id.desc()).all()
    latest = {}
    conclusive = {}
    for row in rows:
        latest.setdefault(row.registry, row)
        if row.result in {'listed', 'clear'}:
            conclusive.setdefault(row.registry, row)
    for provider, row in conclusive.items():
        if row.result == 'listed':
            latest[provider] = row
    return latest


def result_status(row, now=None):
    if not row:
        return 'not_checked'
    if row.result == 'clear' and (not row.expires_at or row.expires_at <= (now or datetime.utcnow())):
        return 'expired'
    return row.result


def registry_policy():
    row = SystemSetting.query.filter_by(category='MarketingCompliance', key='registry_policy', active=True).first()
    try:
        value = json.loads(row.value) if row else None
        if (not isinstance(value, dict) or not isinstance(value.get('required'), list)
                or any(item not in REGISTRIES for item in value['required'])
                or not str(value.get('reference', '')).strip()):
            return None
        return value
    except (ValueError, TypeError):
        return None


def clearance(policy):
    """One gate for manual, scheduled and follow-up WhatsApp marketing."""
    pref = preference_for(policy)
    if pref.opted_out_all or not pref.whatsapp_allowed or is_suppressed(policy) or policy.recovery_status == 'Opted Out':
        return False, 'Client opted out or is suppressed'
    try:
        valid_phone(policy.cell_number)
    except ValueError as exc:
        return False, str(exc)
    latest = latest_results(policy.cell_number)
    for provider, label in REGISTRIES.items():
        if result_status(latest.get(provider)) == 'listed':
            return False, f'Listed on {label}'
    consent = latest.get('whatsapp')
    if not consent or consent.result != 'granted':
        return False, 'Explicit WhatsApp marketing consent is missing or withdrawn'
    configuration = registry_policy()
    if configuration is None:
        return True, 'Consent recorded; external screening is not configured'
    for provider in configuration['required']:
        status = result_status(latest.get(provider))
        if status != 'clear':
            return False, f'{REGISTRIES[provider]}: {RESULTS.get(status, status)}'
    return True, 'Cleared for WhatsApp marketing'


def parse_sast(value, required=True):
    if not value and not required:
        return None
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is not None:
            raise ValueError()
        return parsed - timedelta(hours=2)
    except (ValueError, TypeError):
        raise ValueError('Enter a valid date and time in South African time.')


def make_evidence(policy, provider, result, checked, expiry, reference, user_id, source='manual'):
    phone = valid_phone(policy.cell_number)
    allowed = {'granted', 'withdrawn'} if provider == 'whatsapp' else {'listed', 'clear', 'error'}
    if provider not in {*REGISTRIES, 'whatsapp'} or result not in allowed:
        raise ValueError('Choose a valid register and result.')
    if not checked or checked > datetime.utcnow() + timedelta(minutes=1):
        raise ValueError('The check/consent date cannot be in the future.')
    if result == 'clear' and (not expiry or expiry <= checked):
        raise ValueError('A not-listed result needs a validity end after the check date.')
    reference = (reference or '').strip()
    if not reference or len(reference) > 1000:
        raise ValueError('Provide an evidence reference (up to 1,000 characters).')
    return MarketingScreening(lapsed_policy_id=policy.id, phone_number=phone, registry=provider,
                             result=result, checked_at=checked, expires_at=expiry if result == 'clear' else None,
                             reference=reference, recorded_by_id=user_id, source=source)

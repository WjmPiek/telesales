"""Resolve per-member cover and waiting periods from the selected product."""
from decimal import Decimal

from app.services.compliance_service import age_from_dob, dob_from_sa_id, format_dob


def member_limits(product):
    """Return safe per-product member row limits for application forms."""
    rules = getattr(product, 'rules', None)
    configured = str(getattr(rules, 'plan_type', '') or '').strip().lower()
    if not configured:
        text = f"{getattr(product, 'product_name', '')} {getattr(product, 'plan_name', '')}".lower()
        configured = 'member_product' if 'member +' in text or 'member+' in text else 'family'

    def limit(field, default, maximum=30):
        try:
            return max(0, min(int(getattr(rules, field, default) if rules else default), maximum))
        except (TypeError, ValueError):
            return default

    if configured == 'member_product':
        return {'plan_type': configured, 'spouse': 0, 'child': 0, 'extended': 0,
                'productdep': limit('extra_member_slots', 13)}
    if configured == 'single':
        return {'plan_type': configured, 'spouse': 0, 'child': 0, 'extended': 0, 'productdep': 0}
    return {'plan_type': 'family', 'spouse': limit('spouse_slots', 1, 2),
            'child': limit('child_slots', 6), 'extended': limit('extended_slots', 6), 'productdep': 0}


def _positive(value, fallback=0):
    try:
        number = Decimal(str(value or 0))
    except Exception:
        number = Decimal(str(fallback or 0))
    return number if number > 0 else Decimal(str(fallback or 0))


def member_benefit(product, kind, id_or_dob=''):
    """Return locked cover and waiting period for one covered person."""
    base_cover = _positive(getattr(product, 'cover_amount', 0))
    rules = getattr(product, 'rules', None)
    waiting = f"{getattr(product, 'waiting_period_months', 0) or 0} months"
    kind = str(kind or '').lower()
    age = age_from_dob(dob_from_sa_id(id_or_dob) or id_or_dob)

    if kind == 'principal':
        cover = _positive(getattr(rules, 'main_member_cover', 0), base_cover)
    elif kind == 'spouse':
        cover = _positive(getattr(rules, 'spouse_cover', 0), base_cover)
    elif kind == 'extended':
        cover = _positive(getattr(rules, 'extended_cover', 0), base_cover)
    elif kind == 'productdep':
        field = 'member_0_5_product_only' if age is not None and age <= 5 else 'member_6_70_product_only'
        cover = _positive(getattr(rules, field, 0), base_cover)
    else:
        if age is None:
            cover = base_cover
        elif age < 1:
            cover = _positive(getattr(rules, 'family_0_11', 0), base_cover)
        elif age <= 5:
            cover = _positive(getattr(rules, 'family_1_5', 0), base_cover)
        elif age <= 13:
            cover = _positive(getattr(rules, 'family_6_13', 0), base_cover)
        else:
            cover = _positive(getattr(rules, 'family_14_21', 0), base_cover)
    dob = format_dob(dob_from_sa_id(id_or_dob) or id_or_dob)
    return {
        'cover': format(cover, '.2f'),
        'waiting_period': waiting,
        'date_of_birth': dob,
        'age': age,
    }


def enrich_rows(product, kind, rows):
    enriched = []
    for source in rows:
        row = dict(source)
        row.update(member_benefit(product, kind, row.get('id_or_dob')))
        row['kind'] = kind
        enriched.append(row)
    return enriched

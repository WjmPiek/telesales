"""Resolve per-member cover and waiting periods from the selected product."""
from decimal import Decimal

from app.services.compliance_service import age_from_dob, dob_from_sa_id


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
    return {'cover': format(cover, '.2f'), 'waiting_period': waiting}


def enrich_rows(product, kind, rows):
    enriched = []
    for source in rows:
        row = dict(source)
        row.update(member_benefit(product, kind, row.get('id_or_dob')))
        row['kind'] = kind
        enriched.append(row)
    return enriched

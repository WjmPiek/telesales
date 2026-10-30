"""Append a forgotten member without replacing the existing application details."""
import hashlib
import json
from datetime import datetime

from app.models import ApplicationMemberAddition
from app.services.member_benefits import member_limits, enrich_rows


def token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def rows(raw):
    try:
        value = json.loads(raw or '[]')
        return value if isinstance(value, list) else []
    except (TypeError, ValueError):
        return []


def populated(row):
    return isinstance(row, dict) and any(row.get(k) for k in ('full_name', 'id_or_dob', 'id_number', 'date_of_birth'))


def can_add(application):
    journey = application.whatsapp_journey
    return bool(application.product and application.product.active and application.signed_at
                and application.status not in {'Active', 'QA Approved', 'Compliance Approved', 'Cancelled', 'Declined'}
                and not (journey and journey.activated_at))


def pending_addition(application):
    if (application.signed_at or application.sign_token_revoked or application.sign_token_used_at
            or application.status in {'Active','QA Approved','Compliance Approved','Cancelled','Declined'}
            or (application.whatsapp_journey and application.whatsapp_journey.activated_at)):
        return None
    return ApplicationMemberAddition.query.filter_by(application_id=application.id, completed_at=None, revoked_at=None).filter(
        ApplicationMemberAddition.used_at.isnot(None)).order_by(ApplicationMemberAddition.id.desc()).first()


def snapshot(application):
    keys = ('product_id', 'form_template', 'sign_token', 'signed_at', 'spouse_first_names', 'spouse_surname',
            'spouse_id_number', 'spouse_date_of_birth', 'dependents_json', 'extended_family_json', 'product_dependents_json')
    payload = {key: str(getattr(application, key, '') or '') for key in keys}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def available_kinds(application):
    limits = member_limits(application.product)
    if limits['plan_type'] == 'member_product':
        choices = [('productdep', 'Additional member', 'product_dependents_json', limits['productdep'])]
    else:
        choices = [('child', 'Child', 'dependents_json', limits['child']),
                   ('extended', 'Extended family member', 'extended_family_json', limits['extended'])]
    result = []
    for kind, label, field, maximum in choices:
        used = sum(populated(row) for row in rows(getattr(application, field)))
        if maximum > used:
            result.append({'key': kind, 'label': label, 'spaces': maximum-used})
    spouse_present = any((application.spouse_first_names, application.spouse_surname,
                          application.spouse_id_number, application.spouse_date_of_birth))
    spouse_present = spouse_present or any(isinstance(row, dict) and row.get('kind') == 'spouse'
                                           for row in rows(application.product_dependents_json))
    if limits['spouse'] and not spouse_present:
        result.insert(0, {'key': 'spouse', 'label': 'Spouse', 'spaces': 1})
    return result


def append_member(application, form):
    from app.services.compliance_service import (dob_from_sa_id, format_dob, is_valid_sa_id,
                                                  validate_member_age, assert_application_rules)
    if not can_add(application):
        raise ValueError('This application cannot be changed with an add-member link. Please contact your agent.')
    kind = (form.get('member_kind') or '').strip()
    if kind not in {item['key'] for item in available_kinds(application)}:
        raise ValueError('There is no available space for this member type on the selected product.')
    name = (form.get('full_name') or '').strip()
    relationship = (form.get('relationship') or '').strip()
    identifier = (form.get('id_or_dob') or '').strip()
    if not name or len(name) > 150 or not relationship or len(relationship) > 80:
        raise ValueError('Enter the member’s full name and relationship.')
    if len(identifier) == 13 and identifier.isdigit():
        if not is_valid_sa_id(identifier):
            raise ValueError('Enter a valid South African ID number or the date of birth in YYYY-MM-DD format.')
        dob = dob_from_sa_id(identifier)
    else:
        try:
            dob = datetime.strptime(identifier, '%Y-%m-%d').strftime('%d/%m/%Y')
        except ValueError:
            raise ValueError('Enter a valid South African ID number or the date of birth in YYYY-MM-DD format.')
    errors = []
    validate_member_age(name, dob, kind, application.product, errors)
    if errors:
        raise ValueError('; '.join(errors))
    from app.services.cover_eligibility import application_members
    for member in application_members(application):
        same_id = len(identifier) == 13 and member.get('id_number') == identifier
        same_person = (' '.join(str(member.get('name') or '').casefold().split()) == ' '.join(name.casefold().split())
                       and format_dob(member.get('dob')) == format_dob(dob))
        if same_id or same_person:
            raise ValueError('This member is already captured on the application.')
    entry = enrich_rows(application.product, kind, [{'full_name': name, 'relationship': relationship, 'id_or_dob': identifier}])[0]
    combined = rows(application.product_dependents_json)
    if not combined:
        for old_kind, field in [('child', 'dependents_json'), ('extended', 'extended_family_json')]:
            combined.extend(dict(row, kind=old_kind) for row in rows(getattr(application, field)) if populated(row))
        if application.spouse_first_names or application.spouse_surname:
            combined.append({'kind':'spouse', 'full_name':' '.join(filter(None, [application.spouse_first_names, application.spouse_surname])),
                             'relationship':'Spouse', 'id_or_dob':application.spouse_id_number or application.spouse_date_of_birth})
    if kind == 'spouse':
        names = name.rsplit(' ', 1)
        application.spouse_first_names = names[0]
        application.spouse_surname = names[1] if len(names) > 1 else ''
        application.spouse_id_number = identifier if len(identifier) == 13 else ''
        application.spouse_date_of_birth = dob
    elif kind in {'child', 'extended'}:
        field = 'dependents_json' if kind == 'child' else 'extended_family_json'
        members = rows(getattr(application, field))
        empty = next((i for i, row in enumerate(members) if not populated(row)), None)
        if empty is None:
            members.append(entry)
        else:
            members[empty] = entry
        setattr(application, field, json.dumps(members))
    empty = next((i for i, row in enumerate(combined) if not populated(row)), None) if kind == 'productdep' else None
    if empty is None:
        combined.append(entry)
    else:
        combined[empty] = entry
    application.product_dependents_json = json.dumps(combined)
    application.total_members = max(int(application.total_members or 0), len(application_members(application)))
    ok, errors = assert_application_rules(application)
    if not ok:
        raise ValueError('; '.join(errors))
    return name


def review_return(application):
    """A verified client returns to the focused addition flow after signing."""
    from flask import session, url_for
    saved = session.get(f'member_addition_{application.id}') or {}
    token = saved.get('token', '')
    addition = ApplicationMemberAddition.query.filter_by(id=saved.get('id'), application_id=application.id,
        token_hash=token_hash(token)).first() if token else None
    if addition and addition.used_at and not addition.completed_at and not addition.revoked_at and addition.expires_at > datetime.utcnow():
        return url_for('member_additions.client_form', token=token)
    return None

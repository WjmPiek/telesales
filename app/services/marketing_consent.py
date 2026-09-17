"""Explicit POPIA choices, applied atomically to the Insurance Sales marketing lists."""
from datetime import datetime
from app import db
from app.models import (ApplicationMarketingConsent, ContactSuppression, LapsedPolicy,
                        WhatsAppContact, AuditLog)
from app.services.communication_service import normalize_phone,normalize_email,contact_hash,preference_for,is_suppressed


def consent_value(a):
    row=ApplicationMarketingConsent.query.filter_by(application_id=a.id).first()
    return row.allowed if row else None


def apply_consent(a, allowed, ip=None, user_agent=None):
    row=ApplicationMarketingConsent.query.filter_by(application_id=a.id).first()
    if not row:
        row=ApplicationMarketingConsent(application_id=a.id,allowed=allowed)
        db.session.add(row)
    row.allowed=allowed;row.recorded_at=datetime.utcnow();row.ip_address=ip;row.user_agent=user_agent
    phones={normalize_phone(x) for x in [a.cell_number,a.home_tel,a.work_tel] if normalize_phone(x)}
    email=normalize_email(a.email)
    hashes={contact_hash(x) for x in phones}
    eh=contact_hash(email)
    checks=[]
    if hashes:checks.append(ContactSuppression.phone_hash.in_(hashes))
    if eh:checks.append(ContactSuppression.email_hash==eh)
    matches=ContactSuppression.query.filter(db.or_(*checks)).all() if checks else []
    # Fresh explicit consent supersedes this portal's earlier marketing refusal.
    # Administrative blocks or opt-outs from other sources require their own review.
    if allowed:
        for item in matches:
            if item.source=='popia':db.session.delete(item)
    else:
        for number in phones:
            if not any(x.phone_hash==contact_hash(number) for x in matches):
                db.session.add(ContactSuppression(phone_hash=contact_hash(number),source='popia',reason='POPIA marketing consent declined'))
        if eh and not any(x.email_hash==eh for x in matches):
            db.session.add(ContactSuppression(email_hash=eh,source='popia',reason='POPIA marketing consent declined'))
    db.session.flush()
    policies=[]
    for policy in LapsedPolicy.query.all():
        same_id=bool(a.id_number and policy.id_number==a.id_number)
        same_phone=bool(phones.intersection({normalize_phone(policy.cell_number),normalize_phone(policy.home_tel)}))
        same_email=bool(email and normalize_email(policy.email_address)==email)
        if same_id or same_phone or same_email:policies.append(policy)
    if allowed and not policies and (phones or email):
        policy=LapsedPolicy(initials=a.first_names,surname=a.surname,id_number=a.id_number,
            cell_number=a.cell_number,email_address=a.email,branch=a.branch,assigned_agent_id=a.agent_id,
            recovery_status='Marketing Consented',next_action_date=None,comments='Added after explicit POPIA marketing consent.')
        db.session.add(policy);db.session.flush();policies.append(policy)
    for policy in policies:
        pref=preference_for(policy)
        if not allowed:
            pref.telephone_allowed=pref.whatsapp_allowed=pref.email_allowed=False
            if not pref.opted_out_all:
                pref.opt_out_source='popia';pref.opted_out_at=datetime.utcnow()
            pref.opted_out_all=True
            policy.recovery_status='Opted Out';policy.next_action_date=None
        elif not is_suppressed(policy) and (not pref.opted_out_all or pref.opt_out_source=='popia'):
            pref.telephone_allowed=pref.whatsapp_allowed=pref.email_allowed=True
            pref.opted_out_all=False;pref.opted_out_at=None;pref.opt_out_source=None
            if policy.recovery_status=='Opted Out':policy.recovery_status='Marketing Consented'
    for contact in WhatsAppContact.query.all():
        if normalize_phone(contact.phone_number) in phones or (email and normalize_email(contact.email)==email):
            from app.services.phone_deletion import phone_is_suppressed
            if not allowed:
                if not contact.opted_out:
                    contact.tags=((contact.tags or '')+',popia-opt-out').strip(',')
                contact.opted_out=True;contact.status='Opted Out'
            elif not phone_is_suppressed(contact.phone_number) and 'popia-opt-out' in (contact.tags or '').split(',') and not ContactSuppression.query.filter_by(email_hash=contact_hash(normalize_email(contact.email))).first():
                contact.opted_out=False;contact.status='Marketing Consented'
                contact.tags=','.join(t for t in (contact.tags or '').split(',') if t!='popia-opt-out')
    if allowed and a.cell_number:
        from app.services.phone_deletion import phone_is_suppressed
        number=normalize_phone(a.cell_number)
        existing=WhatsAppContact.query.filter_by(wa_id=number).first()
        if not existing and not phone_is_suppressed(number) and not (eh and ContactSuppression.query.filter_by(email_hash=eh).first()):
            db.session.add(WhatsAppContact(wa_id=number,phone_number=number,display_name=f'{a.first_names} {a.surname}',email=a.email,branch=a.branch,assigned_agent_id=a.agent_id,status='Marketing Consented',tags='popia-consent'))
    db.session.add(AuditLog(action='POPIA_MARKETING_CONSENT',entity_type='ClientApplication',entity_id=str(a.id),details='Client selected '+('YES' if allowed else 'NO')))


def telephone_blocked(policy):
    return is_suppressed(policy) or preference_for(policy).opted_out_all or not preference_for(policy).telephone_allowed

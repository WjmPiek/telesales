import json
import secrets
from datetime import datetime
from flask import Blueprint, request, render_template, redirect, url_for, session, abort, flash, send_file, current_app
from flask_login import login_required, current_user
from werkzeug.exceptions import HTTPException
from app import db
from app.models import ClientApplication, ApplicationJourney, CampaignRecipient, PolicyProduct, DocumentSignature, AuditLog
from app.security import permission_required
from app.services.branch_access import ensure_branch_access
from app.services.online_application import FIELDS, BANKS, STANDARD_BRANCH_CODES, save_questionnaire
from app.services.cdd_service import FIELDS as CDD_FIELDS, answers_for
from app.services.marketing_consent import consent_value
from app.services.signature_fields import signed_documents

online_bp=Blueprint('online_application',__name__,url_prefix='/online-application')

@online_bp.route('/campaign/<int:recipient_id>',methods=['GET','POST'])
@login_required
@permission_required('applications.create')
def prepare(recipient_id):
    recipient=CampaignRecipient.query.get_or_404(recipient_id)
    policy=recipient.policy
    ensure_branch_access(policy,agent_attr='assigned_agent_id')
    from app.services.communication_service import is_suppressed, preference_for
    pref=preference_for(policy)
    if is_suppressed(policy) or pref.opted_out_all or not pref.whatsapp_allowed:
        abort(403)
    existing=ClientApplication.query.join(ApplicationJourney).filter(ClientApplication.lapsed_policy_id==policy.id,ApplicationJourney.campaign_id==recipient.campaign_id).first()
    if existing:return redirect(url_for('applications.view_application',app_id=existing.id))
    products=PolicyProduct.query.filter_by(active=True).all()
    error=None
    if request.method=='POST':
        product=db.session.get(PolicyProduct,request.form.get('product_id',type=int))
        if not product or not product.active:abort(400)
        from app.services.compliance_service import classify_product_template, dob_from_sa_id, assert_application_rules
        a=ClientApplication(application_ref='WA-'+secrets.token_hex(6).upper(),product=product,agent_id=current_user.id,
          branch=policy.branch,lapsed_policy_id=policy.id,first_names=policy.initials,surname=policy.surname,
          id_number=policy.id_number,cell_number=policy.cell_number,email=policy.email_address,
          date_of_birth=dob_from_sa_id(policy.id_number),status='Draft',payment_method='Cash',
          address=policy.address,residential_address=policy.address,form_template=classify_product_template(product),
          monthly_premium=product.monthly_premium,total_payment=product.monthly_premium,cover_amount=product.cover_amount,
          waiting_period=f'{product.waiting_period_months} months',agent_name=current_user.name)
        ok,errors=assert_application_rules(a)
        if not ok:error='; '.join(errors)
        else:
            db.session.add(a);db.session.flush()
            db.session.add(ApplicationJourney(application_id=a.id,campaign_id=recipient.campaign_id))
            db.session.add(AuditLog(user_id=current_user.id,action='WhatsApp application prepared',entity_type='ClientApplication',entity_id=str(a.id),details='Created from campaign recipient; complete employee FIC screening before sending.'))
            db.session.commit()
            return redirect(url_for('applications.view_application',app_id=a.id))
    return render_template('online/prepare.html',policy=policy,products=products,error=error)


def unlocked(token):
    from app.routes.signing import _unlocked_key
    a=ClientApplication.query.filter_by(sign_token=token).first_or_404()
    if not a.whatsapp_journey:abort(404)
    if a.sign_token_revoked or a.sign_token_used_at:abort(410)
    if not session.get(_unlocked_key(a.id)):abort(403)
    return a


@online_bp.route('/<token>',methods=['GET','POST'])
def form(token):
    a=unlocked(token)
    error=None
    nonce_key=f'questionnaire_nonce_{a.id}'
    session.setdefault(nonce_key,secrets.token_urlsafe(24))
    if request.method=='POST':
        if not secrets.compare_digest(session[nonce_key],request.form.get('nonce','')):abort(400)
        try:
            from app.routes.signing import _save_upload, _fica_status, _save_signature_file, finish_application, REQUIRED_SIGNATURE_DOCS
            action=request.form.get('action')
            if action=='replace' and a.whatsapp_journey.signed_bundle_at:
                changed=False
                for kind in ['id_copy','proof_of_address']:
                    upload=request.files.get(kind)
                    if upload and upload.filename:
                        _save_upload(a,kind,upload);changed=True
                if not changed:raise ValueError('Choose a replacement document to upload.')
                a.status='QA Pending'
                db.session.commit()
                flash('Replacement documents saved for staff review. Your signed forms remain unchanged.','success')
                return redirect(url_for('online_application.form',token=token))
            if action=='save':
                if a.whatsapp_journey.signed_bundle_at:abort(409)
                save_questionnaire(a,request.form)
                db.session.commit()
                session.pop(f'questionnaire_review_{a.id}',None)
                return redirect(url_for('online_application.form',token=token))
            if action=='final_submit':
                if not a.whatsapp_journey.ready:raise ValueError('Complete the questions first.')
                completed=signed_documents(a)
                missing=[label for key,label in REQUIRED_SIGNATURE_DOCS if key not in completed]
                if missing:raise ValueError('Open and sign these documents first: '+', '.join(missing))
                a.whatsapp_journey.signed_bundle_at=datetime.utcnow()
                db.session.add(AuditLog(action='WhatsApp documents completed',entity_type='ClientApplication',entity_id=str(a.id),details='Client signed each highlighted signature space inside every required document and submitted the completed application.'))
                db.session.flush()
                return finish_application(a,token)
            raise ValueError('Choose a valid action.')
        except HTTPException:
            db.session.rollback()
            raise
        except ValueError as exc:
            db.session.rollback();error=str(exc)
        except Exception:
            db.session.rollback()
            current_app.logger.exception('Online application action failed')
            error='We could not save your application. Please try again or contact staff.'
    from app.routes.signing import REQUIRED_SIGNATURE_DOCS, _fica_status
    if a.whatsapp_journey.signed_bundle_at:
        return render_template('online/replacements.html',app=a,nonce=session[nonce_key],error=error)
    from app.services.member_benefits import member_benefit, member_limits
    from app.services.compliance_service import age_from_dob, dob_from_sa_id, format_dob
    limits=member_limits(a.product)
    member_values={}
    stored_groups = ([('productdep',a.product_dependents_json)] if limits['plan_type']=='member_product'
                     else [('child',a.dependents_json),('extended',a.extended_family_json)])
    for kind,raw in stored_groups:
        for i,row in enumerate(json.loads(raw or '[]'),1):
            for key,value in row.items():member_values[f'{kind}_{i}_{key}']=value
    if a.spouse_first_names or a.spouse_surname:
        member_values.update(spouse_1_full_name=' '.join(filter(None,[a.spouse_first_names,a.spouse_surname])),
          spouse_1_relationship='Spouse',spouse_1_id_or_dob=a.spouse_id_number or a.spouse_date_of_birth)
    benefit_groups = ([('productdep',limits['productdep'])] if limits['plan_type']=='member_product'
                      else [('spouse',limits['spouse']),('child',limits['child']),('extended',limits['extended'])])
    for kind,count in benefit_groups:
        for i in range(1,count+1):
            benefit=member_benefit(a.product,kind,member_values.get(f'{kind}_{i}_id_or_dob',''))
            member_values.setdefault(f'{kind}_{i}_cover',benefit['cover'])
            member_values.setdefault(f'{kind}_{i}_waiting_period',benefit['waiting_period'])
            member_values.setdefault(f'{kind}_{i}_date_of_birth',benefit['date_of_birth'])
            member_values.setdefault(f'{kind}_{i}_age','' if benefit['age'] is None else benefit['age'])
    selected_members=[]
    try:
        selected_members=json.loads(a.product_dependents_json or '[]')
    except (TypeError, ValueError, json.JSONDecodeError):
        selected_members=[]
    principal_dob=format_dob(a.date_of_birth or dob_from_sa_id(a.id_number))
    principal_age=age_from_dob(principal_dob)
    rules=a.product.rules
    member_age_rules={
      'spouse':(getattr(rules,'spouse_min_age',None) if rules else None,getattr(rules,'spouse_max_age',None) if rules else None,18,70),
      'child':(getattr(rules,'child_min_age',None) if rules else None,getattr(rules,'child_max_age',None) if rules else None,0,21),
      'extended':(getattr(rules,'extended_min_age',None) if rules else None,getattr(rules,'extended_max_age',None) if rules else None,0,100),
      'productdep':(getattr(rules,'extra_member_min_age',None) if rules else None,getattr(rules,'extra_member_max_age',None) if rules else None,0,70),
    }
    member_age_rules={key:(values[2] if values[0] is None else values[0],values[3] if values[1] is None else values[1]) for key,values in member_age_rules.items()}
    return render_template('online/form.html',member_values=member_values,member_limits=limits,app=a,token=token,fields=FIELDS,banks=BANKS,branch_codes=STANDARD_BRANCH_CODES,
      cdd_fields=[f for f in CDD_FIELDS if f[0] not in {'telephone','residential_address','postal_address','email','birth_date'}],
      cdd=answers_for(a),marketing=consent_value(a),docs=REQUIRED_SIGNATURE_DOCS,signed_docs=signed_documents(a),
      received=_fica_status(a)[1],nonce=session[nonce_key],error=error,selected_members=selected_members,
      principal_dob=principal_dob,principal_age=principal_age,member_age_rules=member_age_rules,
      google_maps_api_key=current_app.config.get('GOOGLE_MAPS_API_KEY') or __import__('os').getenv('GOOGLE_MAPS_API_KEY',''))


@online_bp.route('/<token>/document/<kind>')
def document(token,kind):
    a=unlocked(token)
    from app.routes.signing import REQUIRED_SIGNATURE_DOCS, _signable_pdf
    if not a.whatsapp_journey.ready or kind not in dict(REQUIRED_SIGNATURE_DOCS):abort(400)
    path=_signable_pdf(a,kind)
    db.session.commit()
    key=f'questionnaire_review_{a.id}'
    session[key]=list(set(session.get(key,[]))|{kind})
    response=send_file(path,as_attachment=False)
    response.headers['Cache-Control']='no-store'
    response.headers['Referrer-Policy']='no-referrer'
    return response

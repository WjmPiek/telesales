import json
import secrets
from datetime import datetime
from flask import Blueprint, request, render_template, redirect, url_for, session, abort, flash, send_file
from flask_login import login_required, current_user
from werkzeug.exceptions import HTTPException
from app import db
from app.models import ClientApplication, ApplicationJourney, CampaignRecipient, PolicyProduct, DocumentSignature, AuditLog
from app.security import permission_required
from app.services.branch_access import ensure_branch_access
from app.services.online_application import FIELDS, BANKS, save_questionnaire
from app.services.cdd_service import FIELDS as CDD_FIELDS, answers_for
from app.services.marketing_consent import consent_value
from app.services.signature_fields import application_fields

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
                for kind in ['id_copy','proof_of_address']:
                    upload=request.files.get(kind)
                    if upload and upload.filename:_save_upload(a,kind,upload)
                db.session.commit()
                session.pop(f'questionnaire_review_{a.id}',None)
                return redirect(url_for('online_application.form',token=token))
            if action=='sign':
                a=ClientApplication.query.filter_by(id=a.id).with_for_update().populate_existing().one()
                if a.sign_token_used_at:abort(409)
                if a.whatsapp_journey.signed_bundle_at:raise ValueError('These documents have already been signed.')
                if not a.whatsapp_journey.ready:raise ValueError('Complete the questions first.')
                if request.form.get('consent_bundle')!='yes':raise ValueError('Confirm that you agree to apply your signature to all listed documents.')
                reviewed=set(session.get(f'questionnaire_review_{a.id}',[]))
                if reviewed != {key for key,label in REQUIRED_SIGNATURE_DOCS}:raise ValueError('Open and review every document before signing.')
                if _fica_status(a)[2]:raise ValueError('Upload your ID and proof of address before submitting.')
                from app.services.compliance_service import assert_application_rules
                ok,errors=assert_application_rules(a)
                if not ok:raise ValueError('; '.join(errors))
                full_name=f'{a.first_names} {a.surname}'.strip()
                path=_save_signature_file(a,'document_bundle',request.form.get('signature_data',''))
                keys=[f['key'] for f in application_fields(a)]+[key for key,label in REQUIRED_SIGNATURE_DOCS if key!='application']
                for key in keys:
                    db.session.add(DocumentSignature(application_id=a.id,document_type=key,typed_name=full_name,
                      signature_image_path=path,ip_address=request.remote_addr,user_agent=request.headers.get('User-Agent')))
                a.whatsapp_journey.signed_bundle_at=datetime.utcnow()
                db.session.add(AuditLog(action='Document bundle signed',entity_type='ClientApplication',entity_id=str(a.id),details='Client reviewed the application, POPIA, disclosure, welcome pack and CDD, and explicitly authorised one signature for all client signature spaces.'))
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
            from flask import current_app
            current_app.logger.exception('Online application action failed')
            error='We could not save your application. Please try again or contact staff.'
    from app.routes.signing import REQUIRED_SIGNATURE_DOCS, _fica_status
    if a.whatsapp_journey.signed_bundle_at:
        return render_template('online/replacements.html',app=a,nonce=session[nonce_key],error=error)
    member_values={}
    for kind,raw in [('child',a.dependents_json),('extended',a.extended_family_json)]:
        for i,row in enumerate(json.loads(raw or '[]'),1):
            for key,value in row.items():member_values[f'{kind}_{i}_{key}']=value
    if a.spouse_first_names or a.spouse_surname:
        member_values.update(spouse_1_full_name=' '.join(filter(None,[a.spouse_first_names,a.spouse_surname])),
          spouse_1_relationship='Spouse',spouse_1_id_or_dob=a.spouse_id_number or a.spouse_date_of_birth)
    return render_template('online/form.html',member_values=member_values,app=a,token=token,fields=FIELDS,banks=BANKS,
      cdd_fields=[f for f in CDD_FIELDS if f[0] not in {'telephone','residential_address','postal_address','email','birth_date'}],
      cdd=answers_for(a),marketing=consent_value(a),docs=REQUIRED_SIGNATURE_DOCS,
      received=_fica_status(a)[1],nonce=session[nonce_key],error=error)


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

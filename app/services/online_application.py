"""Questionnaire mapping for campaign-origin applications only."""
import json
from datetime import datetime
from app.services.cdd_service import FIELDS as CDD_FIELDS, save_answers
from app.services.compliance_service import assert_application_rules, dob_from_sa_id, format_dob
from app.services.delivery_preferences import valid_email
from app.services.marketing_consent import apply_consent
from app.services.member_benefits import enrich_rows, member_limits

FIELDS = [
 ('Your details', [
  ('title','Title','Mr|Mrs|Ms|Miss|Dr',False),
  ('first_names','First names','',True),('surname','Surname','',True),
  ('cell_number','Mobile number','',True),('email','Email for documents and policy confirmation','email',True),
  ('residential_address','Residential address','',True),('residential_postal_code','Street code','',True),
  ('postal_address','Postal address (optional)','',False),('postal_code','Postal address code (optional)','',False)]),
 ('Nominated beneficiary', [
  ('beneficiary_full_names','Beneficiary full names and surname','',True),
  ('beneficiary_relationship','Relationship','Spouse|Partner|Parent|Child|Sibling|Other',True),
  ('beneficiary_id_number','Beneficiary ID number (if available)','',False),
  ('beneficiary_date_of_birth','Beneficiary date of birth','date',True)]),
 ('Payment', [
  ('payment_method','How will you pay?','Cash|Debit Order|Stop Order',True),
  ('bank_name','Bank (debit order only)','bank',False),('branch_code','Branch code','',False),
  ('account_number','Account number','',False),('account_type','Account type','Savings|Cheque|Transmission',False),
  ('account_holder','Account holder full names','',False),('debit_day','Debit day','1|5|15|20|25|30',False),
  ('first_deduction_date','First deduction date','date',False)])]
BANKS=['Absa','African Bank','Capitec','Discovery Bank','FNB','Investec','Nedbank','Standard Bank','TymeBank']
STANDARD_BRANCH_CODES={
 'Absa':'632005','African Bank':'430000','Capitec':'470010','Discovery Bank':'679000',
 'FNB':'250655','Investec':'580105','Nedbank':'198765','Standard Bank':'051001','TymeBank':'678910'
}


def save_questionnaire(a, form):
    for group, fields in FIELDS:
        for key,label,options,required in fields:
            value=form.get(key,'').strip()
            if key == 'beneficiary_date_of_birth' and not value:
                derived = format_dob(dob_from_sa_id(form.get('beneficiary_id_number', '')))
                if derived:
                    dd, mm, yyyy = derived.split('/')
                    value = f'{yyyy}-{mm}-{dd}'
            if required and not value:
                raise ValueError('Please complete '+label+'.')
            if len(value)>min(300, getattr(a.__table__.columns[key].type,'length',None) or 300):
                raise ValueError(label+' is too long.')
            if '|' in options and value and value not in options.split('|'):
                raise ValueError('Select a valid '+label+'.')
            if options=='date' and value:
                try: datetime.strptime(value,'%Y-%m-%d')
                except ValueError: raise ValueError('Enter a valid date for '+label+'.')
            setattr(a,key,value)
    if a.bank_name in STANDARD_BRANCH_CODES:
        a.branch_code=STANDARD_BRANCH_CODES[a.bank_name]
    if not valid_email(a.email):
        raise ValueError('Enter an email address for the signed documents and policy confirmation.')
    a.document_email=a.email
    a.address=a.residential_address
    a.date_of_birth=dob_from_sa_id(a.id_number)
    a.spouse_first_names=a.spouse_surname=a.spouse_id_number=a.spouse_date_of_birth=''
    limits=member_limits(a.product)
    all_members=[]
    groups = ([('productdep',limits['productdep'],'product_dependents_json')] if limits['plan_type']=='member_product'
              else [('child',limits['child'],'dependents_json'),('extended',limits['extended'],'extended_family_json'),('spouse',limits['spouse'],None)])
    if limits['plan_type']=='member_product':
        a.dependents_json=a.extended_family_json='[]'
    for kind,limit,attribute in groups:
        rows=[]
        for i in range(1,limit+1):
            row={key:form.get(f'{kind}_{i}_{key}','').strip()[:150] for key in ('full_name','relationship','id_or_dob')}
            if not any(row.values()):continue
            if not all(row.values()):raise ValueError('Complete name, relationship and ID or date of birth for each added member.')
            rows.append(row)
        benefit_rows=enrich_rows(a.product,kind,rows)
        if attribute:setattr(a,attribute,json.dumps(benefit_rows))
        elif rows:
            row=rows[0];names=row['full_name'].rsplit(' ',1)
            a.spouse_first_names=names[0];a.spouse_surname=names[1] if len(names)>1 else ''
            identifier=row['id_or_dob']
            if len(identifier)==13:a.spouse_id_number=identifier
            a.spouse_date_of_birth=format_dob(dob_from_sa_id(identifier) or identifier)
        all_members.extend(benefit_rows)
    a.product_dependents_json=json.dumps(all_members)
    if a.payment_method=='Debit Order':
        if not a.debit_day or not a.first_deduction_date:
            raise ValueError('Select the debit day and first deduction date.')
    else:
        for key in ['bank_name','branch_code','account_number','account_type','account_holder','debit_day','first_deduction_date']:setattr(a,key,'')
    ok,errors=assert_application_rules(a)
    if not ok:raise ValueError('; '.join(errors))
    choice=form.get('marketing_choice')
    if choice not in {'yes','no'}:raise ValueError('Select Yes or No for marketing messages.')
    cdd={key:form.get('cdd_'+key,'').strip() for key,label,options in CDD_FIELDS}
    cdd.update(telephone=a.cell_number,residential_address=a.residential_address,postal_address=a.postal_address,email=a.email)
    cdd['birth_date']=datetime.strptime(format_dob(a.date_of_birth),'%d/%m/%Y').strftime('%Y-%m-%d')
    save_answers(a,cdd)
    apply_consent(a,choice=='yes',None,None)
    a.whatsapp_journey.ready=True


def notify_activation(a):
    """Release the final policy pack only after staff activate the policy."""
    import logging
    import os
    import shutil
    from app import db
    from app.models import ApplicationJourney, DocumentSignature
    from app.services.cdd_service import generate_cdd_pdf
    from app.services.client_storage import application_folder
    from app.services.email_service import business_bank_confirmation_attachment, client_email_content, send_email
    from app.services.company_documents import active_company_documents, materialise_company_documents
    from app.services.cover_eligibility import coverage_report
    from app.services.pdf_service import (generate_application_pdf, generate_disclosure_pdf,
                                          generate_popia_pdf, generate_welcome_pack)
    journey=ApplicationJourney.query.filter_by(application_id=a.id).with_for_update().populate_existing().one()
    eligibility = coverage_report(a)
    valid_rules, rule_errors = assert_application_rules(a)
    if eligibility['missing_ids'] or eligibility['blocked'] or not valid_rules:
        logging.getLogger(__name__).error('Activation notice blocked by member ID or cover validation for application %s', a.id)
        return False
    if not journey.activated_at or journey.notice_status in {'Sent','Sending'}:
        return journey.notice_status=='Sent'
    journey.notice_status='Sending'
    db.session.commit()
    bank_letter = None
    company_folder = None
    sent = False
    try:
        signatures = {row.document_type: row for row in DocumentSignature.query.filter_by(application_id=a.id).all()}
        folder = application_folder(a)
        paths = {
            'application': os.path.join(folder, f'signed_application_{a.id}.pdf'),
            'welcome': os.path.join(folder, f'welcome_pack_{a.id}.pdf'),
            'popia': os.path.join(folder, f'popia_consent_{a.id}.pdf'),
            'disclosure': os.path.join(folder, f'policy_disclosure_{a.id}.pdf'),
            'cdd': os.path.join(folder, f'annexure_j1_{a.id}.pdf'),
        }
        principal = signatures.get('application:principal')
        generate_application_pdf(a, paths['application'], signature_path_override=principal.signature_image_path if principal else None)
        for key, generator in [('welcome', generate_welcome_pack), ('popia', generate_popia_pdf),
                               ('disclosure', generate_disclosure_pdf)]:
            row = signatures.get(key)
            generator(a, paths[key], signature_path_override=row.signature_image_path if row else None)
        generate_cdd_pdf(a, paths['cdd'])
        a.signed_pdf_path = paths['application']; a.welcome_pack_path = paths['welcome']
        a.popia_pdf_path = paths['popia']; a.disclosure_pdf_path = paths['disclosure']
        db.session.commit()
        attachments = list(paths.values())
        cash = str(a.payment_method or '').strip().lower() == 'cash'
        company_rows = active_company_documents(a, include_bank=cash)
        company_has_bank = any(row.category == 'bank_confirmation' for row in company_rows)
        if cash and not company_has_bank:
            bank_letter = business_bank_confirmation_attachment()
            if not bank_letter:
                raise ValueError('The current business bank confirmation letter is not configured.')
            attachments.append(bank_letter)
        company_paths, company_folder = materialise_company_documents(a, include_bank=cash)
        attachments.extend(company_paths)
        subject,body=client_email_content('activation',a)
        sent=send_email(a.document_email or a.email,subject,body,attachments,application_id=a.id)
    except Exception as exc:
        logging.getLogger(__name__).warning('Final activation pack delivery failed (%s)', type(exc).__name__)
        sent = False
    finally:
        if bank_letter:
            shutil.rmtree(os.path.dirname(bank_letter), ignore_errors=True)
        if company_folder:
            shutil.rmtree(company_folder, ignore_errors=True)
    journey.notice_status='Sent' if sent else 'Failed'
    if sent:journey.notice_sent_at=datetime.utcnow()
    db.session.commit()
    return sent

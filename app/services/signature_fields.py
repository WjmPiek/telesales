"""Signature locations. Call applications sign separately; WhatsApp bundles require explicit consent to one signature."""
from app.models import DocumentSignature
from reportlab.lib.pagesizes import A4
from app.services.application_layout import SIGNATURE_RECTS


def application_template(a):
    if a.form_template in {*SIGNATURE_RECTS, 'gold_family_fillable'}:
        return a.form_template
    text = f"{a.product.product_name if a.product else ''} {a.product.plan_name if a.product else ''}".lower()
    return 'member_product' if ('member +' in text or ('product' in text and ('+' in text or 'member' in text))) else 'single_family'


def application_fields(a):
    if application_template(a) == 'gold_family_fillable':
        fields = [
            dict(key='application:principal', label='Policyholder / principal member signature', page=2, rect=[155, 73, 315, 97]),
            dict(key='application:terms', label='Policy terms and conditions signature', page=4, rect=[145, 410, 365, 485]),
        ]
        if 'debit' in (a.payment_method or '').lower() or any((a.account_number, a.account_holder, a.bank_name)):
            fields.append(dict(key='application:account', label='Debit-order account holder signature', page=2, rect=[155, 28, 315, 52]))
        return fields
    member = application_template(a) == 'member_product'
    def box(key, label, page, left, top, right, bottom):
        return dict(key='application:'+key,label=label,page=page,rect=[left,A4[1]-bottom,right,A4[1]-top])
    fields = [dict(key='application:principal',label='Principal member signature',page=1,rect=SIGNATURE_RECTS[application_template(a)])]
    if not member:
        fields.append(box('premium','Premium payment agreement',1,487,106,574,132))
    method = (a.payment_method or '').lower()
    if 'debit' in method or any((a.account_number, a.account_holder, a.bank_name)):
        fields.append(box('bank','Debit order authorisation',1,340,611.6 if member else 615.8,580,623.8 if member else 628))
        rect = list(SIGNATURE_RECTS[application_template(a)]);rect[0]=158;rect[2]=295
        fields.append(dict(key='application:account',label='Account holder signature',page=1,rect=rect))
    if 'persal' in method or 'salary' in method:
        fields.append(box('salary','Salary deduction authorisation',1,440 if member else 383,665.5 if member else 656,580,677.8 if member else 668.3))
    fields.append(box('terms','Main member signature - terms and conditions',2,30,792,280,826))
    if 'debit' in method or any((a.account_number,a.account_holder,a.bank_name)):
        fields.append(box('terms_account','Account holder signature - debit order',2,320,792,570,826))
    return sorted(fields,key=lambda f:(f['page'],-f['rect'][3]))


def signature_rows(a):
    return {r.document_type:r for r in DocumentSignature.query.filter_by(application_id=a.id).all()}


def signed_documents(a):
    rows=signature_rows(a)
    done={k for k in ['popia','disclosure','welcome','cdd'] if k in rows}
    if all(f['key'] in rows for f in application_fields(a)):
        done.add('application')
    return done

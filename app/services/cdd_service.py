"""Client answers and the supplied Annexure J.1; staff assessments stay unsigned."""
import base64
import io
import json
from pathlib import Path
from datetime import datetime
from flask import current_app
from pypdf import PdfReader, PdfWriter
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader, simpleSplit
from app import db
from app.models import ApplicationCDD, DocumentSignature
from app.services.client_storage import durable_pdf

FIELDS = [
    ('nationality', 'Nationality', 'South African|Foreign National'),
    ('telephone', 'Telephone number', ''),
    ('residential_address', 'Residential address', ''),
    ('postal_address', 'Postal address', ''),
    ('email', 'Email address', ''),
    ('birth_date', 'Date of birth (YYYY-MM-DD)', ''),
    ('birth_place', 'Place of birth', ''),
    ('employer', 'Place of employment (or Not employed)', ''),
    ('hair', 'Hair colour', 'Blond|Red|Brown|Black|Other'),
    ('eyes', 'Eye colour', 'Blue|Amber|Brown|Green|Other'),
    ('nature', 'Nature of the relationship with Martin\'s Funerals', ''),
    ('purpose', 'Purpose of the relationship', ''),
    ('funds', 'Main source of funds for premiums', 'Salary|Business Income|Dividend|Interest|Gift|Savings|Other'),
    ('funds_details', 'Explain the source of funds (include details if Other)', ''),
    ('pep', 'Do you hold a prominent public or influential position (DPEP, FPEP or PIP)?', 'Yes|No'),
    ('associate', 'Are you a close associate of a person in such a position?', 'Yes|No'),
    ('family', 'Are you a family member of a person in such a position?', 'Yes|No'),
    ('pep_details', 'If any answer above is Yes, give details; otherwise enter Not applicable', ''),
]


def answers_for(a):
    row=ApplicationCDD.query.filter_by(application_id=a.id).first()
    if row:
        return json.loads(row.answers_json)
    from app.services.compliance_service import format_dob
    try:
        birth_date=datetime.strptime(format_dob(a.date_of_birth or a.id_number),'%d/%m/%Y').date().isoformat()
    except (ValueError,TypeError):
        birth_date=''
    return dict(telephone=a.cell_number or a.home_tel or '', residential_address=a.residential_address or a.address or '',
                postal_address=a.postal_address or '', email=a.email or '', employer=a.employer or '', birth_date=birth_date)


def save_answers(a, form):
    values={}
    for key,label,options in FIELDS:
        value=(form.get(key) or '').strip()
        if not value or len(value)>300:
            raise ValueError(f'Complete {label} (maximum 300 characters).')
        if options and value not in options.split('|'):
            raise ValueError(f'Select a valid answer for {label}.')
        if key=='birth_date':
            try:datetime.strptime(value,'%Y-%m-%d')
            except ValueError:raise ValueError('Enter date of birth as YYYY-MM-DD.')
        values[key]=value
    row=ApplicationCDD.query.filter_by(application_id=a.id).first()
    if not row:
        row=ApplicationCDD(application_id=a.id);db.session.add(row)
    encoded=json.dumps(values,sort_keys=True)
    if row.answers_json != encoded:
        DocumentSignature.query.filter_by(application_id=a.id,document_type='cdd').delete(synchronize_session=False)
    row.answers_json=encoded;row.completed_at=datetime.utcnow()
    db.session.flush()


def completed(a):
    return bool(ApplicationCDD.query.filter_by(application_id=a.id).first())


@durable_pdf
def generate_cdd_pdf(a, out_path):
    source=Path(current_app.root_path)/'assets'/'cdd_natural_person.pdf'
    reader=PdfReader(io.BytesIO(source.read_bytes()))
    writer=PdfWriter();writer.append(reader)
    data=answers_for(a)
    width,height=A4
    def overlay(page_no, draw):
        stream=io.BytesIO();c=canvas.Canvas(stream,pagesize=A4);draw(c);c.save();stream.seek(0)
        writer.pages[page_no].merge_page(PdfReader(stream).pages[0])
    # Exact original form artwork, with values confined to its blank value cells.
    def line(c,text,x,top,w,size=8):
        text=str(text or '')
        c.setFont('Helvetica',size)
        while c.stringWidth(text,'Helvetica',size)>w and size>5:
            size-=.25
        if c.stringWidth(text,'Helvetica',size)>w:
            while text and c.stringWidth(text+'...','Helvetica',size)>w:text=text[:-1]
            text+='...'  # The complete client answer remains on the declaration pages.
        c.setFont('Helvetica',size);c.drawString(x,height-top,text)
    def details(c):
        for value,top in [(f'{a.first_names or ""} {a.surname or ""}',478),(a.id_number,498),(data.get('telephone'),518),
             (data.get('residential_address'),536),(data.get('postal_address'),555),(data.get('email'),575),
             (data.get('birth_date'),595),(data.get('birth_place'),615),(data.get('employer'),635)]:
            line(c,value,174,top,383)
        for key,top,xs in [('nationality',455.5,{'South African':235,'Foreign National':319}),
                           ('hair',654,{'Blond':202,'Red':236,'Brown':280,'Black':320,'Other':361}),
                           ('eyes',674,{'Blue':197,'Amber':239,'Brown':280,'Green':321,'Other':361})]:
            if data.get(key) in xs:line(c,'X',xs[data[key]],top,9,8)
    overlay(0,details)
    def transactions(c):
        line(c,data.get('nature'),176,47,380)
        line(c,data.get('purpose'),176,87,380)
        # Free text is placed on the extra line below the original checkbox row.
        line(c,data.get('funds'),176,144,370)
        boxes={'Salary':205,'Business Income':292,'Dividend':342,'Interest':390,'Gift':422,'Savings':472,'Other':510}
        if data.get('funds') in boxes:line(c,'X',boxes[data['funds']],129,8,8)
    overlay(1,transactions)
    # Client declaration is separate from the original organisation signature.
    stream=io.BytesIO();c=canvas.Canvas(stream,pagesize=A4)
    c.setTitle('Annexure J.1 - Client answers and declaration')
    c.setFillColorRGB(.42,.29,.63);c.setFont('Helvetica-Bold',15)
    c.drawString(36,805,'Annexure J.1 - Client answers and declaration')
    c.setFillColorRGB(0,0,0);y=782
    for text in [f'Application: {a.application_ref}',f'Client: {a.first_names or ""} {a.surname or ""}',f'ID / Passport: {a.id_number or ""}',
                 'Risk ratings, institutional verification and the organisation signature are for staff completion.']:
        for part in simpleSplit(text,'Helvetica',9,520):c.setFont('Helvetica',9);c.drawString(36,y,part);y-=13
    y-=8
    for key,label,options in FIELDS:
        for part in simpleSplit(label+': '+data.get(key,'Not completed'),'Helvetica',8.5,520):
            if y<130:
                c.showPage();y=805
            c.setFont('Helvetica',8.5);c.drawString(36,y,part);y-=12
        y-=4
    if y<180:c.showPage();y=805
    for text in simpleSplit('I confirm that the client information and answers above are accurate to the best of my knowledge. I have reviewed Annexure J.1 and provided the source of funds information recorded above.','Helvetica',9,520):
        c.setFont('Helvetica',9);c.drawString(36,y,text);y-=13
    y-=65;rect=[170,y,420,y+48]
    c.setFont('Helvetica-Bold',9);c.drawString(36,y+18,'Client signature:');c.line(170,y,420,y)
    sig=DocumentSignature.query.filter_by(application_id=a.id,document_type='cdd').first()
    if sig:
        c.drawImage(ImageReader(sig.signature_image_path),rect[0],rect[1],width=250,height=48,preserveAspectRatio=True,mask='auto')
        c.setFont('Helvetica',8);c.drawString(36,y-20,'Signed: '+str(sig.signed_at))
    c.save();stream.seek(0);extra=PdfReader(stream);page_no=2+len(extra.pages);writer.append(extra)
    writer.add_metadata({'/Subject':'martins-signature:'+json.dumps({'page':page_no,'rect':rect})})
    with open(out_path,'wb') as f:writer.write(f)
    return out_path

"""Measured A4 field bounds for the supplied CorelDRAW application artwork.

Coordinates are PDF points from the top left, never scaled US Letter positions.
Only populated value areas are cleared; printed labels and outer borders remain.
"""
import json
import re
from datetime import datetime
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase.pdfmetrics import stringWidth

SIGNATURE_RECTS = {
    "single_family": [300, A4[1] - 790, 437, A4[1] - 766],
    "member_product": [300, A4[1] - 813, 437, A4[1] - 789],
}


def field(c, value, left, top, right, bottom=None, size=7):
    """Fit a complete value within its own field, without clipping to char counts."""
    if value is None or str(value).strip() == "":
        return
    text = " ".join(str(value).split())
    bottom = bottom if bottom is not None else top + 13.4
    width = right - left - 4
    natural = stringWidth(text, "Helvetica", size)
    c.saveState()
    c.setFillColorRGB(1, 1, 1)
    c.rect(left + .6, A4[1] - bottom + .6, right - left - 1.2, bottom - top - 1.2, fill=1, stroke=0)
    c.setFillColorRGB(0, 0, 0)
    t = c.beginText(left + 2, A4[1] - (top + bottom) / 2 - size * .35)
    t.setFont("Helvetica", size)
    if natural > width:
        t.setHorizScale(100 * width / natural)
    t.textOut(text)
    c.drawText(t)
    c.restoreState()


def date_field(c, value, left, top, right):
    if not value:
        return
    if hasattr(value, 'strftime'):
        digits = value.strftime('%d%m%Y')
    else:
        raw = str(value).strip()
        if re.match(r'^\d{4}-\d{2}-\d{2}', raw):
            digits = raw[8:10] + raw[5:7] + raw[:4]
        else:
            digits = re.sub(r'\D', '', raw)
    if len(digits) != 8:
        field(c, value, left, top, right)
        return
    step = (right - left) / 8
    for i, char in enumerate(digits):
        field(c, char, left + i * step, top, left + (i + 1) * step)


def money(value):
    return f'{float(value):.2f}' if value is not None else None


def rows(value):
    try:
        return json.loads(value or '[]')
    except (ValueError, TypeError):
        return []


def draw_application_overlay(a, path, template, signature=None):
    c = canvas.Canvas(path, pagesize=A4)
    member = template == 'member_product'
    def f(attr, x, y, right, bottom=None):
        field(c, getattr(a, attr, None), x, y, right, bottom)
    def d(attr, x, y, right=581.1):
        date_field(c, getattr(a, attr, None), x, y, right)
    def m(attr, x, y, right):
        field(c, money(getattr(a, attr, None)), x, y, right)

    # Agent and policy reference are in the header's white boxes.
    header_top = 139.7 if member else 142.87
    f('agent_name', 61.4, header_top, 192.7, header_top + 19.2)
    f('agent_code', 237.5, header_top, 368.5, header_top + 19.2)
    field(c, a.policy_number or a.application_ref, 424.4, header_top, 576.8, header_top + 19.2)
    if not member:
        for attr, top in [('monthly_premium', 60), ('extended_premium', 75), ('total_payment', 90.5)]:
            value = getattr(a, attr, None)
            if attr == 'total_payment' and value is None:
                value = (a.monthly_premium or 0) + (a.extended_premium or 0)
            field(c, money(value), 497, top, 574, top + 10)

    top, step = (178.39, 13.453) if member else (183.79, 13.365)
    f('surname', 70.87, top, 297.64)
    f('first_names', 354.33, top, 581.1)
    f('title', 42.52, top + step, 198.42)
    f('id_number', 240.94, top + step, 425.2)
    d('date_of_birth', 467.72, top + step)
    if not member:
        f('spouse_surname', 85.04, top + step * 2, 297.64)
        f('spouse_first_names', 354.33, top + step * 2, 581.1)
        f('spouse_title', 42.52, top + step * 3, 198.42)
        f('spouse_id_number', 240.94, top + step * 3, 425.2)
        d('spouse_date_of_birth', 467.72, top + step * 3)
    address_top = top + step * (2 if member else 4)
    field(c, a.residential_address or a.address, 85.04, address_top, 297.64)
    f('postal_address', 354.33, address_top, 581.1)
    f('residential_postal_code', 240.94, address_top + step, 297.64)
    f('postal_code', 524.41, address_top + step, 581.1)
    for attr, x, right in [('home_tel',99.21,240.94), ('work_tel',269.29,411.02), ('cell_number',439.37,581.1)]:
        f(attr, x, address_top + step * 2, right)
    f('email',42.52,address_top + step * 3,297.64 if member else 581.1)
    if member:
        m('monthly_premium',396.85,245.65,439.37)
        m('cover_amount',510.24,245.65,581.1)
        choice = str(a.plan_choice or '').strip().lower().removeprefix('plan ').strip()
        for label, left in [('a',141.73),('b',184.25),('c',226.77)]:
            if choice == label:
                field(c,'X',left,259.11,left + 14.17)
        for i, row in enumerate(rows(a.product_dependents_json)[:13]):
            y = 300.2 + i * 13.445
            field(c,row.get('full_name'),42.52,y,297.64)
            field(c,row.get('relationship'),297.64,y,396.85)
            field(c,row.get('id_or_dob'),396.85,y,581.1)
    else:
        d('inception_date',70.87,290.71,184.25)
        m('monthly_premium',481.89,290.71,581.1)
        for i, row in enumerate(rows(a.dependents_json)[:6]):
            y = 331.69 + i * 13.408
            field(c,row.get('full_name'),28.35,y,297.64)
            field(c,row.get('relationship'),297.64,y,396.85)
            field(c,row.get('id_or_dob'),396.85,y,581.1)
        for i, row in enumerate(rows(a.extended_family_json)[:4]):
            y = 439.75 + i * 13.4125
            name = ' / '.join(str(row.get(k) or '') for k in ['full_name','relationship']).strip(' /')
            field(c,name,28.35,y,255.12)
            field(c,row.get('id_or_dob'),255.12,y,439.37)
            field(c,row.get('cover'),453.54,y,510.24)
            field(c,row.get('premium'),524.41,y,581.1)
        total = a.total_payment if a.total_payment is not None else (a.monthly_premium or 0) + (a.extended_premium or 0)
        field(c,money(total),340.16,493.4,467.72)
        m('extended_premium',524.41,493.4,581.1)

    btop = 489.19 if member else 521.01
    f('beneficiary_full_names',113.39,btop,439.37)
    f('beneficiary_relationship',481.89,btop,581.1)
    f('beneficiary_title',42.52,btop+13.41,198.42)
    f('beneficiary_id_number',240.94,btop+13.41,425.2)
    d('beneficiary_date_of_birth',467.72,btop+13.41)
    method = str(a.payment_method or '').lower()
    payment_top = 530.21 if member else 561.62
    choices = [('cash',99.21),('debit',155.91),('salary',240.94)] if member else [('cash',198.42),('debit',255.12),('persal',297.64)]
    for label, left in choices:
        if label in method or (label == 'salary' and 'persal' in method):
            field(c,'X',left,payment_top,left+14.17)
    d('first_deduction_date',467.72,payment_top)
    day_top = 557.16 if member else 575.03
    for label, left in [('1',425.2),('5',453.54),('15',481.89),('20',510.24),('25',538.58),('30',566.93)]:
        if re.sub(r'\D','',str(a.debit_day or '')) == label:
            field(c,'X',left,day_top,left+14.17)
    if member:
        for attr,x,y,right in [
            ('bank_name',56.69,570.63,297.64),('branch_name',354.33,570.63,581.1),
            ('account_number',70.87,584.11,297.64),('account_type',354.33,584.11,581.1),
            ('branch_code',70.87,597.58,297.64),('bank_town',325.98,597.58,581.1),
            ('account_holder',70.87,611.05,297.64),('personal_holder',99.21,678.42,283.46),
            ('persal_no',340.16,678.42,411.02),('department_code',467.72,678.42,581.1)]:
            f(attr,x,y,right)
    else:
        for attr,x,y,right in [
            ('bank_name',56.69,588.44,155.91),('branch_name',212.6,588.44,297.64),
            ('branch_code',354.33,588.44,439.37),('bank_town',467.72,588.44,581.1),
            ('account_number',70.87,601.85,297.64),('account_type',354.33,601.85,581.1),
            ('account_holder',70.87,615.26,297.64),('employer',212.6,628.67,439.37),
            ('persal_no',56.69,642.08,297.64),('paypoint',354.33,642.08,439.37),
            ('personal_holder',70.87,655.49,297.64)]:
            f(attr,x,y,right)
        m('salary',467.72,628.67,581.1)
        m('payroll_premium',481.89,642.08,581.1)
    # Client signature images are placed individually after merging the artwork.
    c.save()

import json
from app.services.client_storage import durable_pdf
import os
import io
import json
from datetime import datetime
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader
from pypdf import PdfReader, PdfWriter

try:
    from app.models import ApplicationSignature
except Exception:
    ApplicationSignature = None


BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
TEMPLATE_DIR = os.path.join(BASE_DIR, "static", "pdf_templates")
LOGO_PATH = os.path.join(BASE_DIR, "static", "img", "martins_logo.png")
TERMS_PDF_PATH = os.path.join(TEMPLATE_DIR, "martins_policy_terms.pdf")
FAMILY_TERMS_PDF_PATH = os.path.join(TEMPLATE_DIR, "family_terms.pdf")
MEMBER_PRODUCT_TERMS_PDF_PATH = os.path.join(TEMPLATE_DIR, "member_product_terms.pdf")
SVG_TEMPLATE_DIR = os.path.join(BASE_DIR, "static", "svg_templates")
COMPANY_NAME = "Martin's Funerals"
FSP_FOOTER = "An Authorised Financial Service Provider (FSP 48189)"
PURPLE = (0.31, 0.16, 0.55)
LIGHT_PURPLE = (0.93, 0.89, 0.97)


def _brand_header(c, title):
    width, height = A4
    # Fixed header alignment:
    # - Text is restricted to the left column.
    # - Logo is restricted to the right column.
    # - Long titles are auto-wrapped/scaled so they never run behind the logo.
    left_x = 32
    logo_w = 205
    logo_h = 85
    logo_x = width - logo_w - 30
    logo_y = height - 95
    max_title_w = logo_x - left_x - 18

    title_text = _safe(title).upper()
    c.setFillColorRGB(*PURPLE)

    def draw_title_lines(lines, font_size):
        c.setFont("Helvetica-Bold", font_size)
        y = height - 42
        for line in lines[:2]:
            c.drawString(left_x, y, line)
            y -= font_size + 3
        return y

    font_size = 22
    if c.stringWidth(title_text, "Helvetica-Bold", font_size) <= max_title_w:
        next_y = draw_title_lines([title_text], font_size)
    else:
        # First try smaller text. If still too long, split into two balanced lines.
        font_size = 16
        words = title_text.split()
        lines = []
        current = ""
        for word in words:
            trial = (current + " " + word).strip()
            if current and c.stringWidth(trial, "Helvetica-Bold", font_size) > max_title_w:
                lines.append(current)
                current = word
            else:
                current = trial
        if current:
            lines.append(current)
        if len(lines) > 2:
            # Keep the header clean by shortening the second line if the title is extremely long.
            lines = [lines[0], " ".join(lines[1:])]
            while c.stringWidth(lines[1] + "...", "Helvetica-Bold", font_size) > max_title_w and len(lines[1]) > 8:
                lines[1] = lines[1][:-1]
            lines[1] = lines[1].rstrip() + "..."
        next_y = draw_title_lines(lines, font_size)

    c.setFont("Helvetica", 10)
    c.setFillColorRGB(0, 0, 0)
    subtitle = "Protection of Personal Information Act, 2013 (POPIA)" if "POPIA" in title_text else COMPANY_NAME
    subtitle_y = min(height - 66, next_y - 4)
    c.drawString(left_x, subtitle_y, subtitle)
    c.setStrokeColorRGB(*PURPLE)
    c.setLineWidth(1.2)
    c.line(left_x, subtitle_y - 12, logo_x - 18, subtitle_y - 12)

    if os.path.exists(LOGO_PATH):
        try:
            c.drawImage(LOGO_PATH, logo_x, logo_y, width=logo_w, height=logo_h, preserveAspectRatio=True, mask="auto")
        except Exception:
            pass
    c.setFillColorRGB(0, 0, 0)


def _footer(c):
    width, _ = A4
    c.setFillColorRGB(*PURPLE)
    c.rect(0, 0, width, 28, fill=True, stroke=False)
    c.setFillColorRGB(1, 1, 1)
    c.setFont("Helvetica", 8.5)
    c.drawCentredString(width / 2, 16, "Thank you for choosing Martin's Funerals.")
    c.drawCentredString(width / 2, 6, FSP_FOOTER)
    c.setFillColorRGB(0, 0, 0)


def _section(c, title, x, y, w=535, h=18):
    c.setFillColorRGB(*PURPLE)
    c.roundRect(x, y - h + 3, w, h, 5, fill=True, stroke=False)
    c.setFillColorRGB(1, 1, 1)
    c.setFont("Helvetica-Bold", 10)
    c.drawString(x + 10, y - 10, title.upper())
    c.setFillColorRGB(0, 0, 0)
    return y - h - 8


def _box(c, x, y, w, h):
    c.setStrokeColorRGB(*PURPLE)
    c.setLineWidth(0.8)
    c.roundRect(x, y - h, w, h, 5, stroke=True, fill=False)
    c.setStrokeColorRGB(0, 0, 0)


def _checkbox_line(c, checked, text, x, y, size=9):
    c.setStrokeColorRGB(*PURPLE)
    c.rect(x, y - 2, 11, 11, stroke=True, fill=False)
    c.setFillColorRGB(*PURPLE)
    if checked:
        c.setFont("Helvetica-Bold", 12)
        c.drawString(x + 1.5, y - 2, "X")
    c.setFillColorRGB(0, 0, 0)
    c.setFont("Helvetica", size)
    c.drawString(x + 20, y, text)


def _latest_signature_record(app_obj):
    if not ApplicationSignature:
        return None
    try:
        return ApplicationSignature.query.filter_by(application_id=app_obj.id).order_by(ApplicationSignature.signed_at.desc()).first()
    except Exception:
        return None


def _signed_date(app_obj):
    sig = _latest_signature_record(app_obj)
    dt = getattr(sig, "signed_at", None) or getattr(app_obj, "signed_at", None) or datetime.now()
    try:
        return dt.strftime("%d/%m/%Y")
    except Exception:
        return datetime.now().strftime("%d/%m/%Y")


def _marketing_consent(app_obj):
    from app.services.marketing_consent import consent_value
    return consent_value(app_obj)


def _wrap_text(text, max_chars=92):
    words = str(text or "").split()
    lines, line = [], ""
    for word in words:
        if len(line) + len(word) + 1 <= max_chars:
            line = (line + " " + word).strip()
        else:
            if line:
                lines.append(line)
            line = word
    if line:
        lines.append(line)
    return lines


def _draw_paragraph(c, text, x, y, max_chars=92, leading=14, size=9):
    c.setFont("Helvetica", size)
    for line in _wrap_text(text, max_chars):
        c.drawString(x, y, line)
        y -= leading
    return y




def _ensure_dir(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)


def _safe(v):
    if v is None:
        return ""
    return str(v)


def _money(v):
    try:
        return f"{float(v):.2f}"
    except Exception:
        return "0.00"


def _date_boxes(value):
    if value is None:
        return ""
    # Date/datetime objects from SQLAlchemy.
    if hasattr(value, "strftime"):
        try:
            return value.strftime("%d/%m/%Y")
        except Exception:
            pass
    raw = _safe(value).strip()
    # ISO date string, e.g. 2026-06-23.
    if len(raw) >= 10 and raw[4:5] == "-" and raw[7:8] == "-":
        try:
            return f"{raw[8:10]}/{raw[5:7]}/{raw[0:4]}"
        except Exception:
            return raw[:10]
    txt = "".join(ch for ch in raw if ch.isdigit())
    if len(txt) == 8:
        # Store and print DOB/date fields consistently as DD/MM/YYYY.
        return f"{txt[:2]}/{txt[2:4]}/{txt[4:8]}"
    return raw[:10]


def _rows(json_text):
    try:
        return json.loads(json_text or "[]")
    except Exception:
        return []


def _draw(c, text, x, y, size=7, max_chars=None, bold=False):
    if text is None:
        return
    text = _safe(text)
    if max_chars:
        text = text[:max_chars]
    c.setFont("Helvetica-Bold" if bold else "Helvetica", size)
    c.drawString(x, y, text)


def _draw_center(c, text, x, y, size=7):
    c.setFont("Helvetica", size)
    c.drawCentredString(x, y, _safe(text))


def _draw_grid(c, text, x, y, step=10, size=6, max_chars=None):
    text = _safe(text)
    if max_chars:
        text = text[:max_chars]
    c.setFont("Helvetica", size)
    for i, ch in enumerate(text):
        c.drawCentredString(x + i * step, y, ch)


def _draw_checkbox(c, selected, x, y):
    if selected:
        c.setFont("Helvetica-Bold", 8)
        c.drawString(x, y, "X")


def _latest_signature(app_obj):
    if not ApplicationSignature:
        return None
    try:
        return ApplicationSignature.query.filter_by(application_id=app_obj.id).order_by(ApplicationSignature.signed_at.desc()).first()
    except Exception:
        return None


def _draw_signature(c, sig_path, x, y, w=95, h=28):
    if sig_path and os.path.exists(sig_path):
        try:
            from PIL import Image, ImageChops
            image=Image.open(sig_path).convert('RGBA')
            white=Image.new('RGBA',image.size,'white');white.alpha_composite(image)
            ink=ImageChops.difference(white.convert('RGB'),Image.new('RGB',image.size,'white')).convert('L').point(lambda v:255 if v>35 else 0)
            bounds=ink.getbbox()
            if not bounds:return
            image=image.crop(bounds)
            scale=min(w/image.width,h/image.height)
            sw,sh=image.width*scale,image.height*scale
            c.drawImage(ImageReader(image), x+(w-sw)/2, y+(h-sh)/2, width=sw, height=sh, mask="auto")
        except Exception:
            pass


def _merge_template(template_path, overlay_pdf_path, out_path):
    reader = PdfReader(template_path)
    overlay = PdfReader(overlay_pdf_path)
    writer = PdfWriter()

    for i, page in enumerate(reader.pages):
        if i < len(overlay.pages):
            page.merge_page(overlay.pages[i])
        writer.add_page(page)

    with open(out_path, "wb") as f:
        writer.write(f)


def _make_overlay_single_family(app_obj, overlay_path, sig_path=None):
    from app.services.application_layout import draw_application_overlay
    draw_application_overlay(app_obj, overlay_path, "single_family", sig_path)


def _make_overlay_member_product(app_obj, overlay_path, sig_path=None):
    from app.services.application_layout import draw_application_overlay
    draw_application_overlay(app_obj, overlay_path, "member_product", sig_path)


def _append_policy_terms(writer, template_choice="single_family"):
    """Append the correct SVG-derived terms and conditions to the application PDF."""
    preferred = MEMBER_PRODUCT_TERMS_PDF_PATH if template_choice == "member_product" else FAMILY_TERMS_PDF_PATH
    terms_path = preferred if os.path.exists(preferred) else TERMS_PDF_PATH
    if not os.path.exists(terms_path):
        return
    try:
        terms = PdfReader(terms_path)
        for page in terms.pages:
            writer.add_page(page)
    except Exception:
        return

def _add_terms_page(writer, app_obj, sig_path=None):
    packet = io.BytesIO()
    c = canvas.Canvas(packet, pagesize=A4)
    y = 800
    c.setFont("Helvetica-Bold", 14)
    c.drawString(40, y, "Application - Terms, Conditions and Electronic Signature Record")
    y -= 30
    c.setFont("Helvetica", 9)
    lines = [
        f"Application Ref: {app_obj.application_ref}",
        f"Policy Number: {app_obj.policy_number or 'Pending'}",
        f"Client: {_safe(app_obj.first_names)} {_safe(app_obj.surname)}",
        f"ID Number: {_safe(app_obj.id_number)}",
        f"Product: {_safe(app_obj.product.product_name if app_obj.product else '')} / {_safe(app_obj.product.plan_name if app_obj.product else '')}",
        f"Cover Amount: R {_money(app_obj.cover_amount)}",
        f"Monthly Premium: R {_money(app_obj.monthly_premium)}",
        f"Joining Fee: {'R 0 - Waived' if app_obj.joining_fee_waived else 'R ' + _money(app_obj.joining_fee)}",
        f"Waiting Period: {_safe(app_obj.waiting_period)}",
        "",
        "Client signatures are recorded using ID-number verification, typed name and a separate drawn signature.",
        "The signed application form must be read together with the policy terms and conditions supplied to the client.",
        "Each required client signature is captured separately at its labelled location.",
        "The intermediary signature is reserved for staff and is not signed by the client.",
        "",
        f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
    ]
    for line in lines:
        c.drawString(40, y, line[:120])
        y -= 18
    if sig_path and os.path.exists(sig_path):
        c.drawString(40, y - 5, "Client Signature:")
        _draw_signature(c, sig_path, 145, y - 36, 220, 65)
    c.save()
    packet.seek(0)
    page = PdfReader(packet).pages[0]
    writer.add_page(page)


def _generate_gold_family_fillable(app_obj, out_path, sig_path):
    """Fill the Gold Family application and place every saved signature separately."""
    template = os.path.join(TEMPLATE_DIR, "gold_family_plan_fillable_application.pdf")
    if not os.path.exists(template):
        raise FileNotFoundError("Gold Family Plan application template is missing")
    children = _rows(app_obj.dependents_json)[:6]
    extended = _rows(app_obj.extended_family_json)[:4]
    payment = str(app_obj.payment_method or "").lower()
    debit_day = str(app_obj.debit_day or "").strip()
    values = {
        "agent_name": _safe(app_obj.agent_name), "agent_code": _safe(app_obj.agent_code),
        "policy_no": _safe(app_obj.policy_number or app_obj.application_ref),
        "surname": _safe(app_obj.surname), "first_names": _safe(app_obj.first_names),
        "title": _safe(app_obj.title), "id_number": _safe(app_obj.id_number),
        "dob": _date_boxes(app_obj.date_of_birth), "contact": _safe(app_obj.cell_number),
        "email": _safe(app_obj.email), "res_address": _safe(app_obj.residential_address or app_obj.address),
        "postal_address": _safe(app_obj.postal_address),
        "postal_code": _safe(app_obj.residential_postal_code or app_obj.postal_code),
        "spouse_surname": _safe(app_obj.spouse_surname), "spouse_first": _safe(app_obj.spouse_first_names),
        "spouse_title": _safe(app_obj.spouse_title), "spouse_id": _safe(app_obj.spouse_id_number),
        "spouse_dob": _date_boxes(app_obj.spouse_date_of_birth),
        "beneficiary_name": _safe(app_obj.beneficiary_full_names), "beneficiary_rel": _safe(app_obj.beneficiary_relationship),
        "beneficiary_id": _safe(app_obj.beneficiary_id_number), "beneficiary_dob": _date_boxes(app_obj.beneficiary_date_of_birth),
        "first_deduction": _date_boxes(app_obj.first_deduction_date),
        "bank_name": _safe(app_obj.bank_name), "branch_name": _safe(app_obj.branch_name),
        "account_no": _safe(app_obj.account_number), "branch_code": _safe(app_obj.branch_code),
        "account_holder": _safe(app_obj.account_holder), "account_type": _safe(app_obj.account_type),
        "monthly_premium_a": _money(app_obj.monthly_premium), "premium_b": _money(app_obj.extended_premium),
        "extended_premium_b": _money(app_obj.extended_premium),
        "total_premium": _money(app_obj.total_payment or app_obj.monthly_premium),
        "employer": _safe(app_obj.employer), "persal_no": _safe(app_obj.persal_no),
        "signature_date": datetime.now().strftime("%d/%m/%Y"),
        "pay_cash": "/Yes" if "cash" in payment else "/Off",
        "pay_debit": "/Yes" if "debit" in payment else "/Off",
        "pay_persal": "/Yes" if "persal" in payment else "/Off",
    }
    for day, suffix in (("1", "1st"), ("5", "5th"), ("15", "15th"), ("20", "20th"), ("25", "25th"), ("30", "30th")):
        values[f"debit_{suffix}"] = "/Yes" if debit_day == day else "/Off"
    for index in range(1, 7):
        row = children[index - 1] if index <= len(children) else {}
        values[f"child_{index}_name"] = _safe(row.get("full_name"))
        values[f"child_{index}_rel"] = _safe(row.get("relationship"))
        values[f"child_{index}_id"] = _safe(row.get("id_or_dob"))
    for index in range(1, 5):
        row = extended[index - 1] if index <= len(extended) else {}
        values[f"ext_{index}_name"] = " / ".join(filter(None, [_safe(row.get("full_name")), _safe(row.get("relationship"))]))
        values[f"ext_{index}_id"] = _safe(row.get("id_or_dob"))
        values[f"ext_{index}_cover"] = _safe(row.get("cover"))
        values[f"ext_{index}_premium"] = _safe(row.get("premium"))

    writer = PdfWriter()
    writer.clone_document_from_reader(PdfReader(template))
    writer.update_page_form_field_values(None, values, auto_regenerate=False)
    label_data = io.BytesIO()
    label = canvas.Canvas(label_data, pagesize=A4)
    label.setFillColorRGB(1, 1, 1); label.rect(296, 480, 80, 18, stroke=0, fill=1)
    label.setFillColorRGB(0, 0, 0); label.setFont("Helvetica", 8); label.drawString(300, 486, "Street Code")
    label.save(); label_data.seek(0)
    writer.pages[0].merge_page(PdfReader(label_data).pages[0])
    # The supplied Gold form contains the declaration, while the official policy
    # terms live in the standard terms template. Keep them in one reviewable PDF
    # and add a visible client signature record for the terms.
    _append_policy_terms(writer, "single_family")
    _add_terms_page(writer, app_obj, None)

    # Do not reuse one signature image for every field. Each signature pad is
    # persisted as its own DocumentSignature row and merged only into the exact
    # PDF rectangle represented by that row.
    from app.services.signature_fields import application_fields, signature_rows
    fields, records = application_fields(app_obj), signature_rows(app_obj)
    for target in fields:
        row = records.get(target["key"])
        signature_path = row.signature_image_path if row else None
        # Compatibility for older completed applications that only have the
        # legacy principal signature passed to the PDF generator.
        if not signature_path and target["key"] == "application:principal":
            signature_path = sig_path
        target["signed"] = bool(row or signature_path)
        if not signature_path or not os.path.exists(signature_path):
            continue
        page_index = target["page"] - 1
        if page_index < 0 or page_index >= len(writer.pages):
            continue
        overlay_data = io.BytesIO()
        overlay = canvas.Canvas(overlay_data, pagesize=A4)
        x1, y1, x2, y2 = target["rect"]
        _draw_signature(overlay, signature_path, x1, y1, x2 - x1, y2 - y1)
        overlay.save()
        overlay_data.seek(0)
        writer.pages[page_index].merge_page(PdfReader(overlay_data).pages[0])

    # Blank AcroForm signature widgets can paint over the merged handwriting
    # on mobile. Remove only widgets whose separate signature has been saved.
    if len(writer.pages) > 1:
        signed_widgets = set()
        if records.get("application:principal") or sig_path:
            signed_widgets.add("policyholder_signature")
        if records.get("application:account"):
            signed_widgets.add("account_signature")
        if signed_widgets:
            from pypdf.generic import ArrayObject, NameObject
            kept = ArrayObject()
            for reference in writer.pages[1].get("/Annots", []):
                annotation = reference.get_object()
                parent = annotation.get("/Parent")
                parent = parent.get_object() if parent else None
                field_name = str(annotation.get("/T") or (parent.get("/T") if parent else "") or "")
                if field_name in signed_widgets:
                    continue
                kept.append(reference)
            writer.pages[1][NameObject("/Annots")] = kept

    writer.add_metadata({"/Subject": "martins-signature:" + json.dumps({"fields": fields})})
    with open(out_path, "wb") as stream:
        writer.write(stream)
    return out_path


@durable_pdf
def generate_application_pdf(app_obj, out_path, signature_path_override=None):
    _ensure_dir(out_path)
    sig_path = _signature_for_app(app_obj, signature_path_override, "application")

    if app_obj.form_template == "gold_family_fillable":
        return _generate_gold_family_fillable(app_obj, out_path, sig_path)

    product_text = ((_safe(app_obj.product.product_name if app_obj.product else "")) + " " + (_safe(app_obj.product.plan_name if app_obj.product else ""))).lower()
    template_choice = app_obj.form_template or ("member_product" if ("member +" in product_text or ("product" in product_text and ("+" in product_text or "member" in product_text))) else "single_family")

    if template_choice == "member_product":
        template = os.path.join(TEMPLATE_DIR, "application_member_product.pdf")
    else:
        template = os.path.join(TEMPLATE_DIR, "application_single_family.pdf")

    overlay_path = out_path + ".overlay.pdf"
    if template_choice == "member_product":
        _make_overlay_member_product(app_obj, overlay_path, sig_path)
    else:
        _make_overlay_single_family(app_obj, overlay_path, sig_path)

    # Merge first-page overlay
    tmp_path = out_path + ".tmp.pdf"
    _merge_template(template, overlay_path, tmp_path)

    # Add official policy terms and conditions plus signature certificate page.
    reader = PdfReader(tmp_path)
    writer = PdfWriter()
    for page in reader.pages:
        writer.add_page(page)
    _append_policy_terms(writer, template_choice)
    # Move the original narrow signature line into a full-height signing band.
    from pypdf import Transformation
    terms_page=writer.pages[1]
    packet=io.BytesIO();clean=canvas.Canvas(packet,pagesize=A4);clean.setFillColorRGB(1,1,1)
    if template_choice=='member_product':clean.rect(304,A4[1]-437,270,10,fill=1,stroke=0)
    else:clean.rect(13,A4[1]-833,280,14,fill=1,stroke=0)
    clean.save();packet.seek(0);terms_page.merge_page(PdfReader(packet).pages[0])
    terms_page.add_transformation(Transformation().scale(1,0.93).translate(0,A4[1]*0.07))
    packet=io.BytesIO();band=canvas.Canvas(packet,pagesize=A4);band.setFont('Helvetica-Bold',9)
    band.drawString(30,A4[1]-788,'Main member signature')
    band.line(30,A4[1]-828,280,A4[1]-828)
    if 'debit' in (app_obj.payment_method or '').lower() or any((app_obj.account_number,app_obj.account_holder,app_obj.bank_name)):
        band.drawString(320,A4[1]-788,"Account holder's signature — debit order")
        band.line(320,A4[1]-828,570,A4[1]-828)
    band.save();packet.seek(0);terms_page.merge_page(PdfReader(packet).pages[0])
    _add_terms_page(writer, app_obj, None)
    from app.services.signature_fields import application_fields, signature_rows
    fields, records = application_fields(app_obj), signature_rows(app_obj)
    for target in fields:
        row = records.get(target['key'])
        target['signed'] = bool(row)
        if row:
            packet = io.BytesIO()
            overlay = canvas.Canvas(packet, pagesize=A4)
            x1,y1,x2,y2 = target['rect']
            _draw_signature(overlay, row.signature_image_path, x1,y1,x2-x1,y2-y1)
            overlay.save(); packet.seek(0)
            writer.pages[target['page']-1].merge_page(PdfReader(packet).pages[0])
    writer.add_metadata({"/Subject": "martins-signature:" + json.dumps({"fields": fields})})
    with open(out_path, "wb") as f:
        writer.write(f)

    try:
        os.remove(overlay_path)
        os.remove(tmp_path)
    except Exception:
        pass
    return out_path



def _signature_for_app(app_obj, signature_path_override=None, document_type=None):
    if signature_path_override:
        return signature_path_override
    if document_type:
        from app.models import DocumentSignature
        document_sig = DocumentSignature.query.filter_by(application_id=app_obj.id, document_type=document_type).order_by(DocumentSignature.signed_at.desc()).first()
        if document_sig:
            return document_sig.signature_image_path
    sig = _latest_signature(app_obj)
    return sig.signature_image_path if sig else None


def _client_details(app_obj):
    return [
        ("Full Name", f"{_safe(app_obj.first_names)} {_safe(app_obj.surname)}"),
        ("ID Number", _safe(app_obj.id_number)),
        ("Date of Birth", _date_boxes(app_obj.date_of_birth)),
        ("Mobile Number", _safe(app_obj.cell_number)),
        ("Email Address", _safe(app_obj.email)),
        ("Physical Address", _safe(app_obj.residential_address or app_obj.address)),
        ("Policy Number", _safe(app_obj.policy_number or "Pending")),
        ("Application Ref", _safe(app_obj.application_ref)),
        ("Product", f"{_safe(app_obj.product.product_name if app_obj.product else '')} / {_safe(app_obj.product.plan_name if app_obj.product else '')}"),
        ("Monthly Premium", f"R {_money(app_obj.monthly_premium)}"),
        ("Cover Amount", f"R {_money(app_obj.cover_amount)}"),
        ("Agent Name", _safe(app_obj.agent_name)),
        ("Agent Code", _safe(app_obj.agent_code)),
    ]


def _draw_key_values(c, rows, x, y, label_w=120, leading=16, size=9):
    for label, value in rows:
        c.setFont("Helvetica-Bold", size)
        c.drawString(x, y, f"{label}:")
        c.setFont("Helvetica", size)
        c.drawString(x + label_w, y, _safe(value)[:85])
        y -= leading
    return y

@durable_pdf
def generate_welcome_pack(app_obj, out_path, signature_path_override=None):
    _ensure_dir(out_path)
    c = canvas.Canvas(out_path, pagesize=A4)
    width, height = A4

    def page_header(page_title="Policy Welcome Pack"):
        _brand_header(c, page_title)
        return height - 118

    y = page_header("Policy Welcome Pack")
    y = _draw_paragraph(c, f"Dear {app_obj.first_names or 'Policyholder'},", 32, y, max_chars=95, leading=14, size=10)
    y = _draw_paragraph(c, "Thank you for choosing Martin's Funerals. We are pleased to welcome you as a valued policyholder. This Welcome Pack contains important information about your policy, benefits, premiums, waiting periods, claims procedures and contact details. Please read this document carefully and keep it in a safe place for future reference.", 32, y - 4, max_chars=105, leading=12, size=8.7)
    y -= 8

    y = _section(c, "Policy Schedule", 32, y)
    policy_rows = [
        ("Policy Number", app_obj.policy_number or "Pending"),
        ("Policyholder Name", f"{_safe(app_obj.first_names)} {_safe(app_obj.surname)}"),
        ("ID Number", app_obj.id_number),
        ("Date of Birth", _date_boxes(app_obj.date_of_birth)),
        ("Policy Start Date", _date_boxes(app_obj.inception_date) or _signed_date(app_obj)),
        ("Policy Status", "Active" if getattr(app_obj, "signed_at", None) else _safe(app_obj.status or "Pending")),
        ("Product / Plan", f"{_safe(app_obj.product.product_name if app_obj.product else '')} / {_safe(app_obj.product.plan_name if app_obj.product else '')}"),
        ("Application Reference", app_obj.application_ref),
    ]
    y = _draw_key_values(c, policy_rows, 42, y, label_w=145, leading=14, size=8.5)
    y -= 6

    y = _section(c, "Cover Details", 32, y)
    c.setFont("Helvetica-Bold", 8.7)
    c.drawString(42, y, "Benefit Description")
    c.drawString(320, y, "Cover Amount")
    y -= 15
    rule = getattr(app_obj.product, "rules", None) if getattr(app_obj, "product", None) else None
    cover_rows = [
        ("Main Member Cover", getattr(rule, "main_member_cover", None) or app_obj.cover_amount),
        ("Spouse Cover", getattr(rule, "spouse_cover", None) or ""),
        ("Child Cover", getattr(rule, "family_14_21", None) or ""),
        ("Parent Cover", getattr(rule, "extended_cover", None) or ""),
        ("Extended Family Cover", getattr(rule, "extended_cover", None) or ""),
    ]
    c.setFont("Helvetica", 8.5)
    for label, amount in cover_rows:
        c.drawString(42, y, label)
        c.drawString(320, y, f"R {_money(amount)}" if _safe(amount) else "As per selected policy")
        y -= 14
    y -= 6

    y = _section(c, "Premium Details", 32, y)
    acct = _safe(app_obj.account_number)
    last4 = acct[-4:] if acct else "N/A"
    premium_rows = [
        ("Monthly Premium", f"R {_money(app_obj.monthly_premium)}"),
        ("Collection Method", _safe(app_obj.payment_method)),
        ("Collection Date", _safe(app_obj.debit_day or app_obj.first_deduction_date)),
        ("Bank Account", f"Last 4 digits: {last4}" if "debit" in _safe(app_obj.payment_method).lower() else "Not applicable"),
    ]
    y = _draw_key_values(c, premium_rows, 42, y, label_w=145, leading=14, size=8.5)
    y = _draw_paragraph(c, "Please ensure sufficient funds are available on the collection date to avoid missed premium payments and potential policy lapses.", 42, y - 4, max_chars=105, leading=12, size=8.2)
    y -= 8

    y = _section(c, "Waiting Periods", 32, y)
    waiting = _safe(app_obj.waiting_period or (str(getattr(app_obj.product, "waiting_period_months", "")) + " months" if getattr(app_obj, "product", None) else ""))
    waiting_rows = [("Natural Death", waiting), ("Accidental Death", "No waiting period after first premium is received"), ("Additional Benefits", waiting or "As per policy terms")]
    y = _draw_key_values(c, waiting_rows, 42, y, label_w=145, leading=14, size=8.5)
    y = _draw_paragraph(c, "Please refer to the Terms and Conditions for full details.", 42, y - 4, max_chars=105, leading=12, size=8.2)

    _footer(c)
    c.showPage()
    y = page_header("Policy Welcome Pack")

    y = _section(c, "Terms and Conditions", 32, y)
    terms = [
        "Premiums must be paid when due.",
        "Claims must be submitted with all required documentation.",
        "Incorrect or incomplete information may affect cover.",
        "Benefits are subject to policy limits and exclusions.",
        "Waiting periods apply where specified.",
        "A complete copy of the policy terms and conditions is attached to the application form.",
    ]
    c.setFont("Helvetica", 8.5)
    for item in terms:
        c.drawString(48, y, "- " + item)
        y -= 13
    y -= 8

    y = _section(c, "Claims Procedure", 32, y)
    claims = [
        "Step 1 - Notify Martin's Funerals or the insurer as soon as possible.",
        "Step 2 - Submit certified ID copy of claimant, certified ID copy of deceased, death certificate, BI-1663 form where applicable, proof of banking details and any additional documents requested.",
        "Step 3 - The claim will be assessed by the insurer.",
        "Step 4 - If approved, payment will be processed to the nominated beneficiary.",
    ]
    for item in claims:
        y = _draw_paragraph(c, item, 42, y, max_chars=105, leading=12, size=8.3)
        y -= 4
    y -= 4

    y = _section(c, "Contact Details", 32, y)
    contact_rows = [
        ("Company", "Martin's Funerals"),
        ("Telephone", "0860 911 777"),
        ("Email", "info@martinsdirect.com"),
        ("Website", "www.martinsfunerals.co.za"),
        ("FSP", "48189"),
    ]
    y = _draw_key_values(c, contact_rows, 42, y, label_w=110, leading=13, size=8.3)
    y -= 8

    y = _section(c, "Complaints Process", 32, y)
    complaint = "If you are dissatisfied with any aspect of your policy or service received, contact Martin's Funerals first so that the complaint can be recorded and resolved promptly and fairly. If you are not satisfied with the outcome, you may escalate your complaint to the relevant insurer, underwriter, compliance department or applicable industry ombud."
    y = _draw_paragraph(c, complaint, 42, y, max_chars=105, leading=12, size=8.3)
    y -= 12

    y = _section(c, "Client Acknowledgement", 32, y)
    c.setFont("Helvetica", 8.5)
    c.drawString(42, y, "I confirm that I have received this Welcome Pack and understand the information provided.")
    y -= 20
    c.setFont("Helvetica-Bold", 8.5)
    c.drawString(42, y, "Policyholder Name:")
    c.setFont("Helvetica", 8.5)
    c.drawString(160, y, f"{_safe(app_obj.first_names)} {_safe(app_obj.surname)}")
    c.setFont("Helvetica-Bold", 8.5)
    c.drawString(365, y, "Date:")
    c.setFont("Helvetica", 8.5)
    c.drawString(405, y, _signed_date(app_obj))
    y -= 18
    c.setFont("Helvetica-Bold", 8.5)
    c.drawString(42, y, "Client Signature:")
    _box(c, 160, y + 12, 245, 55)
    c.setSubject("martins-signature:" + json.dumps({"page": c.getPageNumber(), "rect": [160, y - 43, 405, y + 12]}))
    sig_path = _signature_for_app(app_obj, signature_path_override, "welcome")
    if sig_path and os.path.exists(sig_path):
        _draw_signature(c, sig_path, 165, y - 33, 235, 50)

    _footer(c)
    c.save()
    return out_path

@durable_pdf
def generate_popia_pdf(app_obj, out_path, signature_path_override=None):
    _ensure_dir(out_path)
    c = canvas.Canvas(out_path, pagesize=A4)
    width, height = A4
    _brand_header(c, "POPIA Consent Form")
    y = height - 118

    y = _draw_paragraph(c, "This form records the consent of the applicant/policyholder for the collection, processing, storage and sharing of personal information in accordance with the Protection of Personal Information Act, 2013 (POPIA).", 32, y, max_chars=105, leading=12, size=8.8)
    y -= 8

    y = _section(c, "Client Details", 32, y)
    rows = [
        ("Full Names", f"{_safe(app_obj.first_names)} {_safe(app_obj.surname)}"),
        ("ID Number", _safe(app_obj.id_number)),
        ("Date of Birth", _date_boxes(app_obj.date_of_birth)),
        ("Mobile Number", _safe(app_obj.cell_number)),
        ("Email Address", _safe(app_obj.email)),
        ("Physical Address", _safe(app_obj.residential_address or app_obj.address)),
        ("Policy Number", _safe(app_obj.policy_number or "Pending")),
        ("Product / Plan Selected", f"{_safe(app_obj.product.product_name if app_obj.product else '')} {_safe(app_obj.product.plan_name if app_obj.product else '')}"),
    ]
    y = _draw_key_values(c, rows, 42, y, label_w=140, leading=13, size=8.4)
    y -= 6

    c.setFillColorRGB(*PURPLE)
    c.setFont("Helvetica-Bold", 10)
    c.drawString(42, y, "CONSENT TO PROCESS PERSONAL INFORMATION")
    c.setFillColorRGB(0, 0, 0)
    y -= 16
    p = "I hereby voluntarily provide my personal information and authorise Martin's Funerals, its insurers, administrators, underwriters, service providers and authorised partners to collect, process, verify, store, update and share my personal information for the purposes of assessing and administering my insurance application, policy underwriting and administration, claims administration, compliance, fraud prevention, risk assessment, verification, customer service and policy maintenance."
    y = _draw_paragraph(c, p, 42, y, max_chars=105, leading=11, size=8)
    y -= 4
    y = _draw_paragraph(c, "I understand that my personal information will be processed in accordance with the Protection of Personal Information Act, 2013 (POPIA).", 42, y, max_chars=105, leading=11, size=8)
    y -= 10

    c.setFillColorRGB(*PURPLE)
    c.setFont("Helvetica-Bold", 10)
    c.drawString(42, y, "MARKETING CONSENT")
    c.setFillColorRGB(0, 0, 0)
    y -= 16
    marketing = _marketing_consent(app_obj)
    _checkbox_line(c, marketing is True, "YES - I consent to receiving product updates and marketing communications.", 42, y, 8.5)
    y -= 18
    _checkbox_line(c, marketing is False, "NO - I do not consent to receiving marketing communications.", 42, y, 8.5)
    y -= 22

    c.setFillColorRGB(*PURPLE)
    c.setFont("Helvetica-Bold", 10)
    c.drawString(42, y, "CLIENT DECLARATION")
    c.setFillColorRGB(0, 0, 0)
    y -= 15
    y = _draw_paragraph(c, "I confirm that the information provided is true and correct and understand how my personal information will be used.", 42, y, max_chars=105, leading=11, size=8.3)
    y -= 10

    c.setFont("Helvetica-Bold", 9)
    c.drawString(42, y, "CLIENT SIGNATURE:")
    _box(c, 42, y - 6, 265, 52)
    c.setSubject("martins-signature:" + json.dumps({"page": c.getPageNumber(), "rect": [42, y - 58, 307, y - 6]}))
    sig_path = _signature_for_app(app_obj, signature_path_override, "popia")
    if sig_path and os.path.exists(sig_path):
        _draw_signature(c, sig_path, 55, y - 50, 240, 48)
    c.setFont("Helvetica-Bold", 9)
    c.drawString(340, y - 28, "Date:")
    c.setFont("Helvetica", 9)
    c.drawString(382, y - 28, _signed_date(app_obj) if sig_path else "____ / ____ / ______")
    y -= 78

    y = _section(c, "Company Details", 32, y, w=260)
    company_rows = [("Company Name", "Martin's Funerals"), ("FSP", "48189"), ("Contact", "0860 911 777"), ("Website", "www.martinsfunerals.co.za")]
    _draw_key_values(c, company_rows, 42, y, label_w=95, leading=12, size=7.8)
    _footer(c)
    c.save()
    return out_path

@durable_pdf
def generate_disclosure_pdf(app_obj, out_path, signature_path_override=None):
    _ensure_dir(out_path)
    c = canvas.Canvas(out_path, pagesize=A4)
    _brand_header(c, "Policy Disclosure Record and Client Confirmation")
    y = A4[1] - 135

    c.setFillColorRGB(*PURPLE)
    c.setFont("Helvetica-Bold", 11)
    c.drawString(42, y, "Client and Policy Details")
    c.setFillColorRGB(0, 0, 0)
    y -= 18
    y = _draw_key_values(c, _client_details(app_obj) + [
        ("Waiting Period", _safe(app_obj.waiting_period)),
        ("Joining Fee", "R 0 - Waived" if app_obj.joining_fee_waived else f"R {_money(app_obj.joining_fee)}"),
        ("Payment Method", _safe(app_obj.payment_method)),
    ], 50, y, label_w=125, leading=15, size=8.8)
    y -= 8

    c.setFillColorRGB(*PURPLE)
    c.setFont("Helvetica-Bold", 11)
    c.drawString(42, y, "Disclosure Confirmation")
    c.setFillColorRGB(0, 0, 0)
    y -= 18
    items = [
        "Policy benefits and cover amounts explained",
        "Premium payable and payment method explained",
        "Waiting periods and exclusions explained",
        "Cancellation rights and cooling-off period explained",
        "Claims process and required documentation explained",
        "Beneficiary process and complaints process explained",
        "Client had an opportunity to ask questions and elected to proceed",
    ]
    c.setFont("Helvetica", 9)
    for item in items:
        c.drawString(55, y, "[X] " + item)
        y -= 14

    y -= 20
    c.setFont("Helvetica-Bold", 10)
    c.drawString(50, y, "Client Signature:")
    _box(c, 160, y + 12, 245, 65)
    c.setSubject("martins-signature:" + json.dumps({"page": c.getPageNumber(), "rect": [160, y - 53, 405, y + 12]}))
    sig_path = _signature_for_app(app_obj, signature_path_override, "disclosure")
    if sig_path and os.path.exists(sig_path):
        _draw_signature(c, sig_path, 165, y - 48, 230, 55)
        c.setFont("Helvetica", 9)
        c.drawString(50, y - 70, f"Date: {_signed_date(app_obj)}")

    _footer(c)
    c.save()
    return out_path


@durable_pdf
def generate_fica_pdf(app_obj, out_path, signature_path_override=None):
    _ensure_dir(out_path)
    c = canvas.Canvas(out_path, pagesize=A4)
    _brand_header(c, "FICA Documents Required and Verification Checklist")
    y = A4[1] - 135

    c.setFillColorRGB(*PURPLE)
    c.setFont("Helvetica-Bold", 11)
    c.drawString(42, y, "Policyholder Details")
    c.setFillColorRGB(0, 0, 0)
    y -= 18
    y = _draw_key_values(c, _client_details(app_obj), 50, y, label_w=125, leading=15, size=8.8)
    y -= 10

    method = _safe(app_obj.payment_method).lower()
    debit = "debit" in method
    citizen = len(''.join(ch for ch in _safe(app_obj.id_number) if ch.isdigit())) == 13

    uploaded_types = set()
    rejected_types = set()
    try:
        from app.models import ClientFicaDocument
        rows = ClientFicaDocument.query.filter_by(application_id=app_obj.id).all()
        for row in rows:
            if getattr(row, "status", "") == "Rejected":
                rejected_types.add(row.document_type)
            else:
                uploaded_types.add(row.document_type)
    except Exception:
        rows = []

    required = ["id_copy" if citizen else "passport", "proof_of_address"]
    if not citizen:
        required.append("permit_visa")

    labels = {
        "id_copy": "South African ID copy",
        "passport": "Passport copy",
        "permit_visa": "Permit / Visa",
        "proof_of_address": "Proof of address",
        "bank_statement": "Bank statement / bank confirmation",
    }
    outstanding = [x for x in required if x not in uploaded_types]
    fica_status = "Complete - all required documents received" if not outstanding else "Pending - outstanding documents remain"

    c.setFillColorRGB(*PURPLE)
    c.setFont("Helvetica-Bold", 11)
    c.drawString(42, y, "FICA Rules Applied")
    c.setFillColorRGB(0, 0, 0)
    y -= 18
    rules = [
        ("Client Type", "South African Citizen" if citizen else "Foreign National / Passport"),
        ("ID Number Verified", "Passed - entered ID matched signing link" if citizen else "Passport captured - document review required"),
        ("Identity Required", "South African ID copy" if citizen else "Passport copy and valid permit/visa"),
        ("Proof of Address", "Required"),
        ("FICA Status", fica_status),
    ]
    y = _draw_key_values(c, rules, 50, y, label_w=140, leading=16, size=9)
    y -= 8

    c.setFont("Helvetica-Bold", 10)
    c.drawString(50, y, "Verification Checklist")
    y -= 18
    c.setFont("Helvetica", 9)

    for key in required:
        status = "Received" if key in uploaded_types else "Outstanding"
        prefix = "[X]" if key in uploaded_types else "[ ]"
        c.drawString(55, y, f"{prefix} {labels.get(key, key)} - {status}")
        y -= 15

    if rows:
        y -= 4
        c.setFont("Helvetica-Bold", 10)
        c.drawString(50, y, "Uploaded Documents")
        y -= 16
        c.setFont("Helvetica", 8)
        for row in rows[:8]:
            c.drawString(55, y, f"- {labels.get(row.document_type, row.document_type)}: {getattr(row, 'original_filename', '')} ({getattr(row, 'status', 'Received')})")
            y -= 12

    # FICA is a staff verification checklist, not a separately signed client document.
    _footer(c)
    c.save()
    return out_path

def generate_telesales_script_pdf(session, script_steps, qa_sections, output_path):
    """Generate the stored Sales Script + QA Call Monitoring PDF."""
    _ensure_dir(output_path)
    c = canvas.Canvas(output_path, pagesize=A4)
    width, height = A4

    def answers():
        try:
            return json.loads(session.answers_json or "{}")
        except Exception:
            return {}

    def new_page(title="Insurance Sales FUNERAL COVER SALES SCRIPT"):
        _brand_header(c, title)
        _footer(c)
        return height - 125

    ans = answers()
    y = new_page("Insurance Sales FUNERAL COVER SALES SCRIPT")
    x = 34
    c.setFont("Helvetica-Bold", 12)
    c.drawString(x, y, "Martin's Funerals - Sales Script and QA Call Monitoring Record")
    y -= 20
    c.setFont("Helvetica", 9)
    meta = [
        ("Client Name", getattr(session, "client_name", "")),
        ("Policy Number", getattr(session, "policy_number", "")),
        ("Agent", getattr(getattr(session, "agent", None), "name", "")),
        ("Branch", getattr(session, "branch", "")),
        ("Status", getattr(session, "status", "")),
        ("QA Score", f"{getattr(session, 'qa_score', 0) or 0} / 100 - {getattr(session, 'qa_result', '') or ''}"),
        ("Created", getattr(session, "created_at", "")),
        ("Completed", getattr(session, "completed_at", "")),
    ]
    for label, value in meta:
        c.setFont("Helvetica-Bold", 8.5); c.drawString(x, y, f"{label}:")
        c.setFont("Helvetica", 8.5); c.drawString(x+95, y, _safe(value)[:90])
        y -= 13
    if getattr(session, "blocked_reason", None):
        y -= 4
        c.setFillColorRGB(0.8, 0, 0); c.setFont("Helvetica-Bold", 9)
        c.drawString(x, y, "Blocked Reason: " + _safe(session.blocked_reason)[:105])
        c.setFillColorRGB(0,0,0); y -= 16

    y -= 8
    y = _section(c, "Script Answers and QA Cross-Reference", x, y, 530)
    for step in script_steps:
        if y < 92:
            c.showPage(); y = new_page("Insurance Sales SCRIPT RECORD")
        rec = ans.get(str(step.get("id")), {})
        answer = _safe(rec.get("answer", "not answered")).upper()
        c.setFont("Helvetica-Bold", 9)
        c.drawString(x, y, f"{step.get('id')}. {step.get('title')}  -  Answer: {answer}")
        y -= 12
        c.setFont("Helvetica", 8)
        c.drawString(x+14, y, f"QA: {step.get('qa')}")
        y -= 11
        for line in _wrap_text(step.get("question", ""), 96):
            c.drawString(x+14, y, line); y -= 10
        note = rec.get("note")
        if note:
            c.setFont("Helvetica-Oblique", 7.5)
            for line in _wrap_text("Note: " + note, 96):
                c.drawString(x+14, y, line); y -= 10
        y -= 4

    c.showPage()
    y = new_page("QUALITY ASSURANCE CHECKLIST")
    c.setFont("Helvetica-Bold", 11)
    c.drawString(x, y, "QUALITY ASSURANCE (QA) CALL MONITORING CHECKLIST")
    y -= 18
    c.setFont("Helvetica", 8.5)
    c.drawString(x, y, "PASS MARK: 90%")
    y -= 16
    total = 0
    for name, points in qa_sections:
        if y < 75:
            c.showPage(); y = new_page("QUALITY ASSURANCE CHECKLIST")
        section_has_block = False
        for step in script_steps:
            rec = ans.get(str(step.get("id")), {})
            if step.get("qa") == name and ((step.get("block_on_no") and rec.get("answer") == "no") or not step.get("enabled", True)):
                section_has_block = True
        scored = 0 if section_has_block else points
        total += scored
        c.setFont("Helvetica-Bold", 9)
        c.drawString(x, y, f"{name} ({points} POINTS)")
        c.setFont("Helvetica", 9)
        c.drawRightString(width-40, y, f"Score: {scored} / {points}")
        y -= 14
        linked = [step for step in script_steps if step.get("qa") == name]
        for step in linked:
            rec = ans.get(str(step.get("id")), {})
            mark = "[X]" if rec.get("answer") in {"yes", "no"} else "[ ]"
            c.setFont("Helvetica", 8)
            c.drawString(x+15, y, f"{mark} Step {step.get('id')}: {step.get('title')} - {_safe(rec.get('answer', 'not answered')).upper()}")
            y -= 10
        y -= 5
    y -= 6
    c.setFont("Helvetica-Bold", 12)
    result = "PASS" if total >= 90 else "FAIL"
    c.drawString(x, y, f"FINAL SCORE: {total} / 100  -  {result}")
    y -= 28
    c.setFont("Helvetica", 9)
    c.drawString(x, y, "Evaluator Comments:")
    y -= 13
    c.line(x, y, width-40, y); y -= 18
    c.line(x, y, width-40, y); y -= 28
    c.drawString(x, y, "Evaluator Signature: ________________________________")
    c.save()
    return output_path

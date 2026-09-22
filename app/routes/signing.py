from app.services.cdd_service import generate_cdd_pdf, answers_for, save_answers, completed as cdd_completed, FIELDS as CDD_FIELDS
from app.services.signature_fields import application_fields, signed_documents, signature_rows
from app.services.marketing_consent import consent_value, apply_consent
from app.services.client_storage import application_folder, store_document
import os, base64, json, secrets, io
from pypdf import PdfReader
from PIL import Image, ImageChops
from datetime import datetime
from flask import Blueprint, render_template, request, current_app, abort, send_file, session, redirect, url_for, flash
from werkzeug.utils import secure_filename
from app import db
from sqlalchemy import MetaData, Table
from sqlalchemy.sql.sqltypes import DateTime, Boolean, Integer, Numeric, Float
from app.models import ClientApplication, ApplicationSignature, ClientFicaDocument, DocumentSignature
from app.services.pdf_service import generate_application_pdf, generate_welcome_pack, generate_popia_pdf, generate_disclosure_pdf, generate_fica_pdf
from app.services.email_service import send_email
from app.services.compliance_service import assert_application_rules
from app.services.fica_validation_service import validate_fica_upload
from app.services.document_status_service import required_fica_documents, fica_label

signing_bp = Blueprint("signing", __name__, url_prefix="/sign")

REQUIRED_SIGNATURE_DOCS = [
    ("application", "Application Form, Terms and Conditions, and Debit Order (when selected)"),
    ("popia", "POPIA Consent"),
    ("disclosure", "Policy Disclosure"),
    ("welcome", "Welcome Pack Acknowledgement"),
    ("cdd", "Annexure J.1 - Client Due Diligence"),
]

DOC_LABELS = dict(REQUIRED_SIGNATURE_DOCS + [("fica", "FICA Verification Checklist")])

FICA_LABELS = {
    "id_copy": "South African ID Copy",
    "proof_of_address": "Proof of Address",
    "bank_statement": "Bank Statement / Bank Confirmation",
    "passport": "Passport Copy",
    "permit_visa": "Permit / Visa",
}

ALLOWED_UPLOADS = {"pdf", "png", "jpg", "jpeg", "webp", "heic", "heif"}
ALLOWED_UPLOAD_MIMES = {"application/pdf", "image/png", "image/jpeg", "image/webp", "image/heic", "image/heif"}


def _upload_folder():
    configured = os.path.abspath(current_app.config["UPLOAD_FOLDER"])
    candidates = [configured, os.path.join(current_app.instance_path, "uploads"), "/tmp/telesales_uploads"]
    last_error = None
    for folder in candidates:
        try:
            os.makedirs(folder, exist_ok=True)
            test_path = os.path.join(folder, ".write_test")
            with open(test_path, "w", encoding="utf-8") as f:
                f.write("ok")
            try:
                os.remove(test_path)
            except Exception:
                pass
            return folder
        except Exception as exc:
            last_error = exc
            try:
                current_app.logger.warning("Upload folder not writable: %s (%s)", folder, exc)
            except Exception:
                pass
    raise RuntimeError(f"No writable upload folder is available: {last_error}")


def _resolve_existing(path):
    if not path:
        return None
    base = os.path.basename(path)
    folder = _upload_folder()
    candidates = [path, os.path.abspath(path), os.path.join(folder, base), os.path.join(current_app.root_path, "static", "uploads", base)]
    for candidate in candidates:
        if candidate and os.path.exists(candidate):
            return os.path.abspath(candidate)
    return None


def _digits(value):
    return "".join(ch for ch in str(value or "") if ch.isdigit())


def _signing_salutation(app_obj):
    title = (getattr(app_obj, "title", "") or "").strip()
    surname = (getattr(app_obj, "surname", "") or getattr(app_obj, "first_names", "") or "Client").strip()
    if title:
        return f"{title} {surname}"
    return surname

def _unlocked_key(app_id):
    return f"sign_unlocked_{app_id}"


def _client_is_sa(app_obj):
    return len(_digits(app_obj.id_number)) == 13


def _is_debit_order(app_obj):
    return "debit" in str(app_obj.payment_method or "").lower()


def _required_fica_types(app_obj):
    return [row["key"] for row in required_fica_documents(app_obj)]


def _fica_status(app_obj):
    application_folder(app_obj)
    required = _required_fica_types(app_obj)
    docs = ClientFicaDocument.query.filter_by(application_id=app_obj.id).all()

    # Treat uploaded FICA documents as received even when the live DB uses
    # slightly different status names from older deployments.  The client
    # final-submit check must not fail after a successful upload just because
    # the status is Approved/Reviewed/Received with different casing.
    good_statuses = {"received", "needs review", "reviewed", "approved"}
    received = set()
    for d in docs:
        doc_type = (getattr(d, "document_type", "") or "").strip()
        status = (getattr(d, "status", "") or "Needs Review").strip().lower()
        file_path = getattr(d, "file_path", None)
        has_file = bool(_resolve_existing(file_path))
        if doc_type and status in good_statuses and has_file:
            received.add(doc_type)

    # Extra safety for Render/live systems: if a file was saved successfully
    # but the DB row was not visible because of an older schema, still mark it
    # as received for the same request/session by checking the upload folder.
    folder = os.path.join(application_folder(app_obj), "fica")
    if not docs and os.path.isdir(folder):
        for filename in os.listdir(folder):
            for doc_type in required:
                if filename.startswith(f"{doc_type}_"):
                    received.add(doc_type)

    outstanding = [t for t in required if t not in received]
    return required, received, outstanding, docs


def _signed_doc_types(app_obj):
    return signed_documents(app_obj)


def _latest_document_signature(app_obj):
    return DocumentSignature.query.filter_by(application_id=app_obj.id).order_by(DocumentSignature.signed_at.desc()).first()


def _save_signature_file(app_obj, doc_type, sig_data):
    if not sig_data.startswith("data:image"):
        raise ValueError("Invalid signature data")
    folder = application_folder(app_obj)
    path = os.path.join(folder, f"signature_{doc_type}_{app_obj.id}_{secrets.token_hex(8)}.png")
    raw = base64.b64decode(sig_data.split(",", 1)[1], validate=True)
    if len(raw) > 2 * 1024 * 1024:
        raise ValueError("Signature is too large")
    with Image.open(io.BytesIO(raw)) as image:
        if image.format != "PNG" or image.width > 4096 or image.height > 4096:
            raise ValueError("Invalid signature image")
        rgba = image.convert("RGBA")
        background = Image.new("RGBA", rgba.size, "white")
        background.alpha_composite(rgba)
        if not ImageChops.difference(background.convert("RGB"), Image.new("RGB", rgba.size, "white")).getbbox():
            raise ValueError("Please draw your signature first.")
    with open(path, "wb") as f:
        f.write(raw)
    store_document(app_obj, path)
    return path


def _extension_from_upload(uploaded_file):
    filename = uploaded_file.filename or ""
    if "." in filename:
        ext = filename.rsplit(".", 1)[1].lower()
        if ext in ALLOWED_UPLOADS:
            return ext
    mime_map = {
        "application/pdf": "pdf",
        "image/png": "png",
        "image/jpeg": "jpg",
        "image/webp": "webp",
        "image/heic": "heic",
        "image/heif": "heif",
    }
    return mime_map.get((uploaded_file.mimetype or "").lower(), "")


def _allowed_file(uploaded_file):
    return _extension_from_upload(uploaded_file) in ALLOWED_UPLOADS or (uploaded_file.mimetype or "").lower() in ALLOWED_UPLOAD_MIMES


def _get_uploaded_file(document_type):
    # The public client page normally posts the file as "file".
    # Some browsers/form changes may send the file under the document type name.
    return (
        request.files.get("file")
        or request.files.get(document_type)
        or request.files.get(f"file_{document_type}")
    )


def _fica_table():
    """Reflect the live FICA table so uploads also work on older Render DB schemas."""
    metadata = MetaData()
    return Table("client_fica_documents", metadata, autoload_with=db.engine)


def _default_for_column(column, app_obj, document_type, safe, path, validation_status="Needs Review", validation_notes=""):
    name = column.name.lower()
    if name in {"id"}:
        return None
    if name in {"application_id", "client_application_id", "app_id"}:
        return app_obj.id
    if name in {"document_type", "doc_type", "type", "fica_type"}:
        return document_type
    if name in {"original_filename", "filename", "file_name", "name"}:
        return safe
    if name in {"file_path", "path", "upload_path", "document_path"}:
        return path
    if name in {"status", "document_status"}:
        return validation_status
    if name in {"validation_notes", "review_notes", "notes", "comment", "comments"}:
        return validation_notes
    if name in {"uploaded_ip", "ip_address", "ip"}:
        return request.remote_addr
    if name in {"user_agent", "browser"}:
        return request.headers.get("User-Agent")
    if name in {"uploaded_at", "created_at", "updated_at", "date_uploaded"}:
        return datetime.utcnow()
    if name in {"uploaded_by", "user_id", "created_by"}:
        return None

    # Safety for old live schemas with extra NOT NULL columns and no defaults.
    if isinstance(column.type, (DateTime,)):
        return datetime.utcnow()
    if isinstance(column.type, (Integer, Numeric, Float)):
        return 0
    if isinstance(column.type, Boolean):
        return False
    return ""


def _insert_fica_document_row(app_obj, document_type, safe, path, validation_status="Needs Review", validation_notes=""):
    table = _fica_table()

    # Mark older documents of this type as replaced where the live schema supports it.
    cols = table.c
    if all(c in cols for c in ("application_id", "document_type", "status")):
        db.session.execute(
            table.update()
            .where(cols.application_id == app_obj.id)
            .where(cols.document_type == document_type)
            .values(status="Replaced")
        )

    values = {}
    for column in table.columns:
        if column.primary_key and column.autoincrement:
            continue
        value = _default_for_column(column, app_obj, document_type, safe, path, validation_status, validation_notes)
        if value is None and (column.nullable or column.default is not None or column.server_default is not None):
            continue
        values[column.name] = value

    result = db.session.execute(table.insert().values(**values))
    inserted_id = None
    try:
        if result.inserted_primary_key:
            inserted_id = result.inserted_primary_key[0]
    except Exception:
        inserted_id = None

    if inserted_id:
        row = db.session.get(ClientFicaDocument, inserted_id)
        if row:
            return row

    # Fallback for databases where primary key is not returned.
    return ClientFicaDocument.query.filter_by(
        application_id=app_obj.id,
        document_type=document_type,
        original_filename=safe,
        file_path=path,
    ).order_by(ClientFicaDocument.uploaded_at.desc()).first()


def _save_upload(app_obj, document_type, uploaded_file):
    if not uploaded_file or not uploaded_file.filename:
        raise ValueError("No file selected. Please choose a PDF, JPG, PNG or WEBP file before clicking Upload / Replace.")
    if not _allowed_file(uploaded_file):
        raise ValueError("Only PDF, JPG, PNG, WEBP, HEIC or HEIF files are allowed. Please do not upload screenshots or unrelated pictures.")

    from app.services.upload_guard import reject_duplicate
    reject_duplicate(app_obj, uploaded_file, document_type)
    ext = _extension_from_upload(uploaded_file) or "bin"
    safe = secure_filename(uploaded_file.filename or f"upload.{ext}")
    if "." not in safe:
        safe = f"{safe}.{ext}"
    folder = os.path.join(application_folder(app_obj), "fica")
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, f"{document_type}_{datetime.utcnow().strftime('%Y%m%d%H%M%S%f')}_{safe}")
    uploaded_file.save(path)
    store_document(app_obj, path)

    if not os.path.exists(path) or os.path.getsize(path) == 0:
        raise ValueError("The uploaded file was empty or could not be saved. Please try again. If this was taken with a phone camera, try saving it as JPG or PDF first.")
    if os.path.getsize(path) > 25 * 1024 * 1024:
        try:
            os.remove(path)
        except Exception:
            pass
        raise ValueError("The uploaded file is too large. Please upload a smaller file under 25 MB.")

    try:
        validation_status, validation_notes = validate_fica_upload(path, safe, document_type, app_obj)
    except Exception as exc:
        current_app.logger.exception("FICA validation failed after upload; keeping document for manual review")
        validation_status = "Needs Review"
        validation_notes = f"File uploaded but automatic validation failed: {exc}. Staff must review manually."

    try:
        row = _insert_fica_document_row(app_obj, document_type, safe, path, validation_status, validation_notes)
        if row:
            row.status = validation_status
        from app.models import AuditLog
        db.session.add(AuditLog(action="FICA Uploaded", entity_type="ClientApplication", entity_id=str(app_obj.id), details=f"{document_type}: {safe} uploaded by client; status {validation_status}. Previous versions retained in history."))
        return row
    except Exception:
        current_app.logger.exception("Dynamic FICA insert failed for application %s", app_obj.id)
        raise


def _generate_review_docs(app_obj):
    folder = application_folder(app_obj)
    app_obj.popia_pdf_path = os.path.join(folder, f"popia_consent_{app_obj.id}.pdf")
    app_obj.disclosure_pdf_path = os.path.join(folder, f"policy_disclosure_{app_obj.id}.pdf")
    app_obj.welcome_pack_path = os.path.join(folder, f"welcome_pack_{app_obj.id}.pdf")
    generate_popia_pdf(app_obj, app_obj.popia_pdf_path)
    generate_disclosure_pdf(app_obj, app_obj.disclosure_pdf_path)
    generate_welcome_pack(app_obj, app_obj.welcome_pack_path)
    generate_fica_pdf(app_obj, os.path.join(folder, f"fica_verification_{app_obj.id}.pdf"))


@signing_bp.route("/<token>/upload-fica", methods=["POST"])
def upload_fica_document(token):
    app_obj = ClientApplication.query.filter_by(sign_token=token).first_or_404()
    if app_obj.sign_token_revoked or app_obj.sign_token_used_at:
        flash("This signing link has already been completed and cannot accept more uploads.", "danger")
        return redirect(url_for("signing.sign_application", token=token))
    if not session.get(_unlocked_key(app_obj.id)):
        flash("Please unlock the secure page with the client ID number before uploading documents.", "danger")
        return redirect(url_for("signing.sign_application", token=token))

    try:
        doc_type = request.form.get("document_type")
        if doc_type not in FICA_LABELS:
            raise ValueError("Invalid document type")
        row = _save_upload(app_obj, doc_type, _get_uploaded_file(doc_type))
        generate_fica_pdf(app_obj, os.path.join(application_folder(app_obj), f"fica_verification_{app_obj.id}.pdf"))
        required, received, outstanding, docs = _fica_status(app_obj)
        if outstanding:
            app_obj.status = "FICA Outstanding"
        elif app_obj.status in ("FICA Outstanding", "Signature Sent", "Draft", None):
            app_obj.status = "FICA Review"
        db.session.commit()
        flash(f"{FICA_LABELS.get(doc_type, doc_type)} uploaded successfully: {getattr(row, 'original_filename', None) or 'file received'}", "success")
    except Exception as e:
        db.session.rollback()
        current_app.logger.exception("Client FICA upload failed for application %s", app_obj.id)
        flash(str(e), "danger")
    return redirect(url_for("signing.sign_application", token=token))


@signing_bp.route("/<token>", methods=["GET", "POST"])
def sign_application(token):
    app_obj = ClientApplication.query.filter_by(sign_token=token).first_or_404()
    if app_obj.sign_token_revoked or app_obj.sign_token_used_at:
        return render_template("sign/complete.html", app=app_obj, message="This signing link has already been used and is now deactivated.")

    if request.method == "POST" and request.form.get("action") == "unlock":
        entered = _digits(request.form.get("id_number"))
        expected = _digits(app_obj.id_number)
        if not entered or entered != expected:
            return render_template("sign/unlock.html", app=app_obj, token=token, error="The ID number entered does not match this application.")
        session[_unlocked_key(app_obj.id)] = True
        return redirect(url_for("signing.sign_application", token=token))

    if not session.get(_unlocked_key(app_obj.id)):
        return render_template("sign/unlock.html", app=app_obj, token=token)

    # Campaign/WhatsApp applications use their questionnaire page as the
    # document dashboard, but their document signature forms still post here.
    # Redirect only ordinary page loads; allowing POST keeps the highlighted
    # in-document signature workflow available on mobile.
    if app_obj.whatsapp_journey and request.method == "GET":
        return redirect(url_for("online_application.form", token=token))

    if request.method == "POST":
        action = request.form.get("action")
        try:
            if action == "upload_fica":
                doc_type = request.form.get("document_type")
                if doc_type not in FICA_LABELS:
                    raise ValueError("Invalid document type")
                row = _save_upload(app_obj, doc_type, _get_uploaded_file(doc_type))
                generate_fica_pdf(app_obj, os.path.join(application_folder(app_obj), f"fica_verification_{app_obj.id}.pdf"))
                required, received, outstanding, docs = _fica_status(app_obj)
                app_obj.status = "FICA Outstanding" if outstanding else "FICA Review"
                db.session.commit()
                flash(f"{FICA_LABELS.get(doc_type, doc_type)} uploaded successfully: {getattr(row, 'original_filename', None) or 'file received'}", "success")
                return redirect(url_for("signing.sign_application", token=token))

            if action == "save_cdd":
                expected=session.get(f"document_review_{app_obj.id}_cdd")
                if not expected or not secrets.compare_digest(expected,request.form.get("review_nonce", "")):
                    raise ValueError("Open Annexure J.1 before completing its questions.")
                save_answers(app_obj,request.form)
                _signable_pdf(app_obj,"cdd")
                db.session.commit()
                return redirect(url_for("signing.edit_document",token=token,doc_type="cdd"))

            if action == "save_marketing_consent":
                expected=session.get(f"document_review_{app_obj.id}_popia")
                if not expected or not secrets.compare_digest(expected, request.form.get("review_nonce", "")):
                    raise ValueError("Open the POPIA document before selecting consent.")
                choice=request.form.get("marketing_choice")
                if choice not in {"yes", "no"}:
                    raise ValueError("Please choose Yes or No for marketing consent.")
                previous=consent_value(app_obj)
                apply_consent(app_obj, choice=="yes", request.remote_addr, request.headers.get("User-Agent"))
                if previous != (choice=="yes"):
                    DocumentSignature.query.filter_by(application_id=app_obj.id,document_type="popia").delete(synchronize_session=False)
                db.session.flush()
                _signable_pdf(app_obj,"popia")
                db.session.commit()
                return redirect(url_for("signing.edit_document",token=token,doc_type="popia"))

            if action == "sign_document":
                doc_type = request.form.get("document_type")
                if doc_type not in dict(REQUIRED_SIGNATURE_DOCS):
                    raise ValueError("Invalid document type")
                expected_nonce = session.get(f"document_review_{app_obj.id}_{doc_type}")
                if not expected_nonce or not secrets.compare_digest(expected_nonce, request.form.get("review_nonce", "")):
                    raise ValueError("Open the document and sign in its signature space first.")
                field_key=request.form.get("signature_field", doc_type)
                allowed={f['key'] for f in application_fields(app_obj)} if doc_type=="application" else {doc_type}
                if field_key not in allowed:
                    raise ValueError("Choose the specific signature space inside this document.")
                if doc_type=="popia" and consent_value(app_obj) is None:
                    raise ValueError("Please save Yes or No for marketing consent before signing POPIA.")
                if doc_type=="cdd" and not cdd_completed(app_obj):
                    raise ValueError("Complete and save Annexure J.1 before signing.")
                typed_name = request.form.get("typed_name", "").strip()
                sig_data = request.form.get("signature_data", "")
                if not typed_name:
                    raise ValueError("Please type your full name before signing.")
                sig_path = _save_signature_file(app_obj, field_key.replace(":", "_"), sig_data)
                existing = DocumentSignature.query.filter_by(application_id=app_obj.id, document_type=field_key).first()
                if existing:
                    existing.typed_name = typed_name
                    existing.signature_image_path = sig_path
                    existing.ip_address = request.remote_addr
                    existing.user_agent = request.headers.get("User-Agent")
                    existing.signed_at = datetime.utcnow()
                else:
                    db.session.add(DocumentSignature(application_id=app_obj.id, document_type=field_key, typed_name=typed_name, signature_image_path=sig_path, ip_address=request.remote_addr, user_agent=request.headers.get("User-Agent")))
                db.session.flush()
                _signable_pdf(app_obj, doc_type)
                db.session.commit()
                session.pop(f"document_review_{app_obj.id}_{doc_type}", None)
                # Return to the document after each field so the client can see
                # the saved signature in place before scrolling to the next pad.
                if doc_type in _signed_doc_types(app_obj):
                    # WhatsApp/browser questionnaire applications choose their
                    # next document from the online application page.
                    if app_obj.whatsapp_journey:
                        return redirect(url_for("online_application.form", token=token))
                    return redirect(url_for("signing.sign_application", token=token))
                return redirect(url_for("signing.edit_document", token=token, doc_type=doc_type, saved=field_key))

            if action == "final_submit":
                return finish_application(app_obj, token)

        except Exception as e:
            db.session.rollback()
            current_app.logger.exception("Client signing/FICA action failed for application %s", app_obj.id)
            required, received, outstanding, docs = _fica_status(app_obj)
            return render_template("sign/sign.html", app=app_obj, token=token, error=str(e), required_docs=required, received_docs=received, outstanding_docs=outstanding, fica_docs=docs, fica_labels=FICA_LABELS, sign_docs=REQUIRED_SIGNATURE_DOCS, signed_docs=_signed_doc_types(app_obj), doc_labels=DOC_LABELS)

    _generate_review_docs(app_obj)
    db.session.commit()
    required, received, outstanding, docs = _fica_status(app_obj)
    return render_template("sign/sign.html", app=app_obj, token=token, required_docs=required, received_docs=received, outstanding_docs=outstanding, fica_docs=docs, fica_labels=FICA_LABELS, sign_docs=REQUIRED_SIGNATURE_DOCS, signed_docs=_signed_doc_types(app_obj), doc_labels=DOC_LABELS)


def _signable_pdf(app_obj, doc_type):
    generators = {"application": ("signed_application", generate_application_pdf),
                  "popia": ("popia_consent", generate_popia_pdf),
                  "disclosure": ("policy_disclosure", generate_disclosure_pdf),
                  "welcome": ("welcome_pack", generate_welcome_pack),
                  "cdd": ("annexure_j1", generate_cdd_pdf)}
    prefix, generator = generators[doc_type]
    path = os.path.join(application_folder(app_obj), f"{prefix}_{app_obj.id}.pdf")
    generator(app_obj, path)
    if doc_type=="cdd":
        return path
    field = {"application": "signed_pdf_path", "popia": "popia_pdf_path", "disclosure": "disclosure_pdf_path", "welcome": "welcome_pack_path"}[doc_type]
    if doc_type != "application" or doc_type in _signed_doc_types(app_obj):
        setattr(app_obj, field, path)
    return path


@signing_bp.route("/<token>/review/<doc_type>")
def edit_document(token, doc_type):
    app_obj = ClientApplication.query.filter_by(sign_token=token).first_or_404()
    if app_obj.sign_token_revoked or app_obj.sign_token_used_at:
        abort(404)
    if not session.get(_unlocked_key(app_obj.id)):
        return redirect(url_for("signing.sign_application", token=token))
    if doc_type not in dict(REQUIRED_SIGNATURE_DOCS):
        abort(404)
    path = _signable_pdf(app_obj, doc_type)
    subject = PdfReader(path).metadata.get('/Subject', '')
    target = json.loads(subject.removeprefix('martins-signature:'))
    if doc_type=="application":
        targets=target['fields']
    else:
        targets=[dict(target,key=doc_type,label=DOC_LABELS[doc_type]+" signature",signed=doc_type in _signed_doc_types(app_obj))]
    db.session.commit()
    nonce = secrets.token_urlsafe(24)
    session[f"document_review_{app_obj.id}_{doc_type}"] = nonce
    response = current_app.make_response(render_template("sign/document.html", app=app_obj,
        token=token, doc_type=doc_type, label=DOC_LABELS[doc_type], targets=targets, marketing_consent=consent_value(app_obj), cdd_fields=CDD_FIELDS, cdd_answers=answers_for(app_obj) if doc_type=='cdd' else {}, cdd_complete=cdd_completed(app_obj),
        review_nonce=nonce, signed=doc_type in _signed_doc_types(app_obj), saved_field=request.args.get("saved", "")))
    response.headers['Cache-Control'] = 'no-store'
    response.headers['Referrer-Policy'] = 'no-referrer'
    return response


@signing_bp.route("/<token>/document/<doc_type>")
def view_sign_document(token, doc_type):
    app_obj = ClientApplication.query.filter_by(sign_token=token).first_or_404()
    if app_obj.sign_token_revoked or app_obj.sign_token_used_at:
        abort(404)
    if not session.get(_unlocked_key(app_obj.id)):
        abort(403)

    folder = application_folder(app_obj)
    if doc_type in dict(REQUIRED_SIGNATURE_DOCS):
        path = _signable_pdf(app_obj, doc_type)
    elif doc_type == "fica":
        path = os.path.join(folder, f"fica_verification_{app_obj.id}.pdf")
        generate_fica_pdf(app_obj, path)
    elif doc_type == "signed_application":
        path = _resolve_existing(app_obj.signed_pdf_path)
    else:
        abort(404)
    if not path or not os.path.exists(path):
        abort(404)
    db.session.commit()
    response = send_file(path, as_attachment=False, max_age=0)
    response.headers["Cache-Control"] = "no-store"
    return response


@signing_bp.route("/<token>/fica-upload/<int:doc_id>")
def download_fica_upload(token, doc_id):
    app_obj = ClientApplication.query.filter_by(sign_token=token).first_or_404()
    if not session.get(_unlocked_key(app_obj.id)):
        abort(403)
    application_folder(app_obj)
    doc = ClientFicaDocument.query.filter_by(id=doc_id, application_id=app_obj.id).first_or_404()
    path = _resolve_existing(doc.file_path)
    if not path:
        abort(404)
    return send_file(path, as_attachment=False)


@signing_bp.route("/<token>/supporting-documents", methods=["GET", "POST"])
def supporting_documents(token):
    """Post-signing portal for member identity files and proof of address."""
    app_obj = ClientApplication.query.filter_by(sign_token=token).first_or_404()
    requirements = required_fica_documents(app_obj)
    allowed = {row["key"]: row["label"] for row in requirements}
    error = None

    if request.method == "POST" and request.form.get("action") == "unlock":
        entered = _digits(request.form.get("id_number"))
        if not entered or entered != _digits(app_obj.id_number):
            error = "The ID number entered does not match this application."
        else:
            session[_unlocked_key(app_obj.id)] = True
            return redirect(url_for("signing.supporting_documents", token=token))

    if not session.get(_unlocked_key(app_obj.id)):
        return render_template("sign/supporting_unlock.html", app=app_obj, token=token, error=error)

    if request.method == "POST":
        try:
            action = request.form.get("action")
            if action == "upload":
                doc_type = request.form.get("document_type")
                if doc_type not in allowed:
                    raise ValueError("Invalid document type")
                row = _save_upload(app_obj, doc_type, _get_uploaded_file(doc_type))
                required, received, outstanding, docs = _fica_status(app_obj)
                app_obj.status = "FICA Outstanding" if outstanding else "FICA Review"
                from app.models import AuditLog
                db.session.add(AuditLog(action="Supporting document submitted", entity_type="ClientApplication",
                                        entity_id=str(app_obj.id), details=f"Client uploaded {allowed[doc_type]} for staff review."))
                db.session.commit()
                flash(f"{allowed[doc_type]} uploaded successfully: {getattr(row, 'original_filename', None) or 'file received'}", "success")
                return redirect(url_for("signing.supporting_documents", token=token))
            if action == "complete":
                required, received, outstanding, docs = _fica_status(app_obj)
                if outstanding:
                    raise ValueError("Please upload every required document before submitting: " +
                                     ", ".join(allowed[key] for key in outstanding))
                app_obj.status = "FICA Review"
                from app.models import AuditLog
                db.session.add(AuditLog(action="Supporting documents completed", entity_type="ClientApplication",
                                        entity_id=str(app_obj.id), details="Client submitted all member identity documents and proof of address for staff review."))
                db.session.commit()
                session.pop(_unlocked_key(app_obj.id), None)
                return render_template("sign/supporting_complete.html", app=app_obj)
            raise ValueError("Choose a valid action.")
        except Exception as exc:
            db.session.rollback()
            current_app.logger.exception("Supporting document upload failed for application %s", app_obj.id)
            error = str(exc)

    required, received, outstanding, docs = _fica_status(app_obj)
    latest = {}
    for doc in sorted(docs, key=lambda item: item.uploaded_at or datetime.min, reverse=True):
        if doc.status != "Replaced":
            latest.setdefault(doc.document_type, doc)
    return render_template("sign/supporting_documents.html", app=app_obj, token=token, requirements=requirements,
                           received=received, outstanding=outstanding, latest=latest, error=error)


def finish_application(app_obj, token):
    from app.services.delivery_preferences import receipt_address
    recipient = receipt_address(request.form, app_obj)
    ok, errors = assert_application_rules(app_obj)
    if not ok:
        raise ValueError("Application blocked: " + "; ".join(errors))
    signed = _signed_doc_types(app_obj)
    missing_sigs = [label for key, label in REQUIRED_SIGNATURE_DOCS if key not in signed]
    required, received, outstanding, docs = _fica_status(app_obj)
    if missing_sigs:
        raise ValueError("Please sign these documents first: " + ", ".join(missing_sigs))
    signed_records = {row.document_type: row for row in DocumentSignature.query.filter_by(application_id=app_obj.id).all()}
    sig = signed_records.get("application:principal")
    sig_path = sig.signature_image_path if sig else None
    folder = application_folder(app_obj)
    signed_pdf = os.path.join(folder, f"signed_application_{app_obj.id}.pdf")
    welcome_pdf = os.path.join(folder, f"welcome_pack_{app_obj.id}.pdf")
    popia_pdf = os.path.join(folder, f"popia_consent_{app_obj.id}.pdf")
    disclosure_pdf = os.path.join(folder, f"policy_disclosure_{app_obj.id}.pdf")
    # Each document is rendered immediately after its last signature. Reuse
    # those completed PDFs here instead of regenerating the whole bundle during
    # the final click; only recover a missing file.
    if not _resolve_existing(signed_pdf):
        generate_application_pdf(app_obj, signed_pdf, signature_path_override=sig_path)
    if not _resolve_existing(welcome_pdf):
        generate_welcome_pack(app_obj, welcome_pdf, signature_path_override=signed_records["welcome"].signature_image_path)
    if not _resolve_existing(popia_pdf):
        generate_popia_pdf(app_obj, popia_pdf, signature_path_override=signed_records["popia"].signature_image_path)
    if not _resolve_existing(disclosure_pdf):
        generate_disclosure_pdf(app_obj, disclosure_pdf, signature_path_override=signed_records["disclosure"].signature_image_path)
    cdd_pdf=os.path.join(folder,f"annexure_j1_{app_obj.id}.pdf")
    if not _resolve_existing(cdd_pdf):
        generate_cdd_pdf(app_obj,cdd_pdf)

    if recipient != (app_obj.document_email or ''):
        from app.models import AuditLog
        db.session.add(AuditLog(action="Document email selected", entity_type="ClientApplication", entity_id=str(app_obj.id), details="ID-verified client selected the signed-document email recipient."))
    app_obj.document_email = recipient
    app_obj.status = "Signed"
    app_obj.signed_at = datetime.utcnow()
    app_obj.sign_token_used_at = datetime.utcnow()
    app_obj.sign_token_revoked = True
    app_obj.signed_pdf_path = signed_pdf
    app_obj.welcome_pack_path = welcome_pdf
    app_obj.popia_pdf_path = popia_pdf
    app_obj.disclosure_pdf_path = disclosure_pdf

    if sig:
        db.session.add(ApplicationSignature(
            application_id=app_obj.id,
            typed_name=sig.typed_name,
            otp_verified=False,
            signature_image_path=sig_path,
            ip_address=request.remote_addr,
            user_agent=request.headers.get("User-Agent"),
            consent_popia=True,
            consent_disclosure=True,
            consent_marketing=consent_value(app_obj) is True,
            signed_at=datetime.utcnow(),
        ))
    db.session.commit()
    session.pop(_unlocked_key(app_obj.id), None)
    if recipient:
        from app.services.email_service import client_email_content, signing_email_html
        subject, body = client_email_content("receipt", app_obj)
        upload_link = current_app.config['BASE_URL'].rstrip('/') + url_for('signing.supporting_documents', token=token)
        body += ("\n\nPlease use this secure link to upload the South African ID documents for every member on the policy "
                 "and the proof of address:\n\n" + upload_link +
                 "\n\nYou will need the principal member ID number to unlock the secure upload page.")
        send_email(recipient, subject, body, [signed_pdf, welcome_pdf, popia_pdf, disclosure_pdf, cdd_pdf],
                   html_body=signing_email_html(app_obj, upload_link, body, "Secure supporting document upload"),
                   application_id=app_obj.id)
    office_email = os.getenv("MAIL_DOCUMENTS_TO")
    from email.utils import parseaddr
    if office_email and parseaddr(office_email)[1].strip().casefold()!=parseaddr(recipient or '')[1].strip().casefold():
        app_link = current_app.config['BASE_URL'].rstrip('/') + url_for('client_files.index', application_id=app_obj.id)
        from app.services.email_service import signing_email_html
        office_body = "The client has submitted the signed application."
        if outstanding:
            office_body += "\n\nThe following supporting documents will be uploaded through the client's secure follow-up link: " + ", ".join(fica_label(app_obj, t) for t in outstanding) + "."
        else:
            office_body += " Supporting documents were uploaded with the application."
        office_body += "\n\nOpen the client file (staff login required):\n\n" + app_link
        send_email(office_email, "Signed documents received: " + app_obj.application_ref,
                   office_body, html_body=signing_email_html(app_obj,app_link,office_body), application_id=app_obj.id)
    db.session.commit()
    return render_template("sign/complete.html", app=app_obj)

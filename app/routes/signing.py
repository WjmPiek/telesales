import os, base64
from datetime import datetime
from flask import Blueprint, render_template, request, current_app, abort, send_file, session, redirect, url_for
from werkzeug.utils import secure_filename
from app import db
from app.models import ClientApplication, ApplicationSignature, ClientFicaDocument, DocumentSignature
from app.services.pdf_service import generate_application_pdf, generate_welcome_pack, generate_popia_pdf, generate_disclosure_pdf, generate_fica_pdf
from app.services.email_service import send_email
from app.services.compliance_service import assert_application_rules

signing_bp = Blueprint("signing", __name__, url_prefix="/sign")

REQUIRED_SIGNATURE_DOCS = [
    ("application", "Application Form"),
    ("popia", "POPIA Consent"),
    ("disclosure", "Policy Disclosure"),
    ("welcome", "Welcome Pack Acknowledgement"),
]

DOC_LABELS = dict(REQUIRED_SIGNATURE_DOCS + [("fica", "FICA Verification Checklist")])

FICA_LABELS = {
    "id_copy": "South African ID Copy",
    "proof_of_address": "Proof of Address",
    "bank_statement": "Bank Statement / Bank Confirmation",
    "passport": "Passport Copy",
    "permit_visa": "Permit / Visa",
}

ALLOWED_UPLOADS = {"pdf", "png", "jpg", "jpeg", "webp"}


def _upload_folder():
    folder = os.path.abspath(current_app.config["UPLOAD_FOLDER"])
    os.makedirs(folder, exist_ok=True)
    return folder


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
    required = ["id_copy" if _client_is_sa(app_obj) else "passport", "proof_of_address"]
    if not _client_is_sa(app_obj):
        required.append("permit_visa")
    if _is_debit_order(app_obj):
        required.append("bank_statement")
    return required


def _fica_status(app_obj):
    required = _required_fica_types(app_obj)
    docs = ClientFicaDocument.query.filter_by(application_id=app_obj.id).all()
    received = {d.document_type for d in docs if d.status != "Rejected"}
    outstanding = [t for t in required if t not in received]
    return required, received, outstanding, docs


def _signed_doc_types(app_obj):
    rows = DocumentSignature.query.filter_by(application_id=app_obj.id).all()
    return {r.document_type for r in rows}


def _latest_document_signature(app_obj):
    return DocumentSignature.query.filter_by(application_id=app_obj.id).order_by(DocumentSignature.signed_at.desc()).first()


def _save_signature_file(app_obj, doc_type, sig_data):
    if not sig_data.startswith("data:image"):
        raise ValueError("Invalid signature data")
    folder = _upload_folder()
    path = os.path.join(folder, f"signature_{doc_type}_{app_obj.id}.png")
    raw = sig_data.split(",", 1)[1]
    with open(path, "wb") as f:
        f.write(base64.b64decode(raw))
    return path


def _allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_UPLOADS


def _save_upload(app_obj, document_type, uploaded_file):
    if not uploaded_file or not uploaded_file.filename:
        raise ValueError("No file selected")
    if not _allowed_file(uploaded_file.filename):
        raise ValueError("Only PDF, JPG, PNG or WEBP files are allowed")
    safe = secure_filename(uploaded_file.filename)
    folder = os.path.join(_upload_folder(), f"fica_app_{app_obj.id}")
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, f"{document_type}_{datetime.utcnow().strftime('%Y%m%d%H%M%S')}_{safe}")
    uploaded_file.save(path)
    row = ClientFicaDocument(
        application_id=app_obj.id,
        document_type=document_type,
        original_filename=safe,
        file_path=path,
        status="Received",
        uploaded_ip=request.remote_addr,
        user_agent=request.headers.get("User-Agent"),
    )
    db.session.add(row)
    return row


def _generate_review_docs(app_obj):
    folder = _upload_folder()
    app_obj.popia_pdf_path = os.path.join(folder, f"popia_consent_{app_obj.id}.pdf")
    app_obj.disclosure_pdf_path = os.path.join(folder, f"policy_disclosure_{app_obj.id}.pdf")
    app_obj.welcome_pack_path = os.path.join(folder, f"welcome_pack_{app_obj.id}.pdf")
    generate_popia_pdf(app_obj, app_obj.popia_pdf_path)
    generate_disclosure_pdf(app_obj, app_obj.disclosure_pdf_path)
    generate_welcome_pack(app_obj, app_obj.welcome_pack_path)
    generate_fica_pdf(app_obj, os.path.join(folder, f"fica_verification_{app_obj.id}.pdf"))


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

    if request.method == "POST":
        action = request.form.get("action")
        try:
            if action == "upload_fica":
                doc_type = request.form.get("document_type")
                if doc_type not in FICA_LABELS:
                    raise ValueError("Invalid document type")
                _save_upload(app_obj, doc_type, request.files.get("file"))
                generate_fica_pdf(app_obj, os.path.join(_upload_folder(), f"fica_verification_{app_obj.id}.pdf"))
                db.session.commit()
                return redirect(url_for("signing.sign_application", token=token))

            if action == "sign_document":
                doc_type = request.form.get("document_type")
                if doc_type not in DOC_LABELS:
                    raise ValueError("Invalid document type")
                typed_name = request.form.get("typed_name", "").strip()
                sig_data = request.form.get("signature_data", "")
                if not typed_name:
                    raise ValueError("Please type your full name before signing.")
                sig_path = _save_signature_file(app_obj, doc_type, sig_data)
                existing = DocumentSignature.query.filter_by(application_id=app_obj.id, document_type=doc_type).first()
                if existing:
                    existing.typed_name = typed_name
                    existing.signature_image_path = sig_path
                    existing.ip_address = request.remote_addr
                    existing.user_agent = request.headers.get("User-Agent")
                    existing.signed_at = datetime.utcnow()
                else:
                    db.session.add(DocumentSignature(application_id=app_obj.id, document_type=doc_type, typed_name=typed_name, signature_image_path=sig_path, ip_address=request.remote_addr, user_agent=request.headers.get("User-Agent")))
                db.session.commit()
                return redirect(url_for("signing.sign_application", token=token))

            if action == "final_submit":
                ok, errors = assert_application_rules(app_obj)
                if not ok:
                    raise ValueError("Application blocked: " + "; ".join(errors))
                signed = _signed_doc_types(app_obj)
                missing_sigs = [label for key, label in REQUIRED_SIGNATURE_DOCS if key not in signed]
                required, received, outstanding, docs = _fica_status(app_obj)
                if missing_sigs:
                    raise ValueError("Please sign these documents first: " + ", ".join(missing_sigs))
                if outstanding:
                    raise ValueError("Please upload outstanding FICA documents: " + ", ".join(FICA_LABELS.get(t, t) for t in outstanding))

                sig = _latest_document_signature(app_obj)
                sig_path = sig.signature_image_path if sig else None
                folder = _upload_folder()
                signed_pdf = os.path.join(folder, f"signed_application_{app_obj.id}.pdf")
                welcome_pdf = os.path.join(folder, f"welcome_pack_{app_obj.id}.pdf")
                popia_pdf = os.path.join(folder, f"popia_consent_{app_obj.id}.pdf")
                disclosure_pdf = os.path.join(folder, f"policy_disclosure_{app_obj.id}.pdf")
                fica_pdf = os.path.join(folder, f"fica_verification_{app_obj.id}.pdf")
                generate_application_pdf(app_obj, signed_pdf, signature_path_override=sig_path)
                generate_welcome_pack(app_obj, welcome_pdf, signature_path_override=sig_path)
                generate_popia_pdf(app_obj, popia_pdf, signature_path_override=sig_path)
                generate_disclosure_pdf(app_obj, disclosure_pdf, signature_path_override=sig_path)
                generate_fica_pdf(app_obj, fica_pdf, signature_path_override=sig_path)

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
                        otp_verified=True,
                        signature_image_path=sig_path,
                        ip_address=request.remote_addr,
                        user_agent=request.headers.get("User-Agent"),
                        consent_popia=True,
                        consent_disclosure=True,
                        consent_fica=True,
                        consent_marketing=bool(request.form.get("consent_marketing")),
                        signed_at=datetime.utcnow(),
                    ))
                db.session.commit()
                session.pop(_unlocked_key(app_obj.id), None)
                if app_obj.email:
                    body = (
                        f"Dear {_signing_salutation(app_obj)},\n\n"
                        "Your signed documents have been received and submitted to Martin's Funerals.\n\n"
                        "No documents are attached to this email. The signed documents are stored securely on the Martin's Funerals system."
                    )
                    send_email(app_obj.email, "Martin's Funerals signed documents received", body, [])
                return render_template("sign/complete.html", app=app_obj)
        except Exception as e:
            db.session.rollback()
            required, received, outstanding, docs = _fica_status(app_obj)
            return render_template("sign/sign.html", app=app_obj, token=token, error=str(e), required_docs=required, received_docs=received, outstanding_docs=outstanding, fica_docs=docs, fica_labels=FICA_LABELS, sign_docs=REQUIRED_SIGNATURE_DOCS, signed_docs=_signed_doc_types(app_obj), doc_labels=DOC_LABELS)

    _generate_review_docs(app_obj)
    db.session.commit()
    required, received, outstanding, docs = _fica_status(app_obj)
    return render_template("sign/sign.html", app=app_obj, token=token, required_docs=required, received_docs=received, outstanding_docs=outstanding, fica_docs=docs, fica_labels=FICA_LABELS, sign_docs=REQUIRED_SIGNATURE_DOCS, signed_docs=_signed_doc_types(app_obj), doc_labels=DOC_LABELS)


@signing_bp.route("/<token>/document/<doc_type>")
def view_sign_document(token, doc_type):
    app_obj = ClientApplication.query.filter_by(sign_token=token).first_or_404()
    if app_obj.sign_token_revoked and doc_type != "signed_application":
        abort(404)
    if not session.get(_unlocked_key(app_obj.id)) and doc_type != "signed_application":
        abort(403)

    folder = _upload_folder()
    if doc_type == "application":
        path = os.path.join(folder, f"review_application_{app_obj.id}.pdf")
        generate_application_pdf(app_obj, path)
    elif doc_type == "popia":
        path = os.path.join(folder, f"popia_consent_{app_obj.id}.pdf")
        generate_popia_pdf(app_obj, path)
        app_obj.popia_pdf_path = path
        db.session.commit()
    elif doc_type == "disclosure":
        path = os.path.join(folder, f"policy_disclosure_{app_obj.id}.pdf")
        generate_disclosure_pdf(app_obj, path)
        app_obj.disclosure_pdf_path = path
        db.session.commit()
    elif doc_type == "welcome":
        path = os.path.join(folder, f"welcome_pack_{app_obj.id}.pdf")
        generate_welcome_pack(app_obj, path)
        app_obj.welcome_pack_path = path
        db.session.commit()
    elif doc_type == "fica":
        path = os.path.join(folder, f"fica_verification_{app_obj.id}.pdf")
        generate_fica_pdf(app_obj, path)
    elif doc_type == "signed_application":
        path = _resolve_existing(app_obj.signed_pdf_path)
    else:
        abort(404)
    if not path or not os.path.exists(path):
        abort(404)
    return send_file(path, as_attachment=False)


@signing_bp.route("/<token>/fica-upload/<int:doc_id>")
def download_fica_upload(token, doc_id):
    app_obj = ClientApplication.query.filter_by(sign_token=token).first_or_404()
    if not session.get(_unlocked_key(app_obj.id)):
        abort(403)
    doc = ClientFicaDocument.query.filter_by(id=doc_id, application_id=app_obj.id).first_or_404()
    path = _resolve_existing(doc.file_path)
    if not path:
        abort(404)
    return send_file(path, as_attachment=False)

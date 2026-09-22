import os
import smtplib
import logging
import ssl
import mimetypes
from email.message import EmailMessage
from email.utils import parseaddr


def business_bank_confirmation_attachment():
    """Materialise the admin-approved cash-payment letter for one email send."""
    import base64
    import json
    import tempfile
    from pathlib import Path
    from app.models import BankConfirmationLetter, SystemSetting

    current = BankConfirmationLetter.query.filter_by(active=True).order_by(
        BankConfirmationLetter.uploaded_at.desc(), BankConfirmationLetter.id.desc()
    ).first()
    if current and current.file_data and current.file_data.startswith(b"%PDF"):
        folder = Path(tempfile.mkdtemp(prefix="martins_bank_letter_"))
        path = folder / "martins_business_bank_confirmation_letter.pdf"
        path.write_bytes(current.file_data)
        return str(path)

    # Backward-compatible fallback for the temporary single-file setting used
    # before versioned bank-letter administration was introduced.
    row = SystemSetting.query.filter_by(
        category="Email", key="business_bank_confirmation_pdf", active=True
    ).first()
    if not row or not row.value:
        return None
    try:
        payload = json.loads(row.value)
        content = base64.b64decode(payload.get("content", ""), validate=True)
    except (ValueError, TypeError, json.JSONDecodeError):
        return None
    if not content.startswith(b"%PDF"):
        return None
    folder = Path(tempfile.mkdtemp(prefix="martins_bank_letter_"))
    path = folder / "martins_business_bank_confirmation_letter.pdf"
    path.write_bytes(content)
    return str(path)


def send_email(to_email, subject, body, attachments=None, html_body=None, application_id=None, policy_id=None):
    from app.services.branding import display_brand
    subject, body = display_brand(subject), display_brand(body)
    if html_body: html_body = display_brand(html_body)
    def outcome(ok):
        if application_id or policy_id:
            from app.services.conversation_history import record_communication
            record_communication('Email',body,'Accepted by mail server' if ok else 'Failed',application_id=application_id,policy_id=policy_id,subject=subject,attachments=attachments)
        return ok
    host = os.getenv("SMTP_HOST", "smtp.gmail.com").strip()
    # A different mail provider must have its own credentials; never send Gmail credentials to it.
    legacy_gmail = host.lower() == "smtp.gmail.com"
    user = os.getenv("SMTP_USERNAME") or os.getenv("SMTP_USER") or (os.getenv("GMAIL_SMTP_USER") if legacy_gmail else None)
    password = os.getenv("SMTP_PASSWORD") or (os.getenv("GMAIL_SMTP_PASSWORD") if legacy_gmail else None)
    mail_from = os.getenv("MAIL_FROM", user or "no-reply@example.com")
    if not to_email or not user or not password:
        logging.getLogger(__name__).warning("Email not sent: SMTP credentials are not configured")
        return outcome(False)

    if application_id and not html_body:
        from app.models import ClientApplication
        import re
        application = ClientApplication.query.get(application_id)
        links = re.findall(r'https://[^\s<>]+', body)
        if application and links:
            html_body = signing_email_html(application, links[0], body)
    msg = EmailMessage()
    msg["From"] = mail_from
    msg["To"] = to_email
    msg["Subject"] = subject
    msg["Reply-To"] = os.getenv("MAIL_REPLY_TO") or parseaddr(mail_from)[1]
    msg.set_content(body)
    if html_body:
        msg.add_alternative(html_body, subtype="html")

    for path in attachments or []:
        if not path or not os.path.exists(path):
            continue
        with open(path, "rb") as f:
            data = f.read()
        filename = os.path.basename(path)
        mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        maintype, subtype = mime.split("/", 1)
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)

    try:
        security = os.getenv("SMTP_SECURITY", "ssl").strip().lower()
        if security not in {"ssl", "starttls"}:
            raise ValueError("SMTP_SECURITY must be ssl or starttls")
        port = int(os.getenv("SMTP_PORT") or (465 if security == "ssl" else 587))
        context = ssl.create_default_context()
        if security == "ssl":
            connection = smtplib.SMTP_SSL(host, port, timeout=20, context=context)
        else:
            connection = smtplib.SMTP(host, port, timeout=20)
        with connection as smtp:
            if security == "starttls":
                smtp.ehlo()
                smtp.starttls(context=context)
                smtp.ehlo()
            smtp.login(user, password)
            refused = smtp.send_message(msg)
        return outcome(not bool(refused))
    except (smtplib.SMTPException, OSError, ValueError) as exc:
        # Do not log email bodies, signing links or provider responses containing addresses.
        logging.getLogger(__name__).warning("Email delivery failed (%s)", type(exc).__name__)
        return outcome(False)


def signing_email_html(app_obj, link, body, link_label=None):
    """Keep the opaque secure URL behind an escaped, descriptive email link."""
    from html import escape
    name = " ".join(str(value or "").strip() for value in
                    (app_obj.first_names, app_obj.surname)).strip()
    label = link_label or ((name + " - Online Application") if name else "Online Application")
    paragraphs = []
    for paragraph in body.split("\n\n"):
        if paragraph.strip() == link:
            paragraphs.append('<p><a href="' + escape(link, quote=True) + '">' + escape(label) + '</a></p>')
        else:
            text = escape(paragraph).replace("\n", "<br>")
            if link:
                text = text.replace(escape(link), '<a href="' + escape(link, quote=True) + '">' + escape(label) + '</a>')
            paragraphs.append("<p>" + text + "</p>")
    return '<html><body style="font-family:Arial,sans-serif;line-height:1.6;color:#172337">' + "".join(paragraphs) + '</body></html>'


CLIENT_EMAIL_DEFAULTS = {
    "invitation": {
        "label": "Application signing invitation",
        "subject": "Your Martin's Funerals secure signing link",
        "body": "Dear {client_name},\n\nPlease open this secure Martin's Funerals link to review your application documents, upload your required FICA documents and sign electronically:\n\n{link}\n\nYou will need your ID number to unlock the page.\n\nYour documents are available inside the secure signing link. After final submission, the link is locked.",
    },
    "activation": {
        "label": "Policy activation confirmation",
        "subject": "Martin's Funerals - policy {policy_number} is active",
        "body": "Dear {client_name},\n\nYour application has been verified and policy {policy_number} is now active from {start_date}.\n\nPlease refer to your policy documents for the benefits, waiting periods, exclusions and payment terms.\n\nThank you.\nMartin's Funerals - Insurance Sales",
    },
    "receipt": {
        "label": "Signed documents receipt",
        "subject": "Martin's Funerals signed documents received",
        "body": "Dear {client_name},\n\nYour signed documents have been received and submitted to Martin's Funerals.\n\nYour signed documents are attached for your records. Please keep them in a safe place. Copies are also stored securely with your application.",
    },
}


def client_email_templates():
    import json
    from app.models import SystemSetting
    row = SystemSetting.query.filter_by(category="Email", key="client_templates_v1").first()
    try:
        saved = json.loads(row.value) if row and row.active else {}
    except (ValueError, TypeError):
        saved = {}
    if not isinstance(saved, dict):
        saved = {}
    return {key: {**default, **(saved.get(key) if isinstance(saved.get(key), dict) else {})}
            for key, default in CLIENT_EMAIL_DEFAULTS.items()}


def validate_client_email_templates(templates):
    from string import Formatter
    allowed = {"client_name", "first_names", "surname", "application_ref", "link", "policy_number", "start_date"}
    for key in CLIENT_EMAIL_DEFAULTS:
        fields = set()
        for part in ("subject", "body"):
            value = templates[key][part].strip()
            if not value or len(value) > (200 if part == "subject" else 10000):
                raise ValueError("Enter a subject (up to 200 characters) and message (up to 10,000 characters).")
            if part == "subject" and ("\n" in value or "\r" in value):
                raise ValueError("The email subject must be a single line.")
            for literal, field, spec, conversion in Formatter().parse(value):
                if field is not None:
                    if field not in allowed or spec or conversion:
                        raise ValueError("Use only the placeholders shown below the editor.")
                    if key in {"receipt", "activation"} and field == "link":
                        raise ValueError("The receipt cannot use the signing link because it is locked after submission.")
                    if part == "body":
                        fields.add(field)
        if key == "invitation" and "link" not in fields:
            raise ValueError("The signing invitation must contain {link} in its message.")


def client_email_content(kind, application, link=""):
    templates = client_email_templates()
    try:
        validate_client_email_templates(templates)
    except (ValueError, KeyError, TypeError):
        templates = CLIENT_EMAIL_DEFAULTS
    item = templates[kind]
    values = {"client_name": " ".join(filter(None, [application.first_names, application.surname])) or "Client",
              "first_names": application.first_names or "", "surname": application.surname or "",
              "application_ref": application.application_ref or "", "link": link,
              "policy_number": application.policy_number or "", "start_date": str(application.inception_date or "")}
    return item["subject"].format(**values), item["body"].format(**values)

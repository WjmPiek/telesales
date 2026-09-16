import os
import smtplib
import logging
import ssl
from email.message import EmailMessage
from email.utils import parseaddr


def send_email(to_email, subject, body, attachments=None, html_body=None):
    host = os.getenv("SMTP_HOST", "smtp.gmail.com").strip()
    # A different mail provider must have its own credentials; never send Gmail credentials to it.
    legacy_gmail = host.lower() == "smtp.gmail.com"
    user = os.getenv("SMTP_USERNAME") or os.getenv("SMTP_USER") or (os.getenv("GMAIL_SMTP_USER") if legacy_gmail else None)
    password = os.getenv("SMTP_PASSWORD") or (os.getenv("GMAIL_SMTP_PASSWORD") if legacy_gmail else None)
    mail_from = os.getenv("MAIL_FROM", user or "no-reply@example.com")
    if not to_email or not user or not password:
        logging.getLogger(__name__).warning("Email not sent: SMTP credentials are not configured")
        return False

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
        msg.add_attachment(data, maintype="application", subtype="octet-stream", filename=filename)

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
        return not bool(refused)
    except (smtplib.SMTPException, OSError, ValueError) as exc:
        # Do not log email bodies, signing links or provider responses containing addresses.
        logging.getLogger(__name__).warning("Email delivery failed (%s)", type(exc).__name__)
        return False

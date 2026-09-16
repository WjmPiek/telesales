"""Contact validation shared by signing-link and document delivery."""
import re


def valid_email(value):
    value = (value or '').strip()
    return bool(len(value) <= 254 and re.fullmatch(r"[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+", value))


def receipt_address(form, application):
    value = (form.get('document_email', application.document_email or application.email or '') or '').strip()
    if value and not valid_email(value):
        raise ValueError('Enter a valid email address for your signed documents, or leave it blank.')
    if 'document_email' in form and value != (form.get('document_email_confirm') or '').strip():
        raise ValueError('The two document email addresses must match.')
    return value

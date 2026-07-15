import os
from dataclasses import dataclass
import requests
from urllib.parse import urlparse


@dataclass
class SendResult:
    ok: bool
    message_id: str | None = None
    error: str | None = None
    response_json: dict | None = None


def normalize_phone(value: str) -> str:
    number = "".join(ch for ch in str(value or "") if ch.isdigit())
    if number.startswith("0"):
        number = "27" + number[1:]
    return number


def _provider() -> str:
    return os.getenv("WHATSAPP_PROVIDER", "360dialog").strip().lower()




def validate_public_image_url(image_url: str) -> tuple[bool, str | None]:
    """Confirm the template example image is publicly reachable by Meta/360dialog."""
    value = (image_url or "").strip()
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.netloc:
        return False, "The campaign image URL must be a public HTTPS URL."
    try:
        response = requests.get(value, timeout=25, allow_redirects=True, stream=True)
        content_type = (response.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        if response.status_code != 200:
            return False, f"The campaign image URL returned HTTP {response.status_code}."
        if not content_type.startswith("image/"):
            return False, f"The campaign image URL returned {content_type or 'an unknown content type'} instead of an image."
        return True, None
    except requests.RequestException as exc:
        return False, f"The campaign image URL could not be reached: {exc}"


def send_whatsapp_text(to_number: str, message: str) -> SendResult:
    to_number = normalize_phone(to_number)
    if not to_number or not message.strip():
        return SendResult(False, error="A valid destination number and message are required.")

    if os.getenv("WHATSAPP_ENABLED", "false").lower() not in {"true", "1", "yes", "y"}:
        return SendResult(False, error="WhatsApp is disabled. Set WHATSAPP_ENABLED=true.")

    if _provider() == "360dialog":
        api_key = os.getenv("D360_API_KEY")
        if not api_key:
            return SendResult(False, error="D360_API_KEY is not configured.")
        base_url = os.getenv("D360_API_BASE_URL", "https://waba-v2.360dialog.io").rstrip("/")
        url = f"{base_url}/messages"
        headers = {"D360-API-KEY": api_key, "Content-Type": "application/json"}
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to_number,
            "type": "text",
            "text": {"preview_url": True, "body": message.strip()},
        }
    else:
        token = os.getenv("WHATSAPP_ACCESS_TOKEN")
        phone_number_id = os.getenv("WHATSAPP_PHONE_NUMBER_ID")
        if not token or not phone_number_id:
            return SendResult(False, error="Meta WhatsApp credentials are incomplete.")
        url = f"https://graph.facebook.com/v25.0/{phone_number_id}/messages"
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        payload = {"messaging_product": "whatsapp", "to": to_number, "type": "text", "text": {"preview_url": True, "body": message.strip()}}

    try:
        response = requests.post(url, json=payload, headers=headers, timeout=25)
        data = response.json() if response.content else {}
        if response.status_code >= 400:
            return SendResult(False, error=data.get("error", {}).get("message") or response.text or f"HTTP {response.status_code}", response_json=data)
        message_id = ((data.get("messages") or [{}])[0]).get("id")
        return SendResult(True, message_id=message_id, response_json=data)
    except requests.RequestException as exc:
        return SendResult(False, error=f"WhatsApp provider request failed: {exc}")


def send_whatsapp_message(to_number: str, message: str) -> bool:
    """Backward-compatible helper used by campaign delivery."""
    return send_whatsapp_text(to_number, message).ok


def send_whatsapp_template_image(to_number: str, template_name: str, language_code: str, image_url: str, callback_payload: str, optout_payload: str, customer_name: str = "Customer") -> SendResult:
    """Send an approved WhatsApp marketing template with image header and two quick-reply buttons."""
    to_number = normalize_phone(to_number)
    if not to_number or not template_name or not image_url:
        return SendResult(False, error="Number, approved template name and public image URL are required.")
    if os.getenv("WHATSAPP_ENABLED", "false").lower() not in {"true", "1", "yes", "y"}:
        return SendResult(False, error="WhatsApp is disabled. Set WHATSAPP_ENABLED=true.")

    payload = {
        "messaging_product": "whatsapp",
        "to": to_number,
        "type": "template",
        "template": {
            "name": template_name.strip(),
            "language": {"code": (language_code or "en_US").strip()},
            "components": [
                {"type": "header", "parameters": [{"type": "image", "image": {"link": image_url}}]},
                {"type": "body", "parameters": [{"type": "text", "text": customer_name or "Customer"}]},
                {"type": "button", "sub_type": "quick_reply", "index": "0", "parameters": [{"type": "payload", "payload": callback_payload}]},
                {"type": "button", "sub_type": "quick_reply", "index": "1", "parameters": [{"type": "payload", "payload": optout_payload}]},
            ],
        },
    }

    if _provider() == "360dialog":
        api_key = os.getenv("D360_API_KEY")
        if not api_key:
            return SendResult(False, error="D360_API_KEY is not configured.")
        url = f"{os.getenv('D360_API_BASE_URL', 'https://waba-v2.360dialog.io').rstrip('/')}/messages"
        headers = {"D360-API-KEY": api_key, "Content-Type": "application/json"}
    else:
        token = os.getenv("WHATSAPP_ACCESS_TOKEN")
        phone_number_id = os.getenv("WHATSAPP_PHONE_NUMBER_ID")
        if not token or not phone_number_id:
            return SendResult(False, error="Meta WhatsApp credentials are incomplete.")
        url = f"https://graph.facebook.com/v25.0/{phone_number_id}/messages"
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    try:
        response = requests.post(url, json=payload, headers=headers, timeout=30)
        data = response.json() if response.content else {}
        if response.status_code >= 400:
            error = data.get("error", {})
            return SendResult(False, error=error.get("message") or response.text or f"HTTP {response.status_code}", response_json=data)
        message_id = ((data.get("messages") or [{}])[0]).get("id")
        return SendResult(True, message_id=message_id, response_json=data)
    except requests.RequestException as exc:
        return SendResult(False, error=f"WhatsApp provider request failed: {exc}")


@dataclass
class TemplateStatusResult:
    ok: bool
    status: str = "Unknown"
    raw_status: str | None = None
    error: str | None = None
    template: dict | None = None
    provider: str | None = None
    provider_request_id: str | None = None
    template_id: str | None = None
    category: str | None = None
    language: str | None = None
    rejection_reason: str | None = None
    quality_score: str | None = None


def get_whatsapp_template_status(template_name: str, language_code: str = "en_US") -> TemplateStatusResult:
    """Read the live template state from 360dialog or Meta.

    For 360dialog, D360_TEMPLATE_API_URL can override the default template endpoint.
    The parser accepts the common 360dialog response containers: waba_templates,
    templates, and data.
    """
    name = (template_name or "").strip()
    language = (language_code or "en_US").strip()
    if not name:
        return TemplateStatusResult(False, error="No WhatsApp template name is configured.")

    def normalize(value: str | None) -> str:
        raw = (value or "UNKNOWN").strip().upper().replace("-", "_").replace(" ", "_")
        if raw in {"APPROVED", "ACTIVE"}:
            return "Approved"
        if raw in {"PENDING", "IN_REVIEW", "PENDING_REVIEW", "IN_APPEAL"}:
            return "Pending"
        if raw == "REJECTED":
            return "Rejected"
        if raw == "PAUSED":
            return "Paused"
        if raw == "DISABLED":
            return "Disabled"
        if raw == "DELETED":
            return "Deleted"
        return raw.title().replace("_", " ") if raw else "Unknown"

    if _provider() == "360dialog":
        api_key = os.getenv("D360_API_KEY")
        if not api_key:
            return TemplateStatusResult(False, error="D360_API_KEY is not configured.", provider="360dialog")
        base = os.getenv("D360_API_BASE_URL", "https://waba-v2.360dialog.io").rstrip("/")
        url = os.getenv("D360_TEMPLATE_API_URL", f"{base}/v1/configs/templates").strip()
        headers = {"D360-API-KEY": api_key, "Accept": "application/json"}
    else:
        token = os.getenv("WHATSAPP_ACCESS_TOKEN")
        waba_id = os.getenv("WHATSAPP_BUSINESS_ACCOUNT_ID")
        if not token or not waba_id:
            return TemplateStatusResult(False, error="Meta template credentials are incomplete. Configure WHATSAPP_ACCESS_TOKEN and WHATSAPP_BUSINESS_ACCOUNT_ID.", provider="meta")
        url = f"https://graph.facebook.com/v25.0/{waba_id}/message_templates"
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}

    try:
        response = requests.get(url, headers=headers, timeout=25)
        data = response.json() if response.content else {}
        if response.status_code >= 400:
            message = data.get("error", {}).get("message") if isinstance(data, dict) else None
            return TemplateStatusResult(False, error=message or response.text or f"HTTP {response.status_code}")

        if isinstance(data, list):
            templates = data
        elif isinstance(data, dict):
            templates = data.get("waba_templates") or data.get("templates") or data.get("data") or []
        else:
            templates = []

        def normalized_name(value):
            import re
            return re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().casefold()).strip("_")

        def normalized_language(value):
            return str(value or "").strip().replace("-", "_").casefold()

        wanted_name = normalized_name(name)
        wanted_language = normalized_language(language)
        same_name = []
        available = []
        for item in templates:
            if not isinstance(item, dict):
                continue
            item_name = str(item.get("name") or item.get("template_name") or "").strip()
            item_language = item.get("language") or item.get("language_code") or item.get("languageCode") or ""
            if isinstance(item_language, dict):
                item_language = item_language.get("code") or item_language.get("language_code") or ""
            if item_name:
                available.append(f"{item_name} ({item_language or 'language not supplied'})")
            if normalized_name(item_name) != wanted_name:
                continue
            same_name.append((item, str(item_language or "")))
            actual_language = normalized_language(item_language)
            exact_language = not actual_language or actual_language == wanted_language
            same_base_language = actual_language.split("_")[0] == wanted_language.split("_")[0]
            if exact_language or same_base_language:
                raw_status = item.get("status") or item.get("state") or item.get("template_status")
                reason = (
                    item.get("rejected_reason") or item.get("rejection_reason") or
                    item.get("reason") or item.get("status_reason") or
                    item.get("last_error")
                )
                if isinstance(reason, dict):
                    reason = reason.get("message") or reason.get("reason") or str(reason)
                quality = item.get("quality_score") or item.get("quality_rating") or item.get("quality")
                if isinstance(quality, dict):
                    quality = quality.get("score") or quality.get("rating") or str(quality)
                request_id = (
                    response.headers.get("x-request-id") or
                    response.headers.get("x-fb-trace-id") or
                    response.headers.get("x-business-use-case-usage")
                )
                return TemplateStatusResult(
                    True,
                    status=normalize(raw_status),
                    raw_status=str(raw_status or "UNKNOWN"),
                    template=item,
                    provider=_provider(),
                    provider_request_id=request_id,
                    template_id=str(item.get("id") or item.get("template_id") or item.get("message_template_id") or "") or None,
                    category=str(item.get("category") or "") or None,
                    language=str(item_language or "") or None,
                    rejection_reason=str(reason or "") or None,
                    quality_score=str(quality or "") or None,
                )

        if same_name:
            langs = ", ".join(sorted({lang or "unspecified" for _, lang in same_name}))
            return TemplateStatusResult(True, status="Not found", error=f"Template '{name}' exists, but not for language '{language}'. Available language(s): {langs}.")
        suggestions = ", ".join(available[:8])
        suffix = f" Available templates: {suggestions}." if suggestions else " No templates were returned by the connected account."
        return TemplateStatusResult(True, status="Not found", error=f"Template '{name}' ({language}) was not found in the connected WhatsApp Business Account.{suffix}")
    except requests.RequestException as exc:
        return TemplateStatusResult(False, error=f"Template status request failed: {exc}")

@dataclass
class TemplateCreateResult:
    ok: bool
    status: str = "Unknown"
    template_id: str | None = None
    error: str | None = None
    response_json: dict | None = None


def create_whatsapp_image_template(template_name: str, language_code: str, body_text: str, image_example_url: str) -> TemplateCreateResult:
    """Submit an image marketing template with callback and opt-out quick replies.

    360dialog accepts the WhatsApp/Meta components schema at its template
    configuration endpoint. Provider validation errors are returned verbatim so
    the campaign page can explain exactly what must be corrected.
    """
    name = (template_name or "").strip().lower()
    language = (language_code or "en").strip()
    if language.lower() in {"en_us", "en-us", "english"}:
        language = "en"
    body = (body_text or "").strip()
    image_example_url = (image_example_url or "").strip()
    if not name or not body or not image_example_url:
        return TemplateCreateResult(False, error="Template name, body text and a public example image are required.")
    # Do not make a single-worker Render service call its own public URL here.
    # That can deadlock until timeout. The image bytes were already validated
    # during upload; 360dialog/Meta will fetch the HTTPS URL during submission.
    if not image_example_url.lower().startswith("https://"):
        return TemplateCreateResult(False, error="The template header image must use a public HTTPS URL.")
    if not __import__('re').fullmatch(r"[a-z0-9_]+", name):
        return TemplateCreateResult(False, error="Template name may contain only lowercase letters, numbers and underscores.")
    if "{{1}}" not in body:
        return TemplateCreateResult(False, error="The template body must contain {{1}} for the customer's name.")

    payload = {
        "name": name,
        "category": "MARKETING",
        "language": language,
        "components": [
            {
                "type": "HEADER",
                "format": "IMAGE",
                "example": {"header_handle": [image_example_url]},
            },
            {
                "type": "BODY",
                "text": body,
                "example": {"body_text": [["Wjm"]]},
            },
            {
                "type": "BUTTONS",
                "buttons": [
                    {"type": "QUICK_REPLY", "text": "YES, CALL ME BACK"},
                    {"type": "QUICK_REPLY", "text": "NO THANKS, OPT OUT"},
                ],
            },
        ],
    }

    if _provider() == "360dialog":
        api_key = os.getenv("D360_API_KEY")
        if not api_key:
            return TemplateCreateResult(False, error="D360_API_KEY is not configured.")
        base = os.getenv("D360_API_BASE_URL", "https://waba-v2.360dialog.io").rstrip("/")
        url = os.getenv("D360_TEMPLATE_API_URL", f"{base}/v1/configs/templates").strip()
        headers = {"D360-API-KEY": api_key, "Content-Type": "application/json", "Accept": "application/json"}
    else:
        token = os.getenv("WHATSAPP_ACCESS_TOKEN")
        waba_id = os.getenv("WHATSAPP_BUSINESS_ACCOUNT_ID")
        if not token or not waba_id:
            return TemplateCreateResult(False, error="Meta template credentials are incomplete.")
        url = f"https://graph.facebook.com/v25.0/{waba_id}/message_templates"
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json", "Accept": "application/json"}

    try:
        response = requests.post(url, json=payload, headers=headers, timeout=40)
        data = response.json() if response.content else {}
        if response.status_code >= 400:
            error = data.get("error") if isinstance(data, dict) else None
            if isinstance(error, dict):
                message = error.get("message") or error.get("error_user_msg")
            else:
                message = str(error or "")
            return TemplateCreateResult(False, error=message or response.text or f"HTTP {response.status_code}", response_json=data)
        template_id = str(data.get("id") or data.get("template_id") or data.get("message_template_id") or "") or None
        raw_status = str(data.get("status") or data.get("state") or "PENDING").upper()
        status = "Approved" if raw_status in {"APPROVED", "ACTIVE"} else "Pending"
        # Keep the exact public example URL in the response metadata so the UI
        # can prove which image was submitted without requiring 360dialog login.
        if isinstance(data, dict):
            data.setdefault("submitted_header_image_url", image_example_url)
        return TemplateCreateResult(True, status=status, template_id=template_id, response_json=data)
    except requests.RequestException as exc:
        return TemplateCreateResult(False, error=f"Template submission failed: {exc}")

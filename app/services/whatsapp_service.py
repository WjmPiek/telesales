import os
from dataclasses import dataclass
import requests


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

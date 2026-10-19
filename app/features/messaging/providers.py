"""Where an SMS goes, chosen by SMS_BACKEND (PRD SMS-1, SMS-6).

- console: writes the SMS to the application log instead of sending it. Development and
  tests always use it; config.py refuses africastalking outside production (SEC-7).
- africastalking: one plain httpx request to Africa's Talking's bulk SMS endpoint (no SDK,
  AGENTS.md section 3), one customer per request. That endpoint has no sandbox, so config.py
  allows it in production only (SEC-7).

Only messaging/service.py calls this. A message that did not go raises SmsSendError, with a
fixed plain reason the app chose (SMS-3): never the provider's own text, the API key or the
phone number. Anything unexpected (a bug) is not caught here: it goes to the one broad catch
around the call in messaging/service.py, which marks the SMS failed, logs the stack trace and
records it (PRD SMS-4a, architecture.md section 5). Catching it here as well would only log it
twice.
"""

import logging

import httpx

from app.core.config import Settings
from app.core.templating import mask_phone

logger = logging.getLogger("app.messaging")

AFRICASTALKING_URL = "https://api.africastalking.com/version1/messaging/bulk"
TIMEOUT_SECONDS = 10
# Africa's Talking status codes for a recipient: 100 Processed, 101 Sent, 102 Queued.
# Anything else means the message did not go.
ACCEPTED_STATUS_CODES = {100, 101, 102}
# SMS-3: the reason kept for a failed message is picked here from the status code, so it is
# always a plain sentence, never what the provider wrote
FAILURE_REASONS = {
    402: "The sender ID is not registered.",
    403: "That phone number is not valid.",
    405: "Africa's Talking balance is too low. Top up and press Send again.",
}
UNREADABLE_ANSWER = "Africa's Talking sent an answer that could not be read."


class SmsSendError(Exception):
    """The SMS did not go. The text says why, for messages_log and the audit log."""


def send_sms(settings: Settings, phone: str, text: str) -> str | None:
    """Send one SMS to a stored phone number (254722123456). Returns the provider's message
    id, which the console backend does not have."""
    if settings.sms_backend == "africastalking":
        return send_with_africastalking(settings, phone, text)
    write_to_console(phone, text)
    return None


def write_to_console(phone: str, text: str) -> None:
    """SMS-6: the text goes to the log (the journal on the server), and nothing is sent."""
    logger.info("SMS to %s (console backend, not sent):\n%s", mask_phone(phone), text)


def send_with_africastalking(
    settings: Settings, phone: str, text: str, client: httpx.Client | None = None
) -> str | None:
    """`client` lets a test answer in place of Africa's Talking. Normally a new one is made."""
    try:
        with client or httpx.Client(timeout=TIMEOUT_SECONDS) as http:
            response = http.post(
                AFRICASTALKING_URL,
                # the key goes in a header, never in the URL, so it cannot end up in a log line
                headers={"apiKey": settings.at_api_key, "Accept": "application/json",
                         "Content-Type": "application/json"},
                json={"username": settings.at_username, "phoneNumbers": ["+" + phone], "message": text,
                      "senderId": settings.at_sender_id},
            )
        response.raise_for_status()
        recipient = response.json()["SMSMessageData"]["Recipients"][0]
        status_code = int(recipient["statusCode"])
        if status_code not in ACCEPTED_STATUS_CODES:
            raise SmsSendError(failure_reason(status_code))
        return recipient.get("messageId")
    except SmsSendError:
        raise
    except httpx.TimeoutException as exc:
        raise SmsSendError(f"Africa's Talking did not answer within {TIMEOUT_SECONDS} seconds. "
                           "Press Send again.") from exc
    except httpx.HTTPStatusError as exc:
        raise SmsSendError(f"Africa's Talking refused the request (HTTP {exc.response.status_code}). "
                           "Check AT_USERNAME and AT_API_KEY.") from exc
    except httpx.HTTPError as exc:
        raise SmsSendError("Could not reach Africa's Talking. Check the server's internet connection, "
                           "then press Send again.") from exc
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        # not JSON, no recipient, or a status code that is not a number
        raise SmsSendError(UNREADABLE_ANSWER) from exc


def failure_reason(status_code: int) -> str:
    """The plain reason for a recipient status code that is not a success."""
    return FAILURE_REASONS.get(status_code, f"Africa's Talking could not send this message (code {status_code}).")

"""Where an SMS goes, chosen by SMS_BACKEND (PRD SMS-1, SMS-6).

- console: writes the SMS to the application log instead of sending it. Development and
  tests always use it; config.py refuses africastalking outside production (SEC-7).
- africastalking: one plain httpx request to Africa's Talking (no SDK, AGENTS.md section 3).
  With AT_USERNAME=sandbox it goes to their test address instead, where messages only show in
  their simulator. That is the one way to try it outside production.

Only messaging/service.py calls this. A message that did not go raises SmsSendError, with a
reason that is safe to log: never the API key, never the phone number. Anything unexpected
(a bug) is not caught here: it goes to the one broad catch around the call in
messaging/service.py, which marks the SMS failed, logs the stack trace and records it
(PRD SMS-4a, architecture.md section 5). Catching it here as well would only log it twice.
"""

import logging

import httpx

from app.core.config import Settings
from app.core.templating import mask_phone

logger = logging.getLogger("app.messaging")

AFRICASTALKING_URL = "https://api.africastalking.com/version1/messaging"
# the test account (AT_USERNAME=sandbox) has its own address and sends nothing to a real phone
SANDBOX_URL = "https://api.sandbox.africastalking.com/version1/messaging"
TIMEOUT_SECONDS = 10
# Africa's Talking status codes for a recipient: 100 Processed, 101 Sent, 102 Queued.
# Anything else means the message did not go.
ACCEPTED_STATUS_CODES = {100, 101, 102}


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
                SANDBOX_URL if settings.uses_sandbox else AFRICASTALKING_URL,
                # the key goes in a header, never in the URL, so it cannot end up in a log line
                headers={"apiKey": settings.at_api_key, "Accept": "application/json"},
                data={"username": settings.at_username, "to": "+" + phone, "message": text},
            )
        response.raise_for_status()
        recipient = response.json()["SMSMessageData"]["Recipients"][0]
        if recipient["statusCode"] not in ACCEPTED_STATUS_CODES:
            raise SmsSendError(f"Africa's Talking did not send it: {recipient.get('status', 'no reason given')}")
        return recipient.get("messageId")
    except SmsSendError:
        raise
    except httpx.TimeoutException as exc:
        raise SmsSendError(f"Africa's Talking did not answer within {TIMEOUT_SECONDS} seconds") from exc
    except httpx.HTTPStatusError as exc:
        raise SmsSendError(f"Africa's Talking answered with status {exc.response.status_code}") from exc
    except httpx.HTTPError as exc:
        raise SmsSendError(f"Could not reach Africa's Talking ({type(exc).__name__})") from exc
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise SmsSendError("Africa's Talking sent an answer that could not be read") from exc

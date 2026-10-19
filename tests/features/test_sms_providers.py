"""messaging/providers.py (TASK-040, SMS-3, SMS-6, SEC-7).

Africa's Talking is never reached: each test hands the provider a stand-in transport that
answers the way Africa's Talking's bulk SMS endpoint does, and conftest.py makes every real
network request fail.
"""

import json
import logging
from pathlib import Path

import httpx
import pytest

from app.core.config import load_settings
from app.features.messaging import providers
from app.features.messaging.providers import SmsSendError

FAKE_API_KEY = "test-key-not-a-real-one"


def settings(backend):
    environ = {"APP_ENV": "production", "DATABASE_URL": "mysql+pymysql://u:p@127.0.0.1/water_billing",
               "SESSION_SECRET": "x" * 48, "SMS_BACKEND": backend}
    if backend == "africastalking":
        environ.update({"AT_USERNAME": "ccwater", "AT_API_KEY": FAKE_API_KEY, "AT_SENDER_ID": "CCH2O"})
    return load_settings(environ)


def africastalking_answering(status_code=201, body=None, seen=None):
    """A stand-in for Africa's Talking that answers every request the same way."""

    def handle(request):
        if seen is not None:
            seen.append(request)
        if body is None:
            answer = {"SMSMessageData": {"Message": "Sent to 1/1", "Recipients": [
                {"statusCode": 101, "number": "+254722123456", "status": "Success", "cost": "KES 0.8000",
                 "messageId": "ATXid_abc123"}]}}
            return httpx.Response(status_code, json=answer)
        return httpx.Response(status_code, content=body)

    return httpx.Client(transport=httpx.MockTransport(handle))


def recipient_answer(status_code, status):
    return json.dumps({"SMSMessageData": {"Recipients": [
        {"statusCode": status_code, "number": "+254722123456", "status": status, "cost": "0",
         "messageId": "None"}]}}).encode()


def failure_for(body, status_code=201):
    """The reason send_with_africastalking gives when Africa's Talking answers with `body`."""
    with pytest.raises(SmsSendError) as raised:
        providers.send_with_africastalking(settings("africastalking"), "254722123456", "Hi",
                                           client=africastalking_answering(status_code=status_code, body=body))
    return str(raised.value)


# ---- console ----

def test_the_console_backend_writes_the_text_to_the_log_and_sends_nothing(caplog):
    with caplog.at_level(logging.INFO, logger="app.messaging"):
        message_id = providers.send_sms(settings("console"), "254722123456", "Hi Mary,\nAmount Due: Ksh 2,100")

    assert message_id is None
    logged = caplog.records[-1].getMessage()
    assert "Hi Mary,\nAmount Due: Ksh 2,100" in logged
    assert "not sent" in logged
    # LOG-2: the phone number is masked in the log
    assert "254722123456" not in logged and "0722 XXX 456" in logged


# ---- Africa's Talking ----

def test_africastalking_gets_one_number_the_text_and_the_sender_id_as_json_to_the_bulk_endpoint():
    seen = []

    message_id = providers.send_with_africastalking(settings("africastalking"), "254722123456", "Hi Mary,",
                                                    client=africastalking_answering(seen=seen))

    assert message_id == "ATXid_abc123"
    request = seen[0]
    assert request.method == "POST"
    assert str(request.url) == providers.AFRICASTALKING_URL == "https://api.africastalking.com/version1/messaging/bulk"
    assert request.headers["apiKey"] == FAKE_API_KEY
    assert request.headers["Accept"] == "application/json"
    assert request.headers["Content-Type"] == "application/json"
    assert FAKE_API_KEY not in str(request.url)
    assert json.loads(request.content) == {"username": "ccwater", "phoneNumbers": ["+254722123456"],
                                           "message": "Hi Mary,", "senderId": "CCH2O"}


def test_only_the_bulk_endpoint_is_used():
    # Premium SMS (content.africastalking.com) is a different product, and the old endpoint and
    # its sandbox are gone
    source = Path(providers.__file__).read_text(encoding="utf-8")

    assert "content.africastalking.com" not in source
    assert "sandbox.africastalking.com" not in source
    assert source.count("https://") == 1


@pytest.mark.parametrize("status_code", [100, 101, 102])
def test_processed_sent_and_queued_all_count_as_sent(status_code):
    body = json.dumps({"SMSMessageData": {"Recipients": [
        {"statusCode": status_code, "status": "Success", "messageId": "ATXid_1"}]}}).encode()

    message_id = providers.send_with_africastalking(settings("africastalking"), "254722123456", "Hi",
                                                    client=africastalking_answering(body=body))

    assert message_id == "ATXid_1"


@pytest.mark.parametrize("status_code,status,reason", [
    (405, "InsufficientBalance", "Africa's Talking balance is too low. Top up and press Send again."),
    (402, "InvalidSenderId", "The sender ID is not registered."),
    (403, "InvalidPhoneNumber", "That phone number is not valid."),
    (401, "RiskHold", "Africa's Talking could not send this message (code 401)."),
    (404, "UnsupportedNumberType", "Africa's Talking could not send this message (code 404)."),
    (406, "UserInBlacklist", "Africa's Talking could not send this message (code 406)."),
    (407, "CouldNotRoute", "Africa's Talking could not send this message (code 407)."),
    (409, "DoNotDisturbRejection", "Africa's Talking could not send this message (code 409)."),
    (500, "InternalServerError", "Africa's Talking could not send this message (code 500)."),
    (501, "GatewayError", "Africa's Talking could not send this message (code 501)."),
    (502, "RejectedByGateway", "Africa's Talking could not send this message (code 502)."),
])
def test_each_failure_code_gives_its_plain_reason_never_the_providers_text(status_code, status, reason):
    given = failure_for(recipient_answer(status_code, status))

    assert given == reason
    # SMS-3: the app's own sentence, never what Africa's Talking wrote, the key or the number
    assert status not in given
    assert FAKE_API_KEY not in given and "722123456" not in given


def test_a_status_code_sent_as_text_is_still_read():
    assert failure_for(recipient_answer("405", "InsufficientBalance")).startswith("Africa's Talking balance is too low")


@pytest.mark.parametrize("body", [
    b"<html>Gateway error</html>",
    json.dumps({"SMSMessageData": {"Message": "InvalidSenderId", "Recipients": []}}).encode(),
    json.dumps({"SMSMessageData": {}}).encode(),
    json.dumps({"SMSMessageData": {"Recipients": [{"status": "Success"}]}}).encode(),
    json.dumps({"SMSMessageData": {"Recipients": [{"statusCode": "soon"}]}}).encode(),
], ids=["not json", "no recipient", "no recipient list", "no status code", "status code not a number"])
def test_an_answer_with_no_readable_recipient_is_a_failed_send(body):
    assert failure_for(body) == "Africa's Talking sent an answer that could not be read."


def test_a_wrong_key_is_a_failed_send_that_never_shows_the_key_or_the_answer():
    given = failure_for(f"The supplied authentication is invalid: {FAKE_API_KEY}".encode(), status_code=401)

    assert given == "Africa's Talking refused the request (HTTP 401). Check AT_USERNAME and AT_API_KEY."
    assert FAKE_API_KEY not in given and "authentication" not in given


def test_no_answer_in_time_is_a_failed_send():
    def too_slow(request):
        raise httpx.ReadTimeout("timed out", request=request)

    client = httpx.Client(transport=httpx.MockTransport(too_slow))

    with pytest.raises(SmsSendError) as raised:
        providers.send_with_africastalking(settings("africastalking"), "254722123456", "Hi", client=client)

    assert str(raised.value) == "Africa's Talking did not answer within 10 seconds. Press Send again."


def test_a_bug_while_sending_is_left_for_the_messaging_service_to_record():
    # SMS-4a: not turned into a failed send here, so the one broad catch in
    # messaging/service.py sees the real exception and logs its stack trace once
    def broken(request):
        raise RuntimeError("a bug, not a provider problem")

    client = httpx.Client(transport=httpx.MockTransport(broken))

    with pytest.raises(RuntimeError):
        providers.send_with_africastalking(settings("africastalking"), "254722123456", "Hi", client=client)


# ---- no test can reach the real provider (SEC-7) ----

def test_a_test_that_forgets_the_stand_in_cannot_reach_africastalking():
    # send_sms picks Africa's Talking for this setting, and the network guard stops it
    with pytest.raises(SmsSendError) as raised:
        providers.send_sms(settings("africastalking"), "254722123456", "Hi")

    assert str(raised.value) == ("Could not reach Africa's Talking. Check the server's internet connection, "
                                 "then press Send again.")


def test_tests_run_with_the_console_backend():
    from app.core.config import get_settings

    assert get_settings().sms_backend == "console"

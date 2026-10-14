"""messaging/providers.py (TASK-040, SMS-6, SEC-7).

Africa's Talking is never reached: each test hands the provider a stand-in transport that
answers the way Africa's Talking does, and conftest.py makes every real network request fail.
"""

import json
import logging
from urllib.parse import parse_qs

import httpx
import pytest

from app.core.config import load_settings
from app.features.messaging import providers
from app.features.messaging.providers import SmsSendError

FAKE_API_KEY = "test-key-not-a-real-one"


def settings(backend, username="ccwater"):
    environ = {"APP_ENV": "production", "DATABASE_URL": "mysql+pymysql://u:p@127.0.0.1/water_billing",
               "SESSION_SECRET": "x" * 48, "SMS_BACKEND": backend}
    if backend == "africastalking":
        environ.update({"AT_USERNAME": username, "AT_API_KEY": FAKE_API_KEY})
    return load_settings(environ)


def sandbox_settings():
    # the sandbox is a development tool, so it is built in the development environment
    return load_settings({"APP_ENV": "development", "DATABASE_URL": "mysql+pymysql://u:p@127.0.0.1/dev",
                          "SMS_BACKEND": "africastalking", "AT_USERNAME": "sandbox", "AT_API_KEY": FAKE_API_KEY})


def africastalking_answering(status_code=201, body=None, seen=None):
    """A stand-in for Africa's Talking that answers every request the same way."""

    def handle(request):
        if seen is not None:
            seen.append(request)
        if body is None:
            answer = {"SMSMessageData": {"Message": "Sent to 1/1", "Recipients": [
                {"statusCode": 101, "number": "+254722123456", "status": "Success", "messageId": "ATXid_abc123"}]}}
            return httpx.Response(status_code, json=answer)
        return httpx.Response(status_code, content=body)

    return httpx.Client(transport=httpx.MockTransport(handle))


def recipient_answer(status_code, status):
    return json.dumps({"SMSMessageData": {"Recipients": [
        {"statusCode": status_code, "number": "+254722123456", "status": status}]}}).encode()


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

def test_africastalking_gets_the_number_the_text_and_the_key_in_a_header():
    seen = []

    message_id = providers.send_with_africastalking(settings("africastalking"), "254722123456", "Hi Mary,",
                                                    client=africastalking_answering(seen=seen))

    assert message_id == "ATXid_abc123"
    request = seen[0]
    assert str(request.url) == providers.AFRICASTALKING_URL
    assert request.headers["apiKey"] == FAKE_API_KEY
    assert FAKE_API_KEY not in str(request.url)
    form = parse_qs(request.content.decode())
    assert form == {"username": ["ccwater"], "to": ["+254722123456"], "message": ["Hi Mary,"]}


def test_the_sandbox_account_goes_to_the_sandbox_address_with_its_own_username():
    seen = []

    message_id = providers.send_with_africastalking(sandbox_settings(), "254722123456", "Hi Mary,",
                                                    client=africastalking_answering(seen=seen))

    assert message_id == "ATXid_abc123"
    assert str(seen[0].url) == providers.SANDBOX_URL == "https://api.sandbox.africastalking.com/version1/messaging"
    assert parse_qs(seen[0].content.decode())["username"] == ["sandbox"]
    assert seen[0].headers["apiKey"] == FAKE_API_KEY


def test_a_live_account_never_goes_to_the_sandbox_address():
    seen = []

    providers.send_with_africastalking(settings("africastalking"), "254722123456", "Hi",
                                       client=africastalking_answering(seen=seen))

    assert str(seen[0].url) == providers.AFRICASTALKING_URL


def test_send_sms_with_the_sandbox_fails_cleanly_when_the_network_is_down():
    # conftest.py stops every real request; the sandbox is no exception in tests
    with pytest.raises(SmsSendError, match="Could not reach Africa's Talking"):
        providers.send_sms(sandbox_settings(), "254722123456", "Hi")


@pytest.mark.parametrize("status_code", [100, 101, 102])
def test_processed_sent_and_queued_all_count_as_sent(status_code):
    body = json.dumps({"SMSMessageData": {"Recipients": [
        {"statusCode": status_code, "status": "Success", "messageId": "ATXid_1"}]}}).encode()

    message_id = providers.send_with_africastalking(settings("africastalking"), "254722123456", "Hi",
                                                    client=africastalking_answering(body=body))

    assert message_id == "ATXid_1"


def test_a_number_africastalking_refuses_is_a_failed_send_with_its_reason():
    client = africastalking_answering(body=recipient_answer(403, "InvalidPhoneNumber"))

    with pytest.raises(SmsSendError) as raised:
        providers.send_with_africastalking(settings("africastalking"), "254722123456", "Hi", client=client)

    assert str(raised.value) == "Africa's Talking did not send it: InvalidPhoneNumber"


def test_a_low_balance_is_a_failed_send():
    client = africastalking_answering(body=recipient_answer(405, "InsufficientBalance"))

    with pytest.raises(SmsSendError, match="InsufficientBalance"):
        providers.send_with_africastalking(settings("africastalking"), "254722123456", "Hi", client=client)


def test_a_wrong_key_is_a_failed_send_that_never_shows_the_key():
    client = africastalking_answering(status_code=401, body=b"The supplied authentication is invalid")

    with pytest.raises(SmsSendError) as raised:
        providers.send_with_africastalking(settings("africastalking"), "254722123456", "Hi", client=client)

    assert str(raised.value) == "Africa's Talking answered with status 401"
    assert FAKE_API_KEY not in str(raised.value)


def test_no_answer_in_time_is_a_failed_send():
    def too_slow(request):
        raise httpx.ReadTimeout("timed out", request=request)

    client = httpx.Client(transport=httpx.MockTransport(too_slow))

    with pytest.raises(SmsSendError, match="did not answer within 10 seconds"):
        providers.send_with_africastalking(settings("africastalking"), "254722123456", "Hi", client=client)


def test_an_answer_that_cannot_be_read_is_a_failed_send():
    client = africastalking_answering(body=b"<html>Gateway error</html>")

    with pytest.raises(SmsSendError, match="could not be read"):
        providers.send_with_africastalking(settings("africastalking"), "254722123456", "Hi", client=client)


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
    with pytest.raises(SmsSendError, match="Could not reach Africa's Talking"):
        providers.send_sms(settings("africastalking"), "254722123456", "Hi")


def test_tests_run_with_the_console_backend():
    from app.core.config import get_settings

    assert get_settings().sms_backend == "console"

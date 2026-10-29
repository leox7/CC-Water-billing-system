"""The phone method's screens, end to end through the real app (TASK-077, TASK-078, PRD SMS-15
to SMS-18, DASH-2, SECURITY.md SMS-S2 and SMS-S4).

Messages to send, Mark as sent, the Bills button, the readings result, the bill's Messages
card and the dashboard line. No provider is ever reached: `phone_method` fails the test if
Africa's Talking is called.
"""

import html
import re
from urllib.parse import unquote

import pytest
from sqlalchemy import select

from app.features.bills import service as bills
from app.features.messaging import service as messaging
from app.features.readings import service as readings
from app.models import AuditLog, MessageLog, Payment
from tests.conftest import TEST_CSRF_TOKEN

HTMX = {"HX-Request": "true", "X-CSRF-Token": TEST_CSRF_TOKEN}


def text_of(response):
    return html.unescape(response.text)


def audit_rows(db_session, event_type):
    return db_session.scalars(select(AuditLog).where(AuditLog.event_type == event_type)).all()


def enter(db_session, user, customer, current, reading_date="2025-08-30"):
    return readings.create_reading(db_session, user, customer.customer_id,
                                   {"current_reading": current, "reading_date": reading_date})


def shown_form(card):
    """What the Mark as sent form posts: the total and the fingerprint that were shown."""
    return {"total_due": str(card.bill.total_due), "fingerprint": card.fingerprint}


def first_card(db_session):
    return bills.messages_to_send(db_session)[0]


@pytest.fixture
def owner(make_user):
    return make_user(role="owner", first_name="Daniel")


@pytest.fixture
def member(make_user):
    return make_user(role="member", first_name="Peter")


@pytest.fixture
def as_member(web, member, sign_in_as):
    sign_in_as(web, member)
    return web


@pytest.fixture
def mary(make_customer):
    return make_customer(first_name="Mary", last_name="Wanjiru")


@pytest.fixture
def waiting(db_session, owner, mary, phone_method):
    """Mary's first reading. Its bill waits, ready to send."""
    return enter(db_session, owner, mary, "120", "2025-07-30")


# ---- Messages to send (SMS-15, SMS-16, SMS-S4) ----

def test_the_screen_shows_each_waiting_message_with_its_three_actions(db_session, as_member, waiting):
    card = first_card(db_session)

    response = as_member.get("/messages/to-send")
    page = text_of(response)

    assert response.status_code == 200
    assert "Messages to send" in page
    assert "Tap Send by SMS, press send in your messaging app, then come back and tap Mark as sent." in page
    assert "Mary Wanjiru" in page
    assert '<span class="message-chip">Bill</span>' in page
    assert "Ksh 15,750" in page
    assert "0722 123 456" in page
    assert f'href="{card.link}"' in page
    assert f'data-copy-text="{card.text}"' in page
    assert f'hx-post="/messages/{card.message.message_id}/mark-sent"' in page


def test_the_send_by_sms_link_carries_the_number_and_the_whole_text_with_line_breaks(db_session, as_member,
                                                                                    waiting):
    page = text_of(as_member.get("/messages/to-send"))

    link = re.search(r'href="(sms:[^"]+)"', page).group(1)
    number, body = link[len("sms:"):].split("?&body=")
    assert number == "+254722123456"
    assert unquote(body) == bills.sms_text(db_session, waiting.bill, "bill")
    assert "%0A" in body


def test_the_screen_is_never_stored_by_the_browser(as_member, waiting):
    response = as_member.get("/messages/to-send")

    assert response.headers["Cache-Control"] == "no-store"


def test_mark_as_sent_is_guarded_against_double_taps(as_member, waiting):
    page = as_member.get("/messages/to-send").text

    tag = re.search(r'<form[^>]*mark-sent[^>]*>', page).group(0)
    assert 'hx-disabled-elt="find button[type=submit]"' in tag
    assert 'hx-sync="this:drop"' in tag
    assert 'name="csrf_token"' in page


def test_the_screen_takes_no_text_or_number_from_the_browser(as_member, waiting):
    # SMS-S2: the form sends back only the total and the fingerprint
    page = as_member.get("/messages/to-send").text

    form = page.split("mark-sent", 1)[1].split("</form>", 1)[0]
    names = set(re.findall(r'name="([^"]+)"', form))
    assert names == {"csrf_token", "total_due", "fingerprint"}
    assert "<textarea" not in page


def test_an_empty_list_says_so(as_member, phone_method):
    page = text_of(as_member.get("/messages/to-send"))

    assert "Nothing to send. New bills will appear here." in page


def test_inactive_customers_are_left_off_the_list(db_session, as_member, mary, waiting):
    mary.status = "inactive"
    db_session.commit()

    page = text_of(as_member.get("/messages/to-send"))

    assert "Mary Wanjiru" not in page
    assert "Nothing to send." in page


def test_the_page_holds_no_inline_styles_or_scripts(as_member, waiting):
    page = as_member.get("/messages/to-send").text

    assert "style=" not in page
    assert "<script>" not in page
    assert " onclick=" not in page


# ---- Mark as sent (SMS-17, SMS-S4) ----

def test_mark_as_sent_swaps_the_card_for_a_success_message(db_session, as_member, member, waiting):
    card = first_card(db_session)

    response = as_member.post(f"/messages/{card.message.message_id}/mark-sent", data=shown_form(card),
                              headers=HTMX)

    assert response.status_code == 200
    assert "Marked as sent: Mary Wanjiru." in text_of(response)
    assert 'role="status"' in response.text
    message = db_session.get(MessageLog, card.message.message_id)
    assert (message.status, message.sent_by_user_id) == ("sent", member.user_id)
    assert message.content == card.text
    assert len(audit_rows(db_session, "sms.marked_sent")) == 1


def test_the_owner_can_mark_as_sent_too(db_session, web, owner, sign_in_as, waiting):
    sign_in_as(web, owner)
    card = first_card(db_session)

    response = web.post(f"/messages/{card.message.message_id}/mark-sent", data=shown_form(card), headers=HTMX)

    assert response.status_code == 200
    assert db_session.get(MessageLog, card.message.message_id).sent_by_user_id == owner.user_id


def test_mark_as_sent_without_htmx_comes_back_to_the_list(db_session, as_member, waiting):
    card = first_card(db_session)

    response = as_member.post(f"/messages/{card.message.message_id}/mark-sent",
                              data={**shown_form(card), "csrf_token": TEST_CSRF_TOKEN}, follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/messages/to-send?notice=marked"
    assert "Marked as sent." in text_of(as_member.get(response.headers["location"]))


@pytest.mark.parametrize("token", [None, "not-the-token"])
def test_mark_as_sent_without_the_right_csrf_token_is_refused(db_session, as_member, waiting, token):
    card = first_card(db_session)
    data = shown_form(card)
    if token:
        data["csrf_token"] = token

    response = as_member.post(f"/messages/{card.message.message_id}/mark-sent", data=data)

    assert response.status_code == 403
    assert db_session.get(MessageLog, card.message.message_id).status == "ready"
    assert audit_rows(db_session, "sms.marked_sent") == []


def test_an_anonymous_visitor_is_sent_to_login(web, waiting, db_session):
    card = first_card(db_session)

    page = web.get("/messages/to-send", follow_redirects=False)
    mark = web.post(f"/messages/{card.message.message_id}/mark-sent", data=shown_form(card),
                    headers=HTMX, follow_redirects=False)

    assert (page.status_code, page.headers["location"]) == (303, "/login")
    assert (mark.status_code, mark.headers["HX-Redirect"]) == (401, "/login")
    assert db_session.get(MessageLog, card.message.message_id).status == "ready"


def test_a_second_press_is_refused_with_a_plain_message(db_session, as_member, waiting):
    card = first_card(db_session)
    url = f"/messages/{card.message.message_id}/mark-sent"
    as_member.post(url, data=shown_form(card), headers=HTMX)

    response = as_member.post(url, data=shown_form(card), headers=HTMX)

    assert response.status_code == 409
    assert "This message is no longer waiting to be sent." in text_of(response)
    assert len(audit_rows(db_session, "sms.marked_sent")) == 1


def test_a_changed_bill_brings_the_card_back_with_the_new_text_and_a_warning(db_session, as_member, owner,
                                                                             waiting):
    card = first_card(db_session)
    # a payment keeps the bill in place, then the correction changes its figures
    db_session.add(Payment(bill_id=waiting.bill.bill_id, amount=1000, payment_date=waiting.reading.reading_date,
                           recorded_by=owner.user_id))
    waiting.bill.amount_paid = 1000
    db_session.commit()
    readings.correct_reading(db_session, owner, waiting.reading.reading_id,
                             {"current_reading": "125", "reading_date": "2025-07-30"})

    response = as_member.post(f"/messages/{card.message.message_id}/mark-sent", data=shown_form(card),
                              headers=HTMX)
    page = text_of(response)

    assert response.status_code == 409
    assert "This bill changed. Open the message again." in page
    assert f'id="message-{card.message.message_id}"' in page
    fresh = first_card(db_session)
    assert fresh.fingerprint != card.fingerprint
    assert f'value="{fresh.fingerprint}"' in page
    assert db_session.get(MessageLog, card.message.message_id).status == "ready"


def test_text_or_a_number_posted_by_the_browser_is_ignored(db_session, as_member, waiting):
    # SMS-S2: only the text the server builds is ever stored
    card = first_card(db_session)

    as_member.post(f"/messages/{card.message.message_id}/mark-sent", headers=HTMX,
                   data={**shown_form(card), "text": "Send me money", "content": "x", "phone": "0711000000"})

    message = db_session.get(MessageLog, card.message.message_id)
    assert message.content == card.text
    assert "Send me money" not in message.content


# ---- the Bills button (SMS-15) ----

def test_the_bills_page_has_the_messages_to_send_button_with_the_count(as_member, waiting):
    page = text_of(as_member.get("/bills"))

    assert 'href="/messages/to-send"' in page
    assert "Messages to send (1)" in page


def test_the_bills_button_shows_with_the_phone_method_even_with_none_waiting(as_member, phone_method):
    assert "Messages to send (0)" in text_of(as_member.get("/bills"))


def test_the_bills_button_is_not_shown_with_automatic_sms_and_nothing_waiting(as_member, sms):
    assert "Messages to send" not in text_of(as_member.get("/bills"))


# ---- the readings result (SMS-14) ----

def test_saving_a_reading_says_the_bill_is_ready_to_send_and_links_to_the_list(db_session, as_member, mary,
                                                                              phone_method):
    data = {"csrf_token": TEST_CSRF_TOKEN, "customer_id": str(mary.customer_id), "current_reading": "120",
            "reading_date": "2025-07-30"}

    page = text_of(as_member.post("/readings", data=data, headers=HTMX))

    assert "Reading saved. The bill is ready to send." in page
    assert '<a href="/messages/to-send">Messages to send (1)</a>' in page
    assert phone_method == []


def test_correcting_a_sent_bill_says_the_correction_is_ready_to_send(db_session, as_member, owner, waiting):
    bills.mark_sent(db_session, owner, first_card(db_session).message.message_id,
                    *shown_form(first_card(db_session)).values())

    page = text_of(as_member.post(f"/readings/{waiting.reading.reading_id}/edit", headers=HTMX,
                                  data={"csrf_token": TEST_CSRF_TOKEN, "current_reading": "125"}))

    assert "Reading corrected. The correction is ready to send." in page


def test_correcting_a_bill_never_sent_says_the_bill_is_ready_to_send(db_session, as_member, waiting):
    page = text_of(as_member.post(f"/readings/{waiting.reading.reading_id}/edit", headers=HTMX,
                                  data={"csrf_token": TEST_CSRF_TOKEN, "current_reading": "125"}))

    assert "Reading corrected. The bill is ready to send." in page


def test_a_failure_to_queue_says_so_plainly_and_the_reading_is_saved(db_session, as_member, mary, phone_method,
                                                                     monkeypatch):
    def broken(*args):
        raise RuntimeError("database gone")

    monkeypatch.setattr(messaging, "_write_ready_row", broken)
    data = {"csrf_token": TEST_CSRF_TOKEN, "customer_id": str(mary.customer_id), "current_reading": "120",
            "reading_date": "2025-07-30"}

    response = as_member.post("/readings", data=data, headers=HTMX)

    assert response.status_code == 200
    assert "Reading saved. The SMS could not be prepared. Open the bill and press Send again." in text_of(response)
    assert readings.latest_reading(db_session, mary.customer_id) is not None


# ---- the bill's Messages card (SMS-16 to SMS-18) ----

def test_a_ready_message_shows_ready_to_send_with_send_by_sms(db_session, as_member, waiting):
    card = first_card(db_session)

    page = text_of(as_member.get(f"/bills/{waiting.bill.bill_id}"))

    assert "Ready to send" in page
    assert f'href="{card.link}"' in page
    assert 'href="/messages/to-send"' in page
    # nothing to send again while a message waits
    assert "/resend" not in page


def test_a_marked_message_says_who_marked_it_and_that_delivery_is_not_confirmed(db_session, as_member, member,
                                                                                waiting):
    card = first_card(db_session)
    as_member.post(f"/messages/{card.message.message_id}/mark-sent", data=shown_form(card), headers=HTMX)

    page = text_of(as_member.get(f"/bills/{waiting.bill.bill_id}"))

    assert re.search(r"Marked as sent by Peter, \d{1,2} \w{3} \d{4}, \d{1,2}:\d{2}(am|pm)\. "
                     r"Delivery is not confirmed\.", page)
    assert f'hx-post="/bills/{waiting.bill.bill_id}/resend"' in page


def test_send_again_on_the_bill_page_prepares_a_new_ready_message(db_session, as_member, waiting):
    card = first_card(db_session)
    as_member.post(f"/messages/{card.message.message_id}/mark-sent", data=shown_form(card), headers=HTMX)

    response = as_member.post(f"/bills/{waiting.bill.bill_id}/resend", headers=HTMX)

    assert response.status_code == 200
    assert "The SMS to Mary Wanjiru is ready to send." in text_of(response)
    statuses = [row.status for row in db_session.scalars(
        select(MessageLog).where(MessageLog.bill_id == waiting.bill.bill_id).order_by(MessageLog.message_id))]
    assert statuses == ["sent", "ready"]


def test_send_again_while_a_message_waits_is_refused_with_a_plain_message(db_session, as_member, waiting):
    response = as_member.post(f"/bills/{waiting.bill.bill_id}/resend", headers=HTMX)

    assert response.status_code == 409
    assert "A message is already waiting to be sent." in text_of(response)


# ---- the dashboard line (DASH-2) ----

def test_the_dashboard_says_how_many_messages_wait(as_member, waiting):
    page = text_of(as_member.get("/"))

    assert "1 message waiting to be sent" in page
    assert page.index("Needs attention") < page.index("1 message waiting to be sent")


def test_the_dashboard_line_is_left_out_when_none_wait(as_member, phone_method):
    assert "waiting to be sent" not in text_of(as_member.get("/"))


def test_the_dashboard_line_shows_only_with_the_phone_method(as_member, waiting, sms_backend):
    sms_backend("africastalking")

    assert "waiting to be sent" not in text_of(as_member.get("/"))


# ---- switching SMS_BACKEND with messages already saved (SMS-22) ----

@pytest.mark.parametrize("backend", ["console", "phone", "africastalking"])
def test_every_page_works_after_switching_backend_with_ready_sent_and_failed_messages(
        db_session, as_member, owner, make_customer, waiting, sms_backend, backend):
    # one ready message, one marked sent, one failed with Africa's Talking, one cancelled
    sent_card = first_card(db_session)
    bills.mark_sent(db_session, owner, sent_card.message.message_id, *shown_form(sent_card).values())
    john = make_customer(first_name="John", last_name="Kamau")
    ready = enter(db_session, owner, john, "50", "2025-07-30")
    grace = make_customer(first_name="Grace", last_name="Njeri")
    failed = enter(db_session, owner, grace, "70", "2025-07-30")
    db_session.add(MessageLog(bill_id=failed.bill.bill_id, customer_id=grace.customer_id, message_type="bill",
                              content="Hi Grace,", status="failed", failure_reason="The sender ID is not registered."))
    db_session.add(MessageLog(customer_id=grace.customer_id, message_type="bill", content="Hi Grace,",
                              status="cancelled"))
    db_session.commit()
    sms_backend(backend)

    for path in ["/", "/bills", f"/bills/{waiting.bill.bill_id}", f"/bills/{ready.bill.bill_id}",
                 f"/bills/{failed.bill.bill_id}", "/messages/to-send", "/readings", "/reports/arrears"]:
        response = as_member.get(path)
        assert response.status_code == 200, path

    # SMS-22: what waits can still be sent from a phone after the switch
    page = text_of(as_member.get("/messages/to-send"))
    assert "John Kamau" in page and "Grace Njeri" in page
    assert "Mary Wanjiru" not in page

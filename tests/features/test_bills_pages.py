"""The Bills screen and the bill page, end to end through the real app (TASK-049 to TASK-051).
The SMS provider is the stand-in from conftest.py."""

import html
import re

import pytest
from sqlalchemy import select

from app.features.readings import service as readings
from app.models import AuditLog, MessageLog
from tests.conftest import TEST_CSRF_TOKEN

HTMX = {"HX-Request": "true", "X-CSRF-Token": TEST_CSRF_TOKEN}


def text_of(response):
    return html.unescape(response.text)


def audit_rows(db_session, event_type):
    return db_session.scalars(select(AuditLog).where(AuditLog.event_type == event_type)).all()


def figures(page):
    """The breakdown on the bill page, as {label: figure}."""
    pairs = re.findall(r"<div[^>]*><dt>(.*?)</dt><dd class=\"mono\">(.*?)</dd></div>", page)
    return dict(pairs)


def enter(db_session, user, customer, current, reading_date):
    return readings.create_reading(db_session, user, customer.customer_id,
                                   {"current_reading": current, "reading_date": reading_date})


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
def grace(make_customer):
    return make_customer(first_name="Grace", last_name="Njeri")


@pytest.fixture
def bills_in_two_months(db_session, owner, mary, grace, sms):
    july = enter(db_session, owner, mary, "120", "2025-07-30")
    august = enter(db_session, owner, mary, "135", "2025-08-30")
    grace_august = enter(db_session, owner, grace, "10", "2025-08-29")
    return july, august, grace_august


# ---- the list (TASK-049) ----

def test_the_list_shows_the_newest_month_first(as_member, bills_in_two_months):
    july, august, grace_august = bills_in_two_months

    response = as_member.get("/bills")

    page = text_of(response)
    assert response.status_code == 200
    assert '<p class="text-label">August 2025</p>' in page
    assert f'href="/bills/{august.bill.bill_id}"' in page and f'href="/bills/{grace_august.bill.bill_id}"' in page
    assert f'href="/bills/{july.bill.bill_id}"' not in page
    assert '<option value="2025-08" selected>August 2025</option>' in page
    # each row: a name, the month, the units, the total, and a status in a word
    assert "Grace Njeri" in page and "Ksh 1,450" in page and "Unpaid" in page


def test_the_list_filters_by_month_and_status(as_member, bills_in_two_months):
    july, august, grace_august = bills_in_two_months

    all_months = text_of(as_member.get("/bills", params={"month": ""}))
    july_only = text_of(as_member.get("/bills", params={"month": "2025-07"}))
    paid = text_of(as_member.get("/bills", params={"month": "", "status": "paid"}))

    assert "All months" in all_months and f'href="/bills/{july.bill.bill_id}"' in all_months
    assert f'href="/bills/{july.bill.bill_id}"' in july_only and f'href="/bills/{august.bill.bill_id}"' not in july_only
    assert "No bills match these filters." in paid


@pytest.mark.parametrize("params", [{"month": "2025-13"}, {"status": "everything"}])
def test_a_filter_not_on_the_list_is_refused(as_member, bills_in_two_months, params):
    response = as_member.get("/bills", params=params)

    assert response.status_code == 422
    assert "Please pick" in text_of(response)


def test_the_list_with_no_bills_says_how_bills_are_made(as_member):
    page = text_of(as_member.get("/bills"))

    assert "No bills yet. A bill is made as soon as a reading is saved." in page
    assert 'href="/readings"' in page


def test_the_list_is_ready_for_stacked_cards_on_a_phone(as_member, bills_in_two_months):
    # DESIGN_SYSTEM.md 7.6: each cell carries its label, and the status sits on the top line
    page = text_of(as_member.get("/bills"))

    assert 'data-label="Total due"' in page and 'data-label="Month"' in page
    assert '<td class="cell-end">' in page


# ---- the bill page (TASK-050) ----

def test_the_breakdown_matches_the_sms_figures_exactly(as_member, db_session, bills_in_two_months, sms):
    july, august, grace_august = bills_in_two_months
    sms_text = db_session.scalars(select(MessageLog).where(MessageLog.bill_id == august.bill.bill_id)).one().content
    sms_lines = dict(line.split(":", 1) for line in sms_text.split("\n")[2:-1])

    page = text_of(as_member.get(f"/bills/{august.bill.bill_id}"))
    shown = figures(page)

    assert shown["Previous reading"] == sms_lines["Previous Reading"] == "120"
    assert shown["Current reading"] == sms_lines["Current Reading"] == "135"
    assert shown["Units used"] == sms_lines["Consumption (m3)"] + " m3"
    assert shown["Water charge (Ksh 130 per unit)"] == "Ksh " + sms_lines["Consumption(130ksh/m3)"]
    assert shown["Fixed charge"] == sms_lines["Fixed Charge per month"].strip()
    assert shown["Previous balance"] == sms_lines["Previous Balance"].strip() == "Ksh 15,750"
    assert shown["Total due"] == sms_lines["Amount Due"].strip() == "Ksh 17,850"
    assert list(shown) == ["Previous reading", "Current reading", "Units used", "Water charge (Ksh 130 per unit)",
                           "Fixed charge", "Previous balance", "Total due"]


def test_no_previous_balance_line_when_nothing_was_carried(as_member, bills_in_two_months):
    july, august, grace_august = bills_in_two_months

    page = text_of(as_member.get(f"/bills/{july.bill.bill_id}"))

    assert "Previous balance" not in page
    assert "<h1>Bill for Mary Wanjiru</h1>" in page
    assert "July 2025" in page and "Unpaid" in page


def test_the_messages_card_lists_every_attempt_with_its_status(as_member, db_session, owner, mary, sms):
    sms.failure = "Could not reach Africa's Talking (ConnectError)"
    first = enter(db_session, owner, mary, "12", "2025-08-30")

    page = text_of(as_member.get(f"/bills/{first.bill.bill_id}"))

    assert "Bill SMS" in page and "Failed" in page
    assert "The SMS did not send. Send it again." in page
    assert f'hx-post="/bills/{first.bill.bill_id}/resend"' in page


def test_a_sent_sms_offers_no_send_again(as_member, bills_in_two_months):
    july, august, grace_august = bills_in_two_months

    page = text_of(as_member.get(f"/bills/{july.bill.bill_id}"))

    assert "Sent" in page
    assert "Send again" not in page


def test_an_unknown_bill_is_a_404(as_member):
    assert as_member.get("/bills/999999").status_code == 404
    assert as_member.get("/bills/not-a-number").status_code == 404


# ---- Send again (TASK-051) ----

@pytest.fixture
def failed_bill(db_session, owner, mary, sms):
    sms.failure = "Could not reach Africa's Talking (ConnectError)"
    result = enter(db_session, owner, mary, "12", "2025-08-30")
    sms.failure = None
    return result.bill


def test_send_again_logs_a_new_attempt_and_updates_the_card(as_member, db_session, member, failed_bill, sms):
    response = as_member.post(f"/bills/{failed_bill.bill_id}/resend", headers=HTMX)

    page = text_of(response)
    assert response.status_code == 200
    assert 'id="messages-card"' in page and "<html" not in page
    assert "The SMS was sent again to Mary Wanjiru." in page
    assert "Send again" not in page
    attempts = db_session.scalars(select(MessageLog).where(MessageLog.bill_id == failed_bill.bill_id)
                                  .order_by(MessageLog.message_id)).all()
    assert [attempt.status for attempt in attempts] == ["failed", "sent"]
    assert audit_rows(db_session, "sms.resend_requested")[0].actor_user_id == member.user_id
    assert len(sms.sent) == 1


def test_send_again_that_fails_again_says_so(as_member, failed_bill, sms):
    sms.failure = "Could not reach Africa's Talking (ConnectError)"

    page = text_of(as_member.post(f"/bills/{failed_bill.bill_id}/resend", headers=HTMX))

    assert "did not send again. Please try again later." in page
    assert "Send again" in page


def test_send_again_without_htmx_comes_back_to_the_bill_page(as_member, failed_bill, sms):
    response = as_member.post(f"/bills/{failed_bill.bill_id}/resend", data={"csrf_token": TEST_CSRF_TOKEN},
                              follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == f"/bills/{failed_bill.bill_id}?notice=sent"
    assert "The SMS was sent again to Mary Wanjiru." in text_of(as_member.get(response.headers["location"]))


def test_send_again_on_a_sent_sms_is_refused_and_recorded(as_member, db_session, bills_in_two_months, sms):
    july, august, grace_august = bills_in_two_months

    response = as_member.post(f"/bills/{july.bill.bill_id}/resend", headers=HTMX)

    assert response.status_code == 409
    assert "Only an SMS that failed can be sent again" in text_of(response)
    assert audit_rows(db_session, "sms.resend_refused")[0].details["reason"] == "last message did not fail"


def test_send_again_for_an_inactive_customer_is_not_offered_and_is_refused(as_member, db_session, mary, failed_bill,
                                                                          sms):
    mary.status = "inactive"
    db_session.flush()

    page = text_of(as_member.get(f"/bills/{failed_bill.bill_id}"))
    response = as_member.post(f"/bills/{failed_bill.bill_id}/resend", headers=HTMX)

    assert "Send again" not in page and "Reactivate the customer first" in page
    assert response.status_code == 422
    assert "Reactivate the customer first." in text_of(response)
    assert sms.sent == []
    assert audit_rows(db_session, "sms.resend_refused")[0].details["reason"] == "customer inactive"


def test_send_again_past_the_daily_limit_is_a_429(as_member, db_session, mary, failed_bill, sms):
    for _ in range(4):
        db_session.add(MessageLog(bill_id=failed_bill.bill_id, customer_id=mary.customer_id, message_type="bill",
                                  content="Hi Mary,", status="failed"))
    db_session.flush()

    response = as_member.post(f"/bills/{failed_bill.bill_id}/resend", headers=HTMX)

    assert response.status_code == 429
    assert "already had 5 SMS today" in text_of(response)
    assert sms.sent == []


def test_send_again_without_the_csrf_token_sends_nothing(as_member, failed_bill, sms):
    response = as_member.post(f"/bills/{failed_bill.bill_id}/resend")

    assert response.status_code == 403
    assert sms.sent == []


# ---- SECURITY.md section 11: no way to send other text to another number ----

def test_send_again_ignores_any_number_or_text_sent_with_it(as_member, failed_bill, mary, sms):
    # SMS-S1, SMS-S2: always the stored text, to the phone stored on the bill's customer
    as_member.post(f"/bills/{failed_bill.bill_id}/resend", headers=HTMX,
                   data={"phone": "0700999888", "to": "+254700999888", "content": "Pay to another number",
                         "message": "Pay to another number"})

    phone, text = sms.sent[0]
    assert phone == mary.phone
    assert text.startswith("C&C H2O WATER SUPPLIES\nHi Mary,") and "another number" not in text

"""The Payments card and the payment routes, end to end through the real app (TASK-054,
TASK-055, TASK-057, PAY-1, PAY-2, PAY-7, PAY-9). The SMS provider is the stand-in from conftest.py."""

import html
import re

import pytest
from sqlalchemy import select

from app.features.payments import service as payments
from app.features.readings import service as readings
from app.models import AuditLog, Bill, Payment
from tests.conftest import TEST_CSRF_TOKEN
from tests.test_permissions import app_routes

HTMX = {"HX-Request": "true", "X-CSRF-Token": TEST_CSRF_TOKEN}
CODE = "QGH7X2K9LM"


def text_of(response):
    return html.unescape(response.text)


def audit_rows(db_session, event_type):
    return db_session.scalars(select(AuditLog).where(AuditLog.event_type == event_type)
                              .order_by(AuditLog.audit_id)).all()


def payment_form(amount, code="", payment_date="2025-08-05"):
    return {"amount": amount, "payment_date": payment_date, "mpesa_reference": code}


def bill_in(db_session, owner, customer, current, reading_date):
    """A bill through the real flow: 10 units on a first reading is Ksh 1,450."""
    return readings.create_reading(db_session, owner, customer.customer_id,
                                   {"current_reading": current, "reading_date": reading_date}).bill


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
def bill(db_session, owner, mary, sms):
    return bill_in(db_session, owner, mary, "10", "2025-07-30")


def post_payment(client, bill, form, headers=None):
    return client.post(f"/bills/{bill.bill_id}/payments", data={"csrf_token": TEST_CSRF_TOKEN, **form},
                       headers=headers or {})


# ---- the card on the bill page (TASK-054) ----

def test_the_latest_bill_has_a_record_payment_form(as_member, bill):
    response = as_member.get(f"/bills/{bill.bill_id}")

    page = text_of(response)
    assert response.status_code == 200
    assert '<h2 class="card-title" id="payments-title">Payments</h2>' in page
    assert "Amount received (Ksh)" in page and "M-Pesa code (optional)" in page and "Record payment" in page
    assert f'hx-post="/bills/{bill.bill_id}/payments"' in page
    assert f'hx-get="/bills/{bill.bill_id}/payments/preview"' in page
    assert 'name="csrf_token"' in page
    # the date starts at today, and cannot be later
    today = payments.today().isoformat()
    assert f'value="{today}" max="{today}"' in page
    assert "No payment has been recorded for this bill yet." in page
    assert "Paid so far" in page and "Still owed" in page
    assert "/static/payments/payments.css" in page
    # the live line is never an empty gap before anything is typed (TASK-068)
    assert "Type the amount to see what will still be owed." in page


def test_an_older_bill_shows_carried_forward_and_no_form(as_member, db_session, owner, mary, bill):
    august = bill_in(db_session, owner, mary, "20", "2025-08-30")

    page = text_of(as_member.get(f"/bills/{bill.bill_id}"))

    assert "Carried forward." in page
    assert f'href="/bills/{august.bill_id}"' in page
    assert "Record payment" not in page and 'name="amount"' not in page


def test_a_paid_bill_shows_no_form(as_member, db_session, owner, bill):
    payments.record_payment(db_session, owner, bill.bill_id, payment_form("1450"))

    page = text_of(as_member.get(f"/bills/{bill.bill_id}"))

    assert "This bill is paid. Nothing more is owed on it." in page
    assert 'name="amount"' not in page


def test_the_card_lists_the_payments_so_far(as_member, db_session, owner, bill):
    payments.record_payment(db_session, owner, bill.bill_id, payment_form("500", CODE, "2025-08-04"))
    payments.record_payment(db_session, owner, bill.bill_id, payment_form("100"))

    page = text_of(as_member.get(f"/bills/{bill.bill_id}"))

    assert "4 Aug 2025" in page and "5 Aug 2025" in page
    assert f'M-Pesa code <span class="mono">{CODE}</span>' in page and "No M-Pesa code" in page
    assert '<dt>Paid so far</dt><dd class="mono">Ksh 600</dd>' in page
    assert '<dt>Still owed</dt><dd class="mono">Ksh 850</dd>' in page


# ---- recording from the page, with htmx (TASK-055, PAY-7) ----

def test_a_payment_swaps_the_card_and_the_badge_without_a_reload(as_member, db_session, member, mary, bill):
    response = as_member.post(f"/bills/{bill.bill_id}/payments", data=payment_form("500", CODE), headers=HTMX)

    page = text_of(response)
    assert response.status_code == 200
    # a fragment, not a whole page
    assert "<html" not in page and 'id="payment-card"' in page
    # the message names the result (DESIGN_SYSTEM.md section 8)
    assert "Payment saved. Mary's bill is now Partial." in page
    assert 'role="status" tabindex="-1" autofocus' in page
    # the totals and the list are the new ones
    assert '<dt>Paid so far</dt><dd class="mono">Ksh 500</dd>' in page
    assert '<dt>Still owed</dt><dd class="mono">Ksh 950</dd>' in page
    assert f'M-Pesa code <span class="mono">{CODE}</span>' in page
    # the status badge at the top is replaced out of band, and announced
    assert re.search(r'<div class="bill-status" id="bill-status" hx-swap-oob="true">\s*'
                     r'<span class="badge badge-partial" role="status">', page)
    # the form is empty again, for another payment
    assert re.search(r'name="amount"\s+value=""', page)
    assert re.search(r'name="mpesa_reference"\s+value=""', page)
    saved = db_session.scalars(select(Payment)).one()
    assert (saved.recorded_by, saved.bill_id, str(saved.amount)) == (member.user_id, bill.bill_id, "500.00")


def test_paying_in_full_says_paid_and_removes_the_form(as_member, bill):
    page = text_of(post_payment(as_member, bill, payment_form("1,450"), HTMX))

    assert "Payment saved. Mary's bill is now Paid." in page
    assert 'class="badge badge-paid" role="status"' in page
    assert "This bill is paid. Nothing more is owed on it." in page
    assert 'name="amount"' not in page


def test_the_bill_page_header_has_the_badge_the_swap_replaces(as_member, bill):
    page = text_of(as_member.get(f"/bills/{bill.bill_id}"))

    assert re.search(r'<div class="bill-status" id="bill-status">\s*<span class="badge badge-unpaid">', page)
    # the page itself has no swap markup, only the htmx answer does
    assert "hx-swap-oob" not in page


def test_a_payment_saved_without_htmx_goes_back_to_the_bill_page(as_member, db_session, bill):
    response = post_payment(as_member, bill, payment_form("500", CODE))

    assert response.status_code == 200 and response.history[0].status_code == 303
    assert str(response.url).endswith(f"/bills/{bill.bill_id}?notice=payment_saved")
    page = text_of(response)
    assert "<html" in page and "Payment saved. Mary's bill is now Partial." in page
    # a refresh of the page does not record it again
    as_member.get(str(response.url))
    assert len(db_session.scalars(select(Payment)).all()) == 1


def test_the_plain_post_redirects_to_the_bill_page_with_a_notice(as_member, bill):
    response = as_member.post(f"/bills/{bill.bill_id}/payments",
                              data={"csrf_token": TEST_CSRF_TOKEN, **payment_form("500")}, follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == f"/bills/{bill.bill_id}?notice=payment_saved"


def test_an_unknown_notice_in_the_address_shows_no_message(as_member, bill):
    page = text_of(as_member.get(f"/bills/{bill.bill_id}?notice=<b>hello</b>"))

    assert "Payment saved." not in page and "<b>hello</b>" not in page


# ---- refusals, shown beside the field with what was typed kept ----

def test_a_zero_amount_is_refused_beside_the_amount(as_member, db_session, bill):
    response = post_payment(as_member, bill, payment_form("0", CODE), HTMX)

    page = text_of(response)
    assert response.status_code == 422
    assert 'id="payment-card"' in page and "<html" not in page
    assert '<p class="field-error" id="field-amount-error">' in page
    assert "The amount received must be more than 0, for example 1,600." in page
    assert 'aria-invalid="true"' in page
    # what was typed is kept, and no badge swap is sent
    assert f'value="{CODE}"' in page and "hx-swap-oob" not in page
    assert db_session.scalars(select(Payment)).all() == []
    assert audit_rows(db_session, "payment.rejected")[0].details["reason"].startswith("The amount received")


def test_a_duplicate_code_is_a_409_beside_the_code(as_member, db_session, bill):
    post_payment(as_member, bill, payment_form("500", CODE), HTMX)

    response = post_payment(as_member, bill, payment_form("200", CODE.lower()), HTMX)

    page = text_of(response)
    assert response.status_code == 409
    assert '<p class="field-error" id="field-mpesa_reference-error">' in page
    assert f"The M-Pesa code {CODE} is already recorded: a payment of Ksh 500 on 5 Aug 2025" in page
    assert len(db_session.scalars(select(Payment)).all()) == 1


def test_a_refused_plain_post_shows_the_whole_bill_page_again(as_member, db_session, bill):
    response = post_payment(as_member, bill, payment_form("abc", CODE))

    page = text_of(response)
    assert response.status_code == 422
    assert "<html" in page and "<h1>Bill for Mary Wanjiru</h1>" in page
    assert "Please enter the amount received as a number, for example 1,600." in page
    assert f'value="{CODE}"' in page
    assert db_session.scalars(select(Payment)).all() == []


def test_what_was_typed_is_shown_back_safely(as_member, bill):
    page = post_payment(as_member, bill, payment_form('<script>alert(1)</script>', "<b>x</b>"), HTMX).text

    # OUT-1: autoescape stays on
    assert "<script>alert(1)" not in page and "<b>x</b>" not in page
    assert "&lt;script&gt;" in page


def test_an_older_bill_refuses_a_payment_with_a_message_and_a_422(as_member, db_session, owner, mary, bill):
    bill_in(db_session, owner, mary, "20", "2025-08-30")

    response = post_payment(as_member, bill, payment_form("500"), HTMX)

    assert response.status_code == 422
    assert "carried forward into a newer bill" in text_of(response)
    assert "Mary" not in audit_rows(db_session, "payment.rejected")[0].details["reason"]
    assert db_session.scalars(select(Payment)).all() == []


def test_an_unknown_bill_is_a_404(as_member):
    missing = as_member.post("/bills/999999/payments", data=payment_form("500"), headers=HTMX)
    not_a_number = as_member.post("/bills/not-a-number/payments", data=payment_form("500"), headers=HTMX)

    assert missing.status_code == 404
    assert not_a_number.status_code == 404


def test_a_payment_without_the_csrf_token_saves_nothing(as_member, db_session, bill):
    response = as_member.post(f"/bills/{bill.bill_id}/payments", data=payment_form("500"))

    assert response.status_code == 403
    assert db_session.scalars(select(Payment)).all() == []


def test_the_amount_paid_and_status_cannot_be_set_from_the_form(as_member, db_session, member, bill):
    # IN-7: only the three boxes of the form count
    post_payment(as_member, bill, {**payment_form("100"), "amount_paid": "99999", "status": "paid",
                                   "recorded_by": "999999", "method": "cash", "bill_id": "999999"}, HTMX)

    saved = db_session.scalars(select(Payment)).one()
    db_session.refresh(bill)
    assert (saved.bill_id, saved.method, saved.recorded_by) == (bill.bill_id, "mpesa", member.user_id)
    assert (str(bill.amount_paid), bill.status) == ("100.00", "partial")


def test_the_owner_and_a_member_both_record_payments(web, db_session, owner, member, bill, sign_in_as):
    sign_in_as(web, owner)
    assert post_payment(web, bill, payment_form("100"), HTMX).status_code == 200
    sign_in_as(web, member)
    assert post_payment(web, bill, payment_form("100"), HTMX).status_code == 200

    recorded_by = db_session.scalars(select(Payment.recorded_by).order_by(Payment.payment_id)).all()
    assert recorded_by == [owner.user_id, member.user_id]


# ---- the live line (PAY-2) ----

def preview(client, bill, amount):
    return client.get(f"/bills/{bill.bill_id}/payments/preview", params={"amount": amount}, headers=HTMX)


def test_the_live_line_says_what_would_still_be_owed(as_member, bill):
    response = preview(as_member, bill, "500")

    assert response.status_code == 200
    assert '<span class="mono">Ksh 950</span> will still be owed.' in text_of(response)
    assert 'id="owed-line"' in response.text and 'role="status"' in response.text and "<html" not in response.text


def test_the_live_line_says_when_nothing_would_be_owed(as_member, bill):
    page = text_of(preview(as_member, bill, "1450"))
    over = text_of(preview(as_member, bill, "2,000"))

    assert "Nothing will still be owed." in page and "more than the bill" not in page
    assert "Nothing will still be owed." in over
    assert 'That is <span class="mono">Ksh 550</span> more than the bill. The extra is saved but not carried forward.' in over


@pytest.mark.parametrize("amount,message", [("0", "must be more than 0"), ("abc", "as a number"),
                                            ("", "Please enter the amount received")])
def test_the_live_line_shows_a_problem_as_it_is_typed(as_member, db_session, bill, amount, message):
    response = preview(as_member, bill, amount)

    assert response.status_code == 422
    assert f'<span class="owed-problem">' in response.text and message in text_of(response)
    # looking is not recording
    assert audit_rows(db_session, "payment.rejected") == []


def test_the_live_line_for_an_unknown_bill_is_a_404(as_member):
    response = as_member.get("/bills/999999/payments/preview", params={"amount": "500"}, headers=HTMX)

    assert response.status_code == 404


# ---- no way to edit or delete a payment (TASK-057, PAY-9, MON-1) ----

def test_no_route_edits_or_deletes_a_payment(web):
    routes = app_routes(web.app.routes)

    payment_routes = {(method, path) for method, path in routes if "payment" in path}
    # the payment history report only reads
    assert payment_routes == {("GET", "/bills/{bill_id}/payments/preview"), ("POST", "/bills/{bill_id}/payments"),
                              ("GET", "/reports/payments")}
    # and nothing anywhere changes data with PUT, PATCH or DELETE
    assert not [route for route in routes if route[0] in ("PUT", "PATCH", "DELETE")]


def test_a_recorded_payment_is_never_changed_by_another_one(as_member, db_session, bill):
    post_payment(as_member, bill, payment_form("500", CODE, "2025-08-04"), HTMX)
    first = db_session.scalars(select(Payment)).one()
    before = (first.payment_id, first.amount, first.payment_date, first.mpesa_reference, first.recorded_by)

    post_payment(as_member, bill, payment_form("100"), HTMX)
    post_payment(as_member, bill, payment_form("0"), HTMX)

    db_session.refresh(first)
    assert (first.payment_id, first.amount, first.payment_date, first.mpesa_reference, first.recorded_by) == before
    assert len(db_session.scalars(select(Payment)).all()) == 2


# ---- every payment event is in the audit log (TASK-057, AUD-1) ----

def test_every_payment_event_is_in_the_audit_log_with_the_request_id(as_member, db_session, member, mary, bill):
    saved = post_payment(as_member, bill, payment_form("1450", CODE), HTMX)
    refused = post_payment(as_member, bill, payment_form("0"), HTMX)

    recorded = audit_rows(db_session, "payment.recorded")
    changed = audit_rows(db_session, "bill.status_changed")
    rejected = audit_rows(db_session, "payment.rejected")
    assert len(recorded) == 1 and len(changed) == 1 and len(rejected) == 1
    assert recorded[0].request_id == saved.headers["X-Request-ID"] == changed[0].request_id
    assert rejected[0].request_id == refused.headers["X-Request-ID"]
    assert recorded[0].actor_user_id == member.user_id and recorded[0].customer_id == mary.customer_id
    assert (changed[0].details["old_status"], changed[0].details["new_status"]) == ("unpaid", "paid")
    assert db_session.get(Bill, bill.bill_id).status == "paid"

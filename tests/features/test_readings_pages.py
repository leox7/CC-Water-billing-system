"""The Readings screen, end to end through the real app (TASK-047, TASK-048, RD-1 to RD-6,
PRD 4.4). The SMS provider is the stand-in from conftest.py."""

import html
import re
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.features.readings import service as readings
from app.models import Bill, MeterReading
from tests.conftest import TEST_CSRF_TOKEN

HTMX = {"HX-Request": "true", "X-CSRF-Token": TEST_CSRF_TOKEN}


def reading_form(customer, current, reading_date="2025-08-30"):
    return {"csrf_token": TEST_CSRF_TOKEN, "customer_id": str(customer.customer_id), "current_reading": current,
            "reading_date": reading_date}


def text_of(response):
    return html.unescape(response.text)


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
def mary_in_july(db_session, owner, mary, sms):
    return readings.create_reading(db_session, owner, mary.customer_id,
                                   {"current_reading": "120", "reading_date": "2025-07-30"})


# ---- step 1: pick a customer ----

def test_the_picker_lists_active_customers_not_read_yet_first(as_member, db_session, owner, mary, grace,
                                                              make_customer, sms):
    make_customer(first_name="Gone", status="inactive")
    readings.create_reading(db_session, owner, grace.customer_id, {"current_reading": "5", "reading_date": ""})

    response = as_member.get("/readings")

    page = text_of(response)
    assert response.status_code == 200
    assert "1 of 2 read" in page
    assert page.index("Mary Wanjiru") < page.index("Grace Njeri")
    assert "Not read yet" in page and "Read this month" in page
    assert "Gone" not in page.split("Recent readings")[0]
    assert f'href="/readings/new/{mary.customer_id}"' in response.text


def test_the_picker_searches(as_member, mary, grace):
    response = as_member.get("/readings", params={"search": "Grace"})

    page = text_of(response)
    assert "Grace Njeri" in page and "Mary Wanjiru" not in page
    assert "1 customer found." in page


def test_a_search_that_is_too_long_is_refused_on_the_page(as_member, mary):
    response = as_member.get("/readings", params={"search": "x" * 101})

    assert response.status_code == 422
    assert "Please search with 100 characters or fewer." in text_of(response)


def test_the_picker_with_no_customers_offers_to_add_one(as_member):
    page = text_of(as_member.get("/readings"))

    assert "No active customers yet." in page
    assert 'href="/customers/new"' in page


# ---- step 2: enter the reading ----

def test_the_form_shows_last_month_filled_in_and_today_as_the_date(as_member, mary, mary_in_july):
    response = as_member.get(f"/readings/new/{mary.customer_id}")

    page = text_of(response)
    assert response.status_code == 200
    assert "<h1>Mary Wanjiru</h1>" in page
    assert "Last month" in page and '<p class="last-reading-figure mono">120</p>' in page
    assert "This month's reading" in page
    assert f'value="{readings.today().isoformat()}"' in page
    assert 'inputmode="decimal"' in page
    assert "Save reading" in page


def test_a_first_reading_asks_for_the_previous_reading_and_opening_balance(as_member, mary):
    # CUS-5: both boxes, filled with 0, instead of the read-only panel
    page = text_of(as_member.get(f"/readings/new/{mary.customer_id}"))

    assert "This is Mary's first reading in the app." in page
    assert re.search(r'name="previous_reading"\s+value="0"', page) and re.search(r'name="opening_balance"\s+value="0"', page)
    assert "Opening balance (Ksh)" in page
    assert 'class="last-reading"' not in page


def test_a_later_reading_has_no_boxes_for_them(as_member, mary, mary_in_july):
    page = text_of(as_member.get(f"/readings/new/{mary.customer_id}"))

    assert 'name="previous_reading"' not in page and 'name="opening_balance"' not in page


def test_saving_a_first_reading_with_paper_figures(as_member, db_session, mary, sms):
    data = reading_form(mary, "3420")
    data.update({"previous_reading": "3400", "opening_balance": "1200"})

    response = as_member.post("/readings", data=data, headers=HTMX)

    assert response.status_code == 200
    bill = db_session.scalars(select(Bill)).one()
    assert (bill.previous_balance, bill.total_due) == (Decimal("1200.00"), Decimal("3950.00"))
    assert "Ksh 3,950" in text_of(response)


def test_a_bad_opening_balance_comes_back_beside_its_box(as_member, db_session, mary, sms):
    data = reading_form(mary, "12")
    data.update({"opening_balance": "-5"})

    response = as_member.post("/readings", data=data, headers=HTMX)

    assert response.status_code == 422
    assert 'id="field-opening_balance-error"' in response.text
    assert "The opening balance cannot be less than 0." in text_of(response)
    assert 'value="-5"' in response.text


def test_the_live_line_uses_the_typed_previous_reading(as_member, mary):
    response = as_member.get("/readings/preview", params={"customer_id": mary.customer_id,
                                                          "current_reading": "3420", "previous_reading": "3400"})

    assert "20</span> units used." in response.text


def test_the_fix_page_of_a_first_reading_can_change_its_starting_figures(as_member, db_session, owner, mary, sms):
    first = readings.create_reading(db_session, owner, mary.customer_id,
                                    {"current_reading": "3420", "reading_date": "2025-07-30",
                                     "previous_reading": "3400", "opening_balance": "1200"})

    page = text_of(as_member.get(f"/readings/{first.reading.reading_id}/edit"))
    response = as_member.post(f"/readings/{first.reading.reading_id}/edit", headers=HTMX,
                              data={"csrf_token": TEST_CSRF_TOKEN, "current_reading": "3420",
                                    "previous_reading": "3410", "opening_balance": "1200"})

    assert re.search(r'name="previous_reading"\s+value="3400"', page)
    assert re.search(r'name="opening_balance"\s+value="1200"', page)
    assert response.status_code == 200
    assert "Reading corrected." in text_of(response)
    db_session.expire_all()
    assert db_session.get(type(first.reading), first.reading.reading_id).previous_reading == Decimal("3410.00")


def test_the_form_says_when_the_customer_was_already_read_this_month(as_member, db_session, owner, mary, sms):
    today = readings.create_reading(db_session, owner, mary.customer_id, {"current_reading": "7", "reading_date": ""})

    page = text_of(as_member.get(f"/readings/new/{mary.customer_id}"))

    assert "Mary already has a reading for" in page
    assert f'href="/readings/{today.reading.reading_id}/edit"' in page


def test_an_inactive_customer_has_no_reading_form(as_member, make_customer):
    gone = make_customer(first_name="Gone", status="inactive")

    page = text_of(as_member.get(f"/readings/new/{gone.customer_id}"))

    assert "is inactive, so no bill can be made" in page
    assert 'action="/readings"' not in page


def test_an_unknown_customer_is_a_404(as_member):
    assert as_member.get("/readings/new/999999").status_code == 404


# ---- step 3: the live "units used" line ----

def test_the_live_line_says_how_many_units(as_member, mary, mary_in_july):
    response = as_member.get("/readings/preview", params={"customer_id": mary.customer_id, "current_reading": "144"},
                             headers=HTMX)

    assert response.status_code == 200
    assert "That is" in response.text and "24</span> units used." in response.text


def test_the_live_line_says_one_unit(as_member, mary, mary_in_july):
    response = as_member.get("/readings/preview", params={"customer_id": mary.customer_id, "current_reading": "121"})

    assert "1</span> unit used." in response.text


def test_the_live_line_shows_a_too_low_reading_straight_away(as_member, mary, mary_in_july):
    response = as_member.get("/readings/preview", params={"customer_id": mary.customer_id, "current_reading": "100"},
                             headers=HTMX)

    assert response.status_code == 422
    assert "This is lower than last month's reading (120). Please check the number." in text_of(response)
    assert 'id="units-line"' in response.text


# ---- steps 4 and 5: save, then move on ----

def test_saving_a_reading_says_the_bill_was_sent_and_offers_the_next_customer(as_member, db_session, member, mary,
                                                                               grace, sms):
    response = as_member.post("/readings", data=reading_form(mary, "12", ""), headers=HTMX)

    page = text_of(response)
    assert response.status_code == 200
    assert "Reading saved. The bill was sent to Mary Wanjiru." in page
    assert f'href="/readings/new/{grace.customer_id}"' in page and "Next customer: Grace Njeri" in page
    reading = db_session.scalars(select(MeterReading).where(MeterReading.customer_id == mary.customer_id)).one()
    assert reading.recorded_by == member.user_id
    bill = db_session.scalars(select(Bill).where(Bill.reading_id == reading.reading_id)).one()
    assert f'href="/bills/{bill.bill_id}"' in page
    assert "Ksh 1,710" in page
    assert len(sms.sent) == 1


def test_after_the_last_customer_there_is_no_next_customer(as_member, mary, sms):
    page = text_of(as_member.post("/readings", data=reading_form(mary, "12", ""), headers=HTMX))

    assert "Next customer" not in page
    assert "Every active customer has a reading for this month." in page


def test_a_failed_sms_still_saves_and_says_to_send_it_again(as_member, db_session, mary, sms):
    sms.failure = "Could not reach Africa's Talking (ConnectError)"

    response = as_member.post("/readings", data=reading_form(mary, "12"), headers=HTMX)

    assert response.status_code == 200
    assert "Reading saved. The SMS to Mary Wanjiru did not send. Send it again from the bill." in text_of(response)
    assert db_session.scalars(select(MeterReading)).all() != []


def test_a_crash_in_the_sms_code_is_never_a_500_page(as_member, db_session, mary, monkeypatch):
    # SMS-4a
    def broken(settings, phone, text):
        raise RuntimeError("a bug")

    monkeypatch.setattr("app.features.messaging.providers.send_sms", broken)

    response = as_member.post("/readings", data=reading_form(mary, "12"), headers=HTMX)

    assert response.status_code == 200
    assert "did not send. Send it again from the bill." in text_of(response)


def test_a_too_low_reading_comes_back_beside_the_field_with_what_was_typed(as_member, db_session, mary, mary_in_july):
    response = as_member.post("/readings", data=reading_form(mary, "119"), headers=HTMX)

    page = text_of(response)
    assert response.status_code == 422
    assert 'id="reading-panel"' in page
    assert 'aria-invalid="true"' in page
    assert "This is lower than last month's reading (120). Please check the number." in page
    assert 'value="119"' in page and 'value="2025-08-30"' in page
    assert len(db_session.scalars(select(MeterReading)).all()) == 1


def test_a_second_reading_for_the_month_is_a_409(as_member, mary, mary_in_july):
    response = as_member.post("/readings", data=reading_form(mary, "130", "2025-07-31"), headers=HTMX)

    assert response.status_code == 409
    assert "already has a reading for July 2025" in text_of(response)


def test_without_htmx_the_answer_is_a_whole_page(as_member, mary, sms):
    response = as_member.post("/readings", data=reading_form(mary, "12"))

    assert response.status_code == 200
    assert "<html" in response.text
    assert "Reading saved. The bill was sent to Mary Wanjiru." in text_of(response)


def test_a_save_for_an_unknown_customer_is_a_404(as_member, sms):
    data = {"csrf_token": TEST_CSRF_TOKEN, "customer_id": "999999", "current_reading": "12", "reading_date": ""}

    assert as_member.post("/readings", data=data, headers=HTMX).status_code == 404


def test_a_save_without_the_csrf_token_changes_nothing(as_member, db_session, mary, sms):
    data = reading_form(mary, "12")
    del data["csrf_token"]

    response = as_member.post("/readings", data=data)

    assert response.status_code == 403
    assert db_session.scalars(select(MeterReading)).all() == []


# ---- recent readings and Fix (TASK-048) ----

def test_recent_readings_offer_fix_on_the_latest_reading_only(as_member, db_session, owner, mary, mary_in_july, sms):
    august = readings.create_reading(db_session, owner, mary.customer_id,
                                     {"current_reading": "135", "reading_date": "2025-08-30"})

    response = as_member.get("/readings")

    assert "Recent readings" in response.text
    assert f'href="/readings/{august.reading.reading_id}/edit"' in response.text
    assert f'href="/readings/{mary_in_july.reading.reading_id}/edit"' not in response.text


def test_the_fix_page_says_a_sent_bill_is_corrected_in_place(as_member, db_session, owner, mary, mary_in_july, sms):
    page = text_of(as_member.get(f"/readings/{mary_in_july.reading.reading_id}/edit"))

    assert "The bill was already sent." in page
    assert "Mary gets a correction SMS." in page
    assert 'value="120"' in page
    assert "Delete reading" not in page
    assert "cannot be deleted, because its bill was sent or has a payment" in page


def test_fixing_a_sent_bill_corrects_it_in_place_and_says_so(as_member, db_session, mary, mary_in_july, sms):
    reading_id = mary_in_july.reading.reading_id

    response = as_member.post(f"/readings/{reading_id}/edit",
                              data={"csrf_token": TEST_CSRF_TOKEN, "current_reading": "125"}, headers=HTMX)

    page = text_of(response)
    assert response.status_code == 200
    assert "Reading corrected. A correction SMS was sent to Mary Wanjiru." in page
    assert "Payments already recorded are kept." in page
    db_session.expire_all()
    assert db_session.get(MeterReading, reading_id).current_reading == Decimal("125.00")
    assert "We corrected your meter reading." in sms.sent[-1][1]


def test_fixing_a_bill_not_sent_replaces_it_and_says_so(as_member, db_session, owner, mary, mary_in_july, sms):
    sms.failure = "Could not reach Africa's Talking (ConnectError)"
    august = readings.create_reading(db_session, owner, mary.customer_id,
                                     {"current_reading": "135", "reading_date": "2025-08-30"})
    sms.failure = None

    page_before = text_of(as_member.get(f"/readings/{august.reading.reading_id}/edit"))
    response = as_member.post(f"/readings/{august.reading.reading_id}/edit",
                              data={"csrf_token": TEST_CSRF_TOKEN, "current_reading": "140"}, headers=HTMX)

    assert "was not sent and has no payment" in page_before and "Delete reading" in page_before
    assert "Reading corrected. The bill was sent to Mary Wanjiru." in text_of(response)
    assert "The old reading and its bill were replaced by new ones." in text_of(response)
    assert db_session.get(MeterReading, august.reading.reading_id) is None


def test_a_refused_fix_comes_back_beside_the_field(as_member, mary, mary_in_july):
    response = as_member.post(f"/readings/{mary_in_july.reading.reading_id}/edit",
                              data={"csrf_token": TEST_CSRF_TOKEN, "current_reading": "-1"}, headers=HTMX)

    assert response.status_code == 422
    assert "The reading cannot be less than 0." in text_of(response)
    assert 'value="-1"' in response.text and 'id="fix-panel"' in response.text


def test_an_older_reading_cannot_be_fixed_from_the_page(as_member, db_session, owner, mary, mary_in_july, sms):
    readings.create_reading(db_session, owner, mary.customer_id, {"current_reading": "135", "reading_date": "2025-08-30"})

    page = text_of(as_member.get(f"/readings/{mary_in_july.reading.reading_id}/edit"))
    refused = as_member.post(f"/readings/{mary_in_july.reading.reading_id}/edit",
                             data={"csrf_token": TEST_CSRF_TOKEN, "current_reading": "121"}, headers=HTMX)

    assert "Only the latest reading can be corrected." in page
    assert "Save correction" not in page
    assert refused.status_code == 422


def test_an_inactive_customer_s_reading_cannot_be_fixed_from_the_page(as_member, db_session, mary, mary_in_july):
    mary.status = "inactive"
    db_session.flush()

    page = text_of(as_member.get(f"/readings/{mary_in_july.reading.reading_id}/edit"))

    assert "Reactivate the customer first." in page
    assert "Save correction" not in page


def test_deleting_a_reading_goes_back_to_the_form_to_enter_it_again(as_member, db_session, owner, mary, mary_in_july,
                                                                    sms):
    sms.failure = "Could not reach Africa's Talking (ConnectError)"
    august = readings.create_reading(db_session, owner, mary.customer_id,
                                     {"current_reading": "135", "reading_date": "2025-08-30"})

    response = as_member.post(f"/readings/{august.reading.reading_id}/delete", headers=HTMX)

    assert response.headers["HX-Redirect"] == f"/readings/new/{mary.customer_id}?notice=deleted"
    assert db_session.get(MeterReading, august.reading.reading_id) is None
    assert "Reading deleted. You can enter it again." in text_of(as_member.get(response.headers["HX-Redirect"]))


def test_deleting_without_htmx_redirects(as_member, db_session, owner, mary, mary_in_july, sms):
    sms.failure = "Could not reach Africa's Talking (ConnectError)"
    august = readings.create_reading(db_session, owner, mary.customer_id,
                                     {"current_reading": "135", "reading_date": "2025-08-30"})

    response = as_member.post(f"/readings/{august.reading.reading_id}/delete", data={"csrf_token": TEST_CSRF_TOKEN},
                              follow_redirects=False)

    assert response.status_code == 303


def test_a_reading_whose_bill_was_sent_is_never_deleted_from_the_page(as_member, db_session, mary, mary_in_july):
    response = as_member.post(f"/readings/{mary_in_july.reading.reading_id}/delete", headers=HTMX)

    assert response.status_code == 422
    assert "kept for the record" in text_of(response)
    assert db_session.get(MeterReading, mary_in_july.reading.reading_id) is not None


def test_an_unknown_reading_is_a_404(as_member):
    assert as_member.get("/readings/999999/edit").status_code == 404
    assert as_member.post("/readings/999999/edit", data={"csrf_token": TEST_CSRF_TOKEN,
                                                         "current_reading": "5"}).status_code == 404


# ---- SECURITY.md section 7 ----

def test_only_the_named_fields_are_read_from_the_reading_form(as_member, db_session, member, owner, mary,
                                                              mary_in_july, sms):
    # IN-7: units and who recorded it are never taken from the form, and after the first
    # reading neither are the previous reading or an opening balance (RD-2)
    data = reading_form(mary, "132")
    data.update({"units_consumed": "999", "previous_reading": "5", "opening_balance": "777",
                 "recorded_by": str(owner.user_id), "billing_period": "2020-01", "total_due": "1"})

    as_member.post("/readings", data=data, headers=HTMX)

    reading = db_session.scalars(select(MeterReading).where(MeterReading.billing_period == "2025-08")).one()
    assert (reading.previous_reading, reading.units_consumed) == (Decimal("120.00"), Decimal("12.00"))
    assert (reading.recorded_by, reading.billing_period) == (member.user_id, "2025-08")
    bill = db_session.scalars(select(Bill).where(Bill.reading_id == reading.reading_id)).one()
    # 12 x 130 + 150 + July's unpaid 15,750
    assert (bill.previous_balance, bill.total_due) == (Decimal("15750.00"), Decimal("17460.00"))


@pytest.mark.parametrize("customer_id", ["abc", "", "1.5"])
def test_a_customer_id_that_is_not_a_number_is_refused(as_member, db_session, customer_id, sms):
    # REQ-1
    data = {"csrf_token": TEST_CSRF_TOKEN, "customer_id": customer_id, "current_reading": "12", "reading_date": ""}

    response = as_member.post("/readings", data=data, headers=HTMX)

    assert response.status_code == 422
    assert db_session.scalars(select(MeterReading)).all() == []


def test_ids_in_the_address_must_be_numbers(as_member):
    assert as_member.get("/readings/new/abc").status_code == 404
    assert as_member.get("/readings/abc/edit").status_code == 404
    assert as_member.get("/readings/preview", params={"customer_id": "abc"}).status_code == 422


# ---- the billing month under the date (RD-5) ----

def test_the_form_names_the_month_the_date_falls_in(as_member, mary):
    page = text_of(as_member.get(f"/readings/new/{mary.customer_id}"))

    month = readings.today().strftime("%B %Y")
    assert f'This reading will be billed to <strong>{month}</strong>' in page
    assert 'id="period-line"' in page
    # the date box asks the server again whenever the date changes
    assert 'hx-get="/readings/period"' in page and 'hx-target="#period-line"' in page


def test_the_line_updates_for_the_date_picked(as_member):
    response = as_member.get("/readings/period", params={"reading_date": "2025-07-31"}, headers=HTMX)

    assert response.status_code == 200
    assert 'id="period-line"' in response.text and "<html" not in response.text
    assert "This reading will be billed to <strong>July 2025</strong>" in response.text


def test_a_cleared_date_means_today(as_member):
    response = as_member.get("/readings/period", params={"reading_date": ""}, headers=HTMX)

    assert f"billed to <strong>{readings.today().strftime('%B %Y')}</strong>" in response.text


@pytest.mark.parametrize("bad_date,message", [
    ("2025-02-30", "Please enter the reading date, for example 2026-08-30."),
    ("2999-01-01", "The reading date cannot be in the future. Use today or an earlier day."),
])
def test_a_date_that_cannot_be_used_is_said_on_the_line(as_member, bad_date, message):
    response = as_member.get("/readings/period", params={"reading_date": bad_date}, headers=HTMX)

    assert response.status_code == 422
    assert message in text_of(response)
    assert "will be billed to" not in response.text


def test_the_line_follows_a_date_typed_before_a_refused_save(as_member, mary, mary_in_july):
    response = as_member.post("/readings", data=reading_form(mary, "119", "2025-08-30"), headers=HTMX)

    assert response.status_code == 422
    assert "This reading will be billed to <strong>August 2025</strong>" in response.text


def test_a_refused_date_shows_one_message_and_no_billing_month(as_member, mary):
    response = as_member.post("/readings", data=reading_form(mary, "12", "2025-02-30"), headers=HTMX)

    assert response.status_code == 422
    assert "will be billed to" not in response.text
    # said once, beside the date
    assert text_of(response).count("Please enter the reading date") == 1


def test_the_billing_month_is_what_a_save_really_uses(as_member, db_session, mary, sms):
    # the line and the save agree: both come from the date
    shown = as_member.get("/readings/period", params={"reading_date": "2025-06-15"}, headers=HTMX)
    as_member.post("/readings", data=reading_form(mary, "12", "2025-06-15"), headers=HTMX)

    assert "<strong>June 2025</strong>" in shown.text
    assert db_session.scalars(select(MeterReading)).one().billing_period == "2025-06"

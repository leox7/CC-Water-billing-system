"""reports/service.py (TASK-058 to TASK-061, TASK-063): arrears, payment history, the customer
list, the monthly summary and the dashboard figures. Bills and payments are made through the
real flow; the SMS provider is the stand-in from conftest.py."""

from datetime import date
from decimal import Decimal

import pytest

from app.core.errors import BusinessRuleError, InternalError
from app.features.payments import service as payments
from app.features.readings import service as readings
from app.features.reports import service as reports


@pytest.fixture
def owner(make_user):
    return make_user(role="owner", first_name="Daniel")


@pytest.fixture
def mary(make_customer):
    return make_customer(first_name="Mary", last_name="Wanjiru")


@pytest.fixture
def grace(make_customer):
    return make_customer(first_name="Grace", last_name="Njeri")


@pytest.fixture
def james(make_customer):
    return make_customer(first_name="James", last_name="Otieno")


def bill_in(db_session, owner, customer, current, reading_date, opening_balance=""):
    """The bill a reading makes. 10 units on the first reading is a bill of 1,450
    (10 x 130 + 150), and each further 10 units adds 1,450 of new charges."""
    typed = {"current_reading": current, "reading_date": reading_date, "opening_balance": opening_balance}
    return readings.create_reading(db_session, owner, customer.customer_id, typed).bill


def pay(db_session, owner, bill, amount, payment_date, code=""):
    typed = {"amount": amount, "payment_date": payment_date, "mpesa_reference": code}
    return payments.record_payment(db_session, owner, bill.bill_id, typed).payment


def owed_by_name(report):
    return {row.customer.first_name: row.amount_owed for row in report.rows}


# ---- REP-1 arrears (TASK-058) ----

def test_only_customers_whose_latest_bill_has_a_balance_appear(db_session, owner, mary, grace, james, sms):
    bill_in(db_session, owner, mary, "10", "2025-07-30")
    bill_in(db_session, owner, mary, "20", "2025-08-30")
    grace_bill = bill_in(db_session, owner, grace, "10", "2025-08-29")
    pay(db_session, owner, grace_bill, "1450", "2025-08-31")
    # James has no bill yet

    report = reports.arrears(db_session)

    # Mary's July bill is carried into August: she owes August's total once, never July again
    assert owed_by_name(report) == {"Mary": Decimal("2900.00")}
    assert (report.total_owed, report.customer_count) == (Decimal("2900.00"), 1)


def test_an_older_unpaid_bill_is_not_counted_once_the_latest_is_paid(db_session, owner, mary, sms):
    bill_in(db_session, owner, mary, "10", "2025-07-30")
    august = bill_in(db_session, owner, mary, "20", "2025-08-30")
    pay(db_session, owner, august, "2900", "2025-08-31")

    assert reports.arrears(db_session).rows == []


def test_the_debt_began_where_the_previous_balance_started(db_session, owner, mary, grace, james, sms):
    # Mary: nothing paid since July, so July
    bill_in(db_session, owner, mary, "10", "2025-07-30")
    bill_in(db_session, owner, mary, "20", "2025-08-30")
    # Grace: July paid in full, August unpaid, so August
    grace_july = bill_in(db_session, owner, grace, "10", "2025-07-30")
    pay(db_session, owner, grace_july, "1450", "2025-08-02")
    bill_in(db_session, owner, grace, "20", "2025-08-30")
    # James: a part of July left over, carried into August, still owed: July
    james_july = bill_in(db_session, owner, james, "10", "2025-07-30")
    pay(db_session, owner, james_july, "1000", "2025-08-02")
    bill_in(db_session, owner, james, "20", "2025-08-30")

    since = {row.customer.first_name: row.since for row in reports.arrears(db_session).rows}

    assert since == {"Mary": "2025-07", "Grace": "2025-08", "James": "2025-07"}


def test_paying_a_little_short_every_month_is_in_arrears_since_july(db_session, owner, mary, sms):
    # REP-1: each month's new charges are almost paid, but the shortfall is carried every time
    for current, reading_date, paid_on in (("10", "2025-07-30", "2025-08-02"), ("20", "2025-08-30", "2025-09-02")):
        bill = bill_in(db_session, owner, mary, current, reading_date)
        pay(db_session, owner, bill, str(bill.total_due - 50), paid_on)
    bill_in(db_session, owner, mary, "30", "2025-09-30")

    (row,) = reports.arrears(db_session).rows

    # September's total is 1,450 + 50 carried; the 50 left on July was never cleared
    assert (row.since, row.amount_owed) == ("2025-07", Decimal("1500.00"))


def test_a_debt_cleared_in_between_starts_again_from_the_new_bill(db_session, owner, mary, sms):
    bill_in(db_session, owner, mary, "10", "2025-06-30")
    july = bill_in(db_session, owner, mary, "20", "2025-07-30")
    pay(db_session, owner, july, "2900", "2025-08-01")
    bill_in(db_session, owner, mary, "30", "2025-08-30")
    bill_in(db_session, owner, mary, "40", "2025-09-30")

    (row,) = reports.arrears(db_session).rows

    # July was paid in full, so August carried nothing in: the debt began in August
    assert (row.since, row.amount_owed) == ("2025-08", Decimal("2900.00"))


def test_an_opening_balance_dates_the_debt_from_the_first_bill(db_session, owner, mary, sms):
    bill_in(db_session, owner, mary, "10", "2025-07-30", opening_balance="500")
    bill_in(db_session, owner, mary, "20", "2025-08-30")

    (row,) = reports.arrears(db_session).rows

    assert row.since == "2025-07"
    assert row.amount_owed == Decimal("3400.00")


def test_arrears_are_largest_first_with_the_total_and_the_status_counts(db_session, owner, mary, grace, james, sms):
    bill_in(db_session, owner, mary, "10", "2025-08-30")
    grace_bill = bill_in(db_session, owner, grace, "30", "2025-08-30")
    pay(db_session, owner, grace_bill, "500", "2025-08-31")
    bill_in(db_session, owner, james, "20", "2025-08-30")

    report = reports.arrears(db_session)

    assert [row.customer.first_name for row in report.rows] == ["Grace", "James", "Mary"]
    assert [row.amount_owed for row in report.rows] == [Decimal("3550.00"), Decimal("2750.00"), Decimal("1450.00")]
    assert report.total_owed == Decimal("7750.00")
    assert (report.partial_count, report.unpaid_count) == (1, 2)
    # each row points at the latest bill, where a payment is recorded (PAY-3)
    assert report.rows[0].bill.bill_id == grace_bill.bill_id


def test_an_inactive_customer_who_owes_still_appears(db_session, owner, mary, sms):
    bill_in(db_session, owner, mary, "10", "2025-08-30")
    mary.status = "inactive"
    db_session.flush()

    assert owed_by_name(reports.arrears(db_session)) == {"Mary": Decimal("1450.00")}


def test_an_overpaid_bill_owes_nothing(db_session, owner, mary, sms):
    bill = bill_in(db_session, owner, mary, "10", "2025-08-30")
    pay(db_session, owner, bill, "2000", "2025-08-31")

    assert reports.arrears(db_session).total_owed == Decimal("0.00")


def test_arrears_show_at_most_the_list_limit_but_count_everyone(db_session, owner, mary, grace, james, sms,
                                                                 monkeypatch):
    for customer in (mary, grace, james):
        bill_in(db_session, owner, customer, "10", "2025-08-30")
    monkeypatch.setattr(reports, "LIST_LIMIT", 2)

    report = reports.arrears(db_session)

    assert len(report.rows) == 2
    assert (report.customer_count, report.total_owed) == (3, Decimal("4350.00"))


# ---- REP-2 payment history (TASK-059) ----

@pytest.fixture
def three_payments(db_session, owner, mary, grace, sms):
    mary_july = bill_in(db_session, owner, mary, "10", "2025-07-30")
    first = pay(db_session, owner, mary_july, "500", "2025-07-31", code="QGH7X2K9LM")
    second = pay(db_session, owner, mary_july, "450", "2025-08-03")
    grace_august = bill_in(db_session, owner, grace, "10", "2025-08-29")
    third = pay(db_session, owner, grace_august, "1450", "2025-08-30", code="SFC7X92K")
    return first, second, third


def test_payment_history_lists_every_payment_newest_first(db_session, three_payments):
    first, second, third = three_payments

    history = reports.payment_history(db_session)

    assert [row.payment.payment_id for row in history.rows] == [third.payment_id, second.payment_id, first.payment_id]
    assert (history.payment_count, history.total) == (3, Decimal("2400.00"))
    assert history.rows[0].customer.first_name == "Grace"
    assert [row.bill_period for row in history.rows] == ["2025-08", "2025-07", "2025-07"]


def test_payment_history_filters_by_customer(db_session, mary, three_payments):
    history = reports.payment_history(db_session, customer=str(mary.customer_id))

    assert [row.customer.first_name for row in history.rows] == ["Mary", "Mary"]
    assert (history.payment_count, history.total) == (2, Decimal("950.00"))


def test_payment_history_filters_by_the_month_paid_not_the_bill_month(db_session, three_payments):
    first, second, third = three_payments

    history = reports.payment_history(db_session, month="2025-08")

    # Mary's second payment was on her July bill, but it was paid in August
    assert [row.payment.payment_id for row in history.rows] == [third.payment_id, second.payment_id]
    assert history.total == Decimal("1900.00")


def test_payment_history_filters_by_customer_and_month(db_session, mary, three_payments):
    history = reports.payment_history(db_session, customer=str(mary.customer_id), month="2025-07")

    assert [row.payment.amount for row in history.rows] == [Decimal("500.00")]


def test_payment_history_with_no_match_is_empty(db_session, grace, three_payments):
    history = reports.payment_history(db_session, customer=str(grace.customer_id), month="2025-07")

    assert (history.rows, history.payment_count, history.total) == ([], 0, Decimal("0"))


@pytest.mark.parametrize("bad_customer", ["abc", "-1", "1.5", "12345678901", "999999999", " 1", "1 OR 1=1"])
def test_payment_history_refuses_a_customer_not_on_the_list(db_session, bad_customer):
    with pytest.raises(BusinessRuleError) as refused:
        reports.payment_history(db_session, customer=bad_customer)

    assert refused.value.field == "customer"
    assert refused.value.message == "Please pick a customer from the list."


@pytest.mark.parametrize("bad_month", ["2025-13", "2025-00", "2025-8", "August", "2025-08-01", "x" * 300])
def test_payment_history_refuses_a_month_that_is_not_a_month(db_session, bad_month):
    with pytest.raises(BusinessRuleError) as refused:
        reports.payment_history(db_session, month=bad_month)

    assert refused.value.field == "month"


def test_payment_history_shows_at_most_the_list_limit_but_totals_them_all(db_session, three_payments, monkeypatch):
    monkeypatch.setattr(reports, "LIST_LIMIT", 2)

    history = reports.payment_history(db_session)

    assert len(history.rows) == 2
    assert (history.payment_count, history.total) == (3, Decimal("2400.00"))


def test_the_month_filter_offers_the_months_with_payments(db_session, three_payments):
    assert reports.payment_months(db_session) == ["2025-08", "2025-07"]


def test_the_customer_filter_offers_every_customer_by_name(db_session, mary, grace, james):
    names = [customer.first_name for customer in reports.customer_choices(db_session)]

    assert names == ["Grace", "James", "Mary"]


# ---- REP-3 customer list (TASK-060) ----

def test_each_customer_shows_the_status_of_their_latest_bill(db_session, owner, mary, grace, james, sms):
    mary_july = bill_in(db_session, owner, mary, "10", "2025-07-30")
    pay(db_session, owner, mary_july, "1450", "2025-08-01")
    mary_august = bill_in(db_session, owner, mary, "20", "2025-08-30")
    grace_august = bill_in(db_session, owner, grace, "10", "2025-08-30")
    pay(db_session, owner, grace_august, "500", "2025-08-31")

    report = reports.customer_report(db_session)

    by_name = {row.customer.first_name: row for row in report.rows}
    assert [row.customer.first_name for row in report.rows] == ["Grace", "James", "Mary"]
    # Mary paid July, but her latest bill is August, and it is unpaid
    assert (by_name["Mary"].latest.bill.bill_id, by_name["Mary"].latest.period) == (mary_august.bill_id, "2025-08")
    assert (by_name["Mary"].latest.bill.status, by_name["Mary"].amount_owed) == ("unpaid", Decimal("1450.00"))
    assert (by_name["Grace"].latest.bill.status, by_name["Grace"].amount_owed) == ("partial", Decimal("950.00"))
    assert (by_name["James"].latest, by_name["James"].amount_owed) == (None, Decimal("0.00"))
    assert report.customer_count == 3


def test_the_customer_list_includes_inactive_customers(db_session, mary):
    mary.status = "inactive"
    db_session.flush()

    assert [row.customer.first_name for row in reports.customer_report(db_session).rows] == ["Mary"]


def test_the_customer_list_filters_by_the_latest_bill_status(db_session, owner, mary, grace, james, sms):
    bill_in(db_session, owner, mary, "10", "2025-08-30")
    grace_bill = bill_in(db_session, owner, grace, "10", "2025-08-30")
    pay(db_session, owner, grace_bill, "1450", "2025-08-31")

    unpaid = reports.customer_report(db_session, "unpaid")
    paid = reports.customer_report(db_session, "paid")

    # James has no bill, so he matches no status
    assert [row.customer.first_name for row in unpaid.rows] == ["Mary"]
    assert [row.customer.first_name for row in paid.rows] == ["Grace"]
    assert reports.customer_report(db_session, "partial").rows == []


@pytest.mark.parametrize("bad_status", ["owing", "PAID", "paid ", "1"])
def test_the_customer_list_refuses_a_status_not_on_the_list(db_session, bad_status):
    with pytest.raises(BusinessRuleError) as refused:
        reports.customer_report(db_session, bad_status)

    assert refused.value.field == "status"


def test_the_customer_list_shows_at_most_the_list_limit(db_session, mary, grace, james, monkeypatch):
    monkeypatch.setattr(reports, "LIST_LIMIT", 2)

    report = reports.customer_report(db_session)

    assert (len(report.rows), report.customer_count) == (2, 3)


# ---- REP-4 monthly summary (TASK-061) ----

def test_the_monthly_summary_adds_up_that_months_bills_and_payments(db_session, owner, mary, grace, sms):
    mary_july = bill_in(db_session, owner, mary, "10", "2025-07-30")
    pay(db_session, owner, mary_july, "450", "2025-08-02")
    # Mary's August bill carries the 1,000 left on July
    mary_august = bill_in(db_session, owner, mary, "20", "2025-08-30")
    grace_august = bill_in(db_session, owner, grace, "30", "2025-08-30")
    pay(db_session, owner, grace_august, "1000", "2025-08-31")

    summary = reports.monthly_summary(db_session, "2025-08")

    # billed: new charges only, 1,450 for Mary and 4,050 for Grace, not Mary's carried 1,000
    assert summary.billed == Decimal("5500.00")
    assert summary.bill_count == 2
    assert summary.billed == sum(bill.consumption_charge + bill.service_charge for bill in (mary_august, grace_august))
    # collected: payments dated in August, whichever bill they were on
    assert (summary.collected, summary.payment_count) == (Decimal("1450.00"), 2)
    # outstanding: what is still unpaid on August's bills, carried balance included
    assert summary.outstanding == Decimal("2450.00") + Decimal("3050.00")


def test_the_monthly_summary_leaves_out_other_months(db_session, owner, mary, sms):
    july = bill_in(db_session, owner, mary, "10", "2025-07-30")
    pay(db_session, owner, july, "450", "2025-07-31")
    bill_in(db_session, owner, mary, "20", "2025-08-30")

    summary = reports.monthly_summary(db_session, "2025-07")

    assert (summary.billed, summary.bill_count) == (Decimal("1450.00"), 1)
    assert (summary.collected, summary.outstanding) == (Decimal("450.00"), Decimal("1000.00"))


def test_a_month_with_nothing_in_it_is_all_zero(db_session):
    summary = reports.monthly_summary(db_session, "2025-01")

    assert (summary.billed, summary.collected, summary.outstanding) == (Decimal("0"), Decimal("0"), Decimal("0"))
    assert (summary.bill_count, summary.payment_count) == (0, 0)


def test_an_overpaid_bill_adds_nothing_to_outstanding(db_session, owner, mary, sms):
    bill = bill_in(db_session, owner, mary, "10", "2025-08-30")
    pay(db_session, owner, bill, "2000", "2025-08-31")

    summary = reports.monthly_summary(db_session, "2025-08")

    assert (summary.collected, summary.outstanding) == (Decimal("2000.00"), Decimal("0.00"))


@pytest.mark.parametrize("bad_month", ["", "2025-13", "25-08", "this month"])
def test_the_monthly_summary_refuses_a_month_that_is_not_a_month(db_session, bad_month):
    with pytest.raises(BusinessRuleError) as refused:
        reports.monthly_summary(db_session, bad_month)

    assert refused.value.field == "month"


def test_the_summary_offers_months_with_bills_or_payments(db_session, owner, mary, sms):
    july = bill_in(db_session, owner, mary, "10", "2025-07-30")
    pay(db_session, owner, july, "450", "2025-09-02")

    assert reports.summary_months(db_session) == ["2025-09", "2025-07"]


# ---- DASH-1 dashboard (TASK-063) ----

@pytest.fixture
def august_31(monkeypatch):
    monkeypatch.setattr(reports, "today", lambda: date(2025, 8, 31))


def test_the_dashboard_figures_agree_with_the_reports(db_session, owner, mary, grace, james, august_31, sms):
    grace.location = "Muthaiga Rd"
    james.status = "inactive"
    bill_in(db_session, owner, mary, "10", "2025-07-30")
    bill_in(db_session, owner, mary, "20", "2025-08-30")
    grace_bill = bill_in(db_session, owner, grace, "10", "2025-08-29")
    pay(db_session, owner, grace_bill, "500", "2025-08-31")

    dashboard = reports.dashboard(db_session)

    # James is inactive: two active customers, in two places
    assert (dashboard.active_customers, dashboard.location_count) == (2, 2)
    assert dashboard.period == "2025-08"
    assert dashboard.summary == reports.monthly_summary(db_session, "2025-08")
    assert (dashboard.summary.billed, dashboard.summary.bill_count) == (Decimal("2900.00"), 2)
    arrears = reports.arrears(db_session)
    assert dashboard.arrears.total_owed == arrears.total_owed == Decimal("3850.00")
    assert (dashboard.arrears.partial_count, dashboard.arrears.unpaid_count) == (1, 1)
    assert [row.customer.first_name for row in dashboard.needs_attention] == ["Mary", "Grace"]


def test_the_dashboard_lists_only_the_first_people_who_owe(db_session, owner, mary, grace, james, august_31,
                                                            monkeypatch, sms):
    for customer in (mary, grace, james):
        bill_in(db_session, owner, customer, "10", "2025-08-30")
    monkeypatch.setattr(reports, "NEEDS_ATTENTION_LIMIT", 2)

    dashboard = reports.dashboard(db_session)

    assert len(dashboard.needs_attention) == 2
    assert dashboard.arrears.customer_count == 3


def test_an_empty_dashboard_is_all_zero(db_session, august_31):
    dashboard = reports.dashboard(db_session)

    assert (dashboard.active_customers, dashboard.location_count) == (0, 0)
    assert (dashboard.summary.billed, dashboard.arrears.total_owed) == (Decimal("0"), Decimal("0"))
    assert dashboard.needs_attention == []


def test_this_month_is_the_month_of_today_in_kenya(monkeypatch):
    monkeypatch.setattr(reports, "today", lambda: date(2026, 10, 1))

    assert reports.current_period() == "2026-10"


# ---- an unexpected failure is a 500 with a safe message ----

class BrokenSession:
    """Stands in for a database that has gone away."""

    def __getattr__(self, name):
        raise RuntimeError("the database went away")


@pytest.mark.parametrize("call", [
    reports.arrears,
    reports.payment_history,
    reports.payment_months,
    reports.customer_choices,
    reports.customer_report,
    lambda db: reports.monthly_summary(db, "2025-08"),
    reports.summary_months,
    reports.dashboard,
])
def test_a_database_failure_becomes_a_safe_500(call):
    with pytest.raises(InternalError) as failed:
        call(BrokenSession())

    assert failed.value.status_code == 500
    assert "database" not in failed.value.message

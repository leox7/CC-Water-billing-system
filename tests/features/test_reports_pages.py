"""The Reports screens and the dashboard, end to end through the real app (TASK-062 to TASK-064).
The SMS provider is the stand-in from conftest.py."""

import html
import re
from datetime import date

import pytest
from sqlalchemy import func, select

from app.features.payments import service as payments
from app.features.readings import service as readings
from app.features.reports import service as reports
from app.models import AuditLog

REPORT_PAGES = ["/reports", "/reports/arrears", "/reports/payments", "/reports/customers", "/reports/summary"]


def text_of(response):
    return html.unescape(response.text)


def bill_in(db_session, owner, customer, current, reading_date):
    typed = {"current_reading": current, "reading_date": reading_date}
    return readings.create_reading(db_session, owner, customer.customer_id, typed).bill


def pay(db_session, owner, bill, amount, payment_date, code=""):
    typed = {"amount": amount, "payment_date": payment_date, "mpesa_reference": code}
    return payments.record_payment(db_session, owner, bill.bill_id, typed).payment


def audit_count(db_session):
    return db_session.scalar(select(func.count()).select_from(AuditLog))


@pytest.fixture
def owner(make_user):
    return make_user(role="owner", first_name="Daniel")


@pytest.fixture
def as_member(web, make_user, sign_in_as):
    sign_in_as(web, make_user(role="member", first_name="Peter"))
    return web


@pytest.fixture
def mary(make_customer):
    return make_customer(first_name="Mary", last_name="Wanjiru")


@pytest.fixture
def grace(make_customer):
    grace = make_customer(first_name="Grace", last_name="Njeri")
    grace.location = "Muthaiga Rd"
    return grace


@pytest.fixture
def august(db_session, owner, mary, grace, sms, monkeypatch):
    """Mary owes July and August (2,900, since July). Grace paid 500 of August's 1,450."""
    monkeypatch.setattr(reports, "today", lambda: date(2025, 8, 31))
    bill_in(db_session, owner, mary, "10", "2025-07-30")
    mary_august = bill_in(db_session, owner, mary, "20", "2025-08-30")
    grace_august = bill_in(db_session, owner, grace, "10", "2025-08-29")
    pay(db_session, owner, grace_august, "500", "2025-08-31", code="SFC7X92K")
    return mary_august, grace_august


# ---- the menu (TASK-062) ----

def test_the_menu_has_the_four_reports_arrears_first(as_member):
    response = as_member.get("/reports")

    page = text_of(response)
    assert response.status_code == 200
    menu = page.split('class="report-menu"')[1].split("</ul>")[0]
    assert re.findall(r'href="(/reports/[a-z]+)"', menu) == [
        "/reports/arrears", "/reports/payments", "/reports/customers", "/reports/summary"]
    for title in ("Who owes money", "Payments received", "All customers", "This month at a glance"):
        assert title in page
    assert '<link rel="stylesheet" href="/static/reports/reports.css">' in page
    assert 'href="/reports" aria-current="page"' in page


@pytest.mark.parametrize("path", REPORT_PAGES[1:])
def test_each_report_has_a_way_back_to_the_menu(as_member, path):
    page = as_member.get(path).text

    assert 'class="button button-secondary page-back" href="/reports"' in page
    # on a phone the top bar holds it
    assert 'class="topbar-link" href="/reports"' in page


# ---- arrears ----

def test_the_arrears_page_shows_who_owes_with_the_total(as_member, august):
    mary_august, grace_august = august

    response = as_member.get("/reports/arrears")

    page = text_of(response)
    assert response.status_code == 200
    assert "2 customers owe" in page and "Ksh 3,850" in page
    # largest first: Mary, then Grace
    assert page.index("Mary Wanjiru") < page.index("Grace Njeri")
    assert "In arrears since" in page and "Ksh 2,900" in page and "July 2025" in page
    assert "Ksh 950" in page and "August 2025" in page
    # each row opens the latest bill, where the payment goes
    assert f'href="/bills/{mary_august.bill_id}"' in page and f'href="/bills/{grace_august.bill_id}"' in page
    assert "Partial" in page and "Unpaid" in page


def test_the_arrears_page_says_when_nobody_owes(as_member):
    page = text_of(as_member.get("/reports/arrears"))

    assert "Nobody owes money right now." in page
    assert 'class="table"' not in page


# ---- payment history ----

def test_the_payment_history_page_lists_payments_with_the_total(as_member, august):
    mary_august, grace_august = august

    page = text_of(as_member.get("/reports/payments"))

    assert "1 payment," in page and "Ksh 500" in page
    assert "SFC7X92K" in page and "31 Aug 2025" in page
    assert f'href="/bills/{grace_august.bill_id}"' in page
    assert '<option value="2025-08">August 2025</option>' in page


def test_the_payment_history_page_keeps_the_chosen_filters(as_member, august, grace):
    response = as_member.get(f"/reports/payments?customer={grace.customer_id}&month=2025-08")

    page = text_of(response)
    assert response.status_code == 200
    assert f'<option value="{grace.customer_id}" selected>Grace Njeri ({grace.meter_number})</option>' in page
    assert '<option value="2025-08" selected>August 2025</option>' in page
    assert "SFC7X92K" in page


def test_a_filter_with_no_payments_offers_to_show_them_all(as_member, august, mary):
    page = text_of(as_member.get(f"/reports/payments?customer={mary.customer_id}"))

    assert "No payments match these filters." in page
    assert 'href="/reports/payments">Show all payments' in page


@pytest.mark.parametrize("query,field,message", [
    ("customer=abc", "customer", "Please pick a customer from the list."),
    ("customer=99999999", "customer", "Please pick a customer from the list."),
    ("month=2025-13", "month", "Please pick a month from the list."),
])
def test_a_bad_payment_filter_is_refused_beside_its_field(as_member, august, query, field, message):
    response = as_member.get(f"/reports/payments?{query}")

    page = text_of(response)
    assert response.status_code == 422
    assert f'id="field-{field}-error"' in page and message in page
    assert f'id="field-{field}" name="{field}" aria-invalid="true"' in page
    # nothing technical, and no payments shown under a refused filter
    assert "Traceback" not in page and "SFC7X92K" not in page


def test_a_customer_name_is_escaped_on_the_reports(as_member, db_session, owner, make_customer, sms):
    customer = make_customer(first_name="<b>Mary</b>", last_name="Wanjiru")
    bill = bill_in(db_session, owner, customer, "10", "2025-08-30")
    pay(db_session, owner, bill, "500", "2025-08-31")

    for path in ("/reports/arrears", "/reports/payments", "/reports/customers"):
        page = as_member.get(path).text
        assert "<b>Mary</b>" not in page
        assert "&lt;b&gt;Mary&lt;/b&gt;" in page


# ---- customer list ----

def test_the_customer_list_page_shows_each_latest_bill(as_member, august, make_customer):
    make_customer(first_name="James", last_name="Otieno")

    page = text_of(as_member.get("/reports/customers"))

    assert "3 customers." in page
    assert "Muthaiga Rd" in page and "No bill yet" in page
    assert page.index("Grace Njeri") < page.index("James Otieno") < page.index("Mary Wanjiru")


def test_the_customer_list_page_filters_by_status(as_member, august):
    page = text_of(as_member.get("/reports/customers?status=partial"))

    assert "Grace Njeri" in page and "Mary Wanjiru" not in page
    assert '<option value="partial" selected>Partial</option>' in page


def test_a_bad_status_is_refused_beside_its_field(as_member, august):
    response = as_member.get("/reports/customers?status=owing")

    assert response.status_code == 422
    assert 'id="field-status-error"' in response.text
    assert "Mary Wanjiru" not in response.text


def test_the_customer_list_page_with_no_customers_offers_to_add_one(as_member):
    page = text_of(as_member.get("/reports/customers"))

    assert "No customers yet." in page and 'href="/customers/new"' in page


# ---- monthly summary ----

def test_the_summary_opens_on_the_newest_month(as_member, august):
    response = as_member.get("/reports/summary")

    page = text_of(response)
    assert response.status_code == 200
    assert '<option value="2025-08" selected>August 2025</option>' in page
    # billed: 1,450 for each of them, without Mary's carried July balance
    assert "Ksh 2,900" in page and "2 bills, new charges only" in page
    assert "Ksh 500" in page and "1 payment dated this month" in page
    assert "Ksh 3,850" in page


def test_the_summary_shows_the_chosen_month(as_member, august):
    page = text_of(as_member.get("/reports/summary?month=2025-07"))

    assert '<option value="2025-07" selected>July 2025</option>' in page
    assert "Ksh 1,450" in page and "1 bill, new charges only" in page


def test_a_bad_summary_month_is_refused(as_member, august):
    response = as_member.get("/reports/summary?month=July")

    assert response.status_code == 422
    assert 'id="field-month-error"' in response.text and "Please pick a month from the list." in response.text


def test_the_summary_with_no_bills_shows_this_month_at_zero(as_member, monkeypatch):
    monkeypatch.setattr(reports, "today", lambda: date(2026, 10, 17))

    page = text_of(as_member.get("/reports/summary"))

    assert '<option value="2026-10" selected>October 2026</option>' in page
    assert "Ksh 0" in page


# ---- the dashboard (TASK-063) ----

def test_the_dashboard_shows_the_four_figures_and_who_needs_attention(as_member, august):
    mary_august, grace_august = august

    response = as_member.get("/")

    page = text_of(response)
    assert response.status_code == 200
    assert "Welcome, Peter" in page
    figures = re.findall(r'<p class="stat-figure">(.*?)</p>', page)
    assert figures == ["2", "Ksh 2,900", "Ksh 3,850", "2"]
    assert "Billed in August" in page and "Billed this month" not in page
    assert "Across 2 locations" in page and "2 bills for August 2025" in page
    assert "Across 2 customers" in page and "1 partial, 1 unpaid" in page
    attention = page.split('id="attention-title"')[1]
    assert attention.index("Mary Wanjiru") < attention.index("Grace Njeri")
    assert f'href="/bills/{mary_august.bill_id}"' in attention and "Muthaiga Rd" in attention
    assert 'href="/" aria-current="page"' in page


def test_the_dashboard_figures_agree_with_the_reports(as_member, august):
    dashboard = text_of(as_member.get("/"))
    arrears = text_of(as_member.get("/reports/arrears"))
    summary = text_of(as_member.get("/reports/summary?month=2025-08"))

    outstanding, billed = "Ksh 3,850", "Ksh 2,900"
    assert outstanding in dashboard and outstanding in arrears
    assert billed in dashboard and billed in summary


def test_a_month_with_no_bills_says_so_on_the_billed_card(as_member, august, monkeypatch):
    # DASH-1: bills exist, but none yet for October
    monkeypatch.setattr(reports, "today", lambda: date(2025, 10, 5))

    page = text_of(as_member.get("/"))

    assert "Billed in October" in page and "No bills yet for October" in page
    assert re.findall(r'<p class="stat-figure">(.*?)</p>', page)[1] == "Ksh 0"
    # the debt from earlier months is still counted
    assert "Ksh 3,850" in page


def test_the_dashboard_says_when_nobody_owes(as_member):
    page = text_of(as_member.get("/"))

    assert "Nobody owes money right now." in page
    assert re.findall(r'<p class="stat-figure">(.*?)</p>', page) == ["0", "Ksh 0", "Ksh 0", "0"]
    assert "No active customers yet" in page


def test_a_long_needs_attention_list_points_to_the_arrears_report(as_member, august, monkeypatch):
    monkeypatch.setattr(reports, "NEEDS_ATTENTION_LIMIT", 1)

    page = text_of(as_member.get("/"))

    assert 'class="button button-quiet" href="/reports/arrears">See all 2 customers who owe money' in page
    assert "Grace Njeri" not in page.split('id="attention-title"')[1]


def test_the_owner_sees_the_same_dashboard(web, owner, sign_in_as, august):
    sign_in_as(web, owner)

    page = text_of(web.get("/"))

    assert "Welcome, Daniel" in page and "Ksh 3,850" in page


# ---- reports change nothing and record nothing (AUDIT_LOG.md section 4) ----

def test_opening_the_reports_and_the_dashboard_records_nothing(as_member, august, db_session):
    before = audit_count(db_session)

    for path in ["/", *REPORT_PAGES, "/reports/payments?month=2025-13"]:
        as_member.get(path)

    assert audit_count(db_session) == before


@pytest.mark.parametrize("path", ["/", *REPORT_PAGES])
def test_a_report_takes_no_post(as_member, path):
    # REQ-2: reading is GET only
    assert as_member.post(path, data={}).status_code in (403, 405)


def test_a_database_failure_shows_the_safe_error_page(as_member, monkeypatch):
    def broken(db):
        raise RuntimeError("the database went away")

    monkeypatch.setattr(reports, "_bills_by_customer", broken)

    response = as_member.get("/reports/arrears")

    assert response.status_code == 500
    assert "Something went wrong on our side" in response.text
    assert "database went away" not in response.text

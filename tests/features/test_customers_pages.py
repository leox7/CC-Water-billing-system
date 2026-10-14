"""The Customers screen, end to end through the real app (TASK-035 to TASK-037, CUS-1 to CUS-4)."""

import re
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.features.customers import service as customers
from app.models import AuditLog, Customer, MeterReading
from tests.conftest import TEST_CSRF_TOKEN

HTMX = {"HX-Request": "true", "X-CSRF-Token": TEST_CSRF_TOKEN}


def form_data(**changes):
    values = {"csrf_token": TEST_CSRF_TOKEN, "first_name": "Mary", "last_name": "Wanjiru", "phone": "0722 123 456",
              "location": "Kiawara Estate", "meter_number": "mtr-0032", "rate_per_unit": "130.00",
              "service_charge": "150.00"}
    values.update(changes)
    return values


@pytest.fixture
def owner(make_user):
    return make_user(role="owner", first_name="Daniel", phone="254711000001")


@pytest.fixture
def member(make_user):
    return make_user(role="member", first_name="Peter", phone="254733000002")


@pytest.fixture
def as_owner(web, owner, sign_in_as):
    sign_in_as(web, owner)
    return web


@pytest.fixture
def as_member(web, member, sign_in_as):
    sign_in_as(web, member)
    return web


def add_customer(db_session, owner, first_name="Mary", last_name="Wanjiru", meter_number="MTR-0032",
                 location="Kiawara Estate", phone="0722 111 111"):
    return customers.create_customer(db_session, owner, {
        "first_name": first_name, "last_name": last_name, "phone": phone,
        "location": location, "meter_number": meter_number})


@pytest.fixture
def mary(db_session, owner):
    return add_customer(db_session, owner)


def add_reading(db_session, customer, recorded_by):
    db_session.add(MeterReading(customer_id=customer.customer_id, billing_period="2026-08",
                                previous_reading=Decimal("0"), current_reading=Decimal("10"),
                                units_consumed=Decimal("10"), reading_date=date(2026, 8, 20),
                                recorded_by=recorded_by.user_id))
    db_session.flush()


def row_html(html, customer_id):
    return re.search(rf'<tr id="customer-row-{customer_id}">.*?</tr>', html, re.S).group(0)


def results_html(html):
    return re.search(r'<div id="customer-results">.*', html, re.S).group(0)


# ---- the list (TASK-035) ----

def test_the_owner_sees_the_list_with_add_and_row_actions(as_owner, mary):
    html = as_owner.get("/customers").text

    assert "<h1>Customers</h1>" in html
    assert 'href="/customers/new"' in html and "Add customer" in html
    row = row_html(html, mary.customer_id)
    assert "Mary Wanjiru" in row
    assert "MTR-0032" in row and "Kiawara Estate" in row
    assert 'href="/customers/{}/edit"'.format(mary.customer_id) in row
    assert "Deactivate" in row and "Remove Mary Wanjiru" in row
    assert "/static/customers/customers.css" in html


def test_the_list_counts_the_active_customers(as_owner, db_session, owner, mary):
    add_customer(db_session, owner, "John", "Mwangi", "MTR-0014")
    customers.deactivate_customer(db_session, owner, mary.customer_id)

    html = as_owner.get("/customers").text

    assert '<p class="text-label" id="active-count">1 active</p>' in html


def test_the_phone_number_is_masked_in_the_list(as_owner, mary):
    # OUT-3: the full number is not needed on the list
    row = row_html(as_owner.get("/customers").text, mary.customer_id)

    assert "0722 XXX 111" in row
    assert "254722111111" not in row and "0722111111" not in row


def test_each_row_shows_its_status_with_a_word_and_an_icon(as_owner, db_session, owner, mary):
    customers.deactivate_customer(db_session, owner, mary.customer_id)

    row = row_html(as_owner.get("/customers").text, mary.customer_id)

    assert 'class="badge badge-inactive"' in row and "Inactive" in row and "<svg" in row


def test_the_meter_number_is_written_in_the_mono_font(as_owner, mary):
    assert '<span class="mono">MTR-0032</span>' in as_owner.get("/customers").text


@pytest.mark.parametrize("search,shown,hidden", [
    ("mary", ["Mary Wanjiru"], ["John Mwangi", "Grace Njeri"]),
    ("MTR-0014", ["John Mwangi"], ["Mary Wanjiru", "Grace Njeri"]),
    ("muthaiga", ["Grace Njeri"], ["Mary Wanjiru", "John Mwangi"]),
])
def test_the_list_searches_by_name_meter_number_or_location(as_owner, db_session, owner, mary, search, shown, hidden):
    add_customer(db_session, owner, "John", "Mwangi", "MTR-0014")
    add_customer(db_session, owner, "Grace", "Njeri", "MTR-0027", location="Muthaiga Rd")

    html = as_owner.get("/customers", params={"search": search}).text

    for name in shown:
        assert name in results_html(html)
    for name in hidden:
        assert name not in results_html(html)
    assert f'value="{search}"' in html
    assert "1 customer found." in html


def test_an_htmx_search_gets_the_same_page_for_htmx_to_pick_from(as_owner, mary):
    response = as_owner.get("/customers", params={"search": "mary"}, headers={"HX-Request": "true"})

    assert response.status_code == 200
    assert 'id="customer-results"' in response.text and 'id="search-status"' in response.text


def test_the_search_box_is_wired_to_htmx(as_owner, mary):
    html = as_owner.get("/customers").text

    assert 'hx-get="/customers"' in html
    assert 'hx-select="#customer-results"' in html and 'hx-target="#customer-results"' in html
    assert 'hx-push-url="true"' in html
    # it still works as a normal search without htmx
    assert '<form class="search-form" method="get" action="/customers" role="search"' in html


def test_a_search_with_no_match_says_so_and_offers_the_full_list(as_owner, mary):
    html = as_owner.get("/customers", params={"search": "nobody"}).text

    assert 'No customers match "nobody".' in html
    assert 'href="/customers">Show all customers</a>' in html
    assert "0 customers found." in html


def test_the_owner_sees_the_empty_state_with_one_action(as_owner):
    html = as_owner.get("/customers").text

    assert "No customers yet. Add your first customer to get started." in html
    assert 'class="card empty-state"' in html
    assert 'id="customer-rows"' not in html


def test_a_search_that_is_too_long_is_refused_with_a_message(as_owner, mary):
    response = as_owner.get("/customers", params={"search": "a" * 101})

    assert response.status_code == 422
    assert "Please search with 100 characters or fewer." in results_html(response.text)
    assert 'aria-invalid="true"' in response.text


def test_what_a_customer_typed_is_escaped(as_owner, db_session, owner):
    add_customer(db_session, owner, first_name="<script>alert(1)</script>")

    html = as_owner.get("/customers").text
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html

    # what was typed in the search box too, in the box and in the "no match" line
    html = as_owner.get("/customers", params={"search": "<b>"}).text
    assert "<b>" not in html
    assert 'value="&lt;b&gt;"' in html


def test_the_pages_hold_no_inline_styles_or_scripts(as_owner, mary):
    for path in ("/customers", "/customers/new", f"/customers/{mary.customer_id}/edit"):
        html = as_owner.get(path).text
        assert "style=" not in html and "<style" not in html
        assert "<script>" not in html and "onclick" not in html


# ---- adding and editing (TASK-036) ----

def test_the_add_form_shows_the_default_rate_and_service_charge(as_owner):
    html = as_owner.get("/customers/new").text

    assert "<h1>Add customer</h1>" in html
    assert 'name="rate_per_unit" value="130.00"' in html
    assert 'name="service_charge" value="150.00"' in html
    assert 'action="/customers/new"' in html
    assert 'name="csrf_token"' in html
    # a phone gets the right keypad, and the mobile top bar has a Back button
    assert 'type="tel"' in html and 'inputmode="decimal"' in html
    assert '<a class="topbar-link" href="/customers">' in html


def test_the_owner_adds_a_customer_and_goes_back_to_the_list(as_owner, db_session):
    response = as_owner.post("/customers/new", data=form_data(), follow_redirects=False)

    customer = db_session.scalars(select(Customer)).one()
    assert response.status_code == 303
    assert response.headers["location"] == f"/customers?notice=added&customer={customer.customer_id}"
    assert customer.meter_number == "MTR-0032" and customer.phone == "254722123456"

    html = as_owner.get(response.headers["location"]).text
    assert "Mary Wanjiru was added." in html
    assert "data-auto-dismiss" in html


def test_a_notice_names_only_a_real_customer(as_owner, mary):
    html = as_owner.get("/customers", params={"notice": "added", "customer": 999999}).text
    assert "was added." not in html

    html = as_owner.get("/customers", params={"notice": "anything", "customer": mary.customer_id}).text
    assert "message-success" not in html


def test_a_refused_customer_comes_back_with_the_message_beside_the_field(as_owner, db_session):
    response = as_owner.post("/customers/new", data=form_data(phone="12345", location="Muthaiga Rd"))

    assert response.status_code == 422
    html = response.text
    assert "Please enter a Kenyan mobile number" in html
    assert 'id="field-phone-error"' in html
    assert re.search(r'id="field-phone"[^>]*aria-invalid="true"', html)
    # every typed value is kept
    assert 'value="12345"' in html and 'value="Muthaiga Rd"' in html and 'value="mtr-0032"' in html
    assert db_session.scalars(select(Customer)).all() == []


def test_a_meter_number_in_use_comes_back_as_a_conflict(as_owner, mary):
    response = as_owner.post("/customers/new", data=form_data(first_name="John", meter_number="MTR-0032"))

    assert response.status_code == 409
    assert "Meter number MTR-0032 already belongs to Mary Wanjiru." in response.text
    assert 'id="field-meter_number-error"' in response.text
    assert 'value="John"' in response.text


def test_a_status_sent_with_the_form_is_ignored(as_owner, db_session):
    as_owner.post("/customers/new", data=form_data(status="inactive"))

    assert db_session.scalars(select(Customer)).one().status == "active"


def test_the_edit_form_is_filled_in_with_the_customer(as_owner, mary):
    html = as_owner.get(f"/customers/{mary.customer_id}/edit").text

    assert "<h1>Edit customer</h1>" in html
    assert 'value="Mary"' in html and 'value="Wanjiru"' in html
    assert 'value="0722111111"' in html
    assert 'value="MTR-0032"' in html and 'value="130.00"' in html
    assert f'action="/customers/{mary.customer_id}/edit"' in html
    assert "Save changes" in html


def test_the_owner_edits_a_customer(as_owner, db_session, mary):
    response = as_owner.post(f"/customers/{mary.customer_id}/edit",
                             data=form_data(first_name="Mary", location="Muthaiga Rd", phone="0722111111",
                                            meter_number="MTR-0032", rate_per_unit="140"),
                             follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == f"/customers?notice=saved&customer={mary.customer_id}"
    db_session.refresh(mary)
    assert mary.location == "Muthaiga Rd" and mary.rate_per_unit == Decimal("140.00")
    # the apostrophe is escaped in the HTML
    assert "Mary Wanjiru&#39;s details were saved." in as_owner.get(response.headers["location"]).text


def test_a_refused_edit_keeps_the_typed_values(as_owner, db_session, mary):
    response = as_owner.post(f"/customers/{mary.customer_id}/edit",
                             data=form_data(location="Muthaiga Rd", rate_per_unit="abc"))

    assert response.status_code == 422
    assert "Please enter the rate per unit as a number" in response.text
    assert 'value="abc"' in response.text and 'value="Muthaiga Rd"' in response.text
    db_session.refresh(mary)
    assert mary.location == "Kiawara Estate"


@pytest.mark.parametrize("method,path", [("GET", "/customers/999999/edit"), ("POST", "/customers/999999/edit"),
                                         ("GET", "/customers/abc/edit")])
def test_an_unknown_customer_is_a_404(as_owner, method, path):
    response = as_owner.request(method, path, data=form_data() if method == "POST" else None)

    assert response.status_code == 404
    assert "We could not find" in response.text


def test_a_post_without_the_csrf_token_changes_nothing(as_owner, db_session):
    response = as_owner.post("/customers/new", data=form_data(csrf_token=""))

    assert response.status_code == 403
    assert db_session.scalars(select(Customer)).all() == []


# ---- deactivate, reactivate and remove (TASK-037) ----

def test_deactivating_swaps_the_row_and_announces_the_new_status(as_owner, db_session, mary):
    response = as_owner.post(f"/customers/{mary.customer_id}/deactivate", headers=HTMX)

    assert response.status_code == 200
    row = row_html(response.text, mary.customer_id)
    assert 'role="status"' in row and "Inactive" in row
    assert "Reactivate" in row
    assert "Mary Wanjiru was deactivated and will get no new bills." in response.text
    assert 'hx-swap-oob="innerHTML"' in response.text
    assert '<p class="text-label" id="active-count" hx-swap-oob="true">0 active</p>' in response.text
    db_session.refresh(mary)
    assert mary.status == "inactive"


def test_the_deactivate_dialog_asks_first_and_keeps_its_errors_inside(as_owner, mary):
    row = row_html(as_owner.get("/customers").text, mary.customer_id)

    assert f'<dialog class="modal" id="deactivate-dialog-{mary.customer_id}"' in row
    assert "Deactivate Mary Wanjiru?" in row
    assert "Keep active" in row and "Yes, deactivate" in row
    assert f'data-error-target="#deactivate-error-{mary.customer_id}"' in row


def test_reactivating_swaps_the_row_back(as_owner, db_session, owner, mary):
    customers.deactivate_customer(db_session, owner, mary.customer_id)

    response = as_owner.post(f"/customers/{mary.customer_id}/reactivate", headers=HTMX)

    assert response.status_code == 200
    row = row_html(response.text, mary.customer_id)
    assert "Active" in row and "Deactivate" in row
    assert "Mary Wanjiru was reactivated" in response.text
    db_session.refresh(mary)
    assert mary.status == "active"


def test_removing_a_customer_with_no_history_removes_the_row(as_owner, db_session, mary):
    customer_id = mary.customer_id

    response = as_owner.post(f"/customers/{customer_id}/remove", headers=HTMX)

    assert response.status_code == 200
    assert "<tr" not in response.text
    assert "Mary Wanjiru was removed." in response.text
    assert db_session.get(Customer, customer_id) is None


def test_a_customer_with_history_is_never_hard_deleted(as_owner, db_session, owner, mary):
    add_reading(db_session, mary, owner)

    row = row_html(as_owner.get("/customers").text, mary.customer_id)
    assert "Remove Mary Wanjiru" not in row and "remove-dialog" not in row

    # even a hand-made post is refused
    response = as_owner.post(f"/customers/{mary.customer_id}/remove", headers=HTMX)
    assert response.status_code == 422
    assert "Deactivate the customer instead." in response.text
    assert 'role="alert"' in response.text
    assert db_session.get(Customer, mary.customer_id) is not None


def test_without_htmx_a_change_goes_back_to_the_list(as_owner, db_session, mary):
    response = as_owner.post(f"/customers/{mary.customer_id}/deactivate", data={"csrf_token": TEST_CSRF_TOKEN},
                             follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/customers"
    db_session.refresh(mary)
    assert mary.status == "inactive"


@pytest.mark.parametrize("action", ["deactivate", "reactivate", "remove"])
def test_an_action_on_an_unknown_customer_is_a_404(as_owner, action):
    response = as_owner.post(f"/customers/999999/{action}", headers=HTMX)

    assert response.status_code == 404
    assert "We could not find that customer." in response.text


# ---- a member does the same customer work as the owner (PRD section 3) ----

def test_a_member_sees_add_and_every_row_action(as_member, mary):
    html = as_member.get("/customers").text

    assert 'href="/customers/new"' in html and "Add customer" in html
    row = row_html(html, mary.customer_id)
    assert f'href="/customers/{mary.customer_id}/edit"' in row
    assert "Deactivate" in row and "Remove Mary Wanjiru" in row
    assert as_member.get("/customers/new").status_code == 200
    assert as_member.get(f"/customers/{mary.customer_id}/edit").status_code == 200


def test_a_member_adds_and_edits_a_customer(as_member, db_session, member):
    response = as_member.post("/customers/new", data=form_data(), follow_redirects=False)
    assert response.status_code == 303
    customer = db_session.scalars(select(Customer)).one()

    response = as_member.post(f"/customers/{customer.customer_id}/edit",
                              data=form_data(meter_number="MTR-0032", location="Muthaiga Rd"), follow_redirects=False)

    assert response.status_code == 303
    db_session.refresh(customer)
    assert customer.location == "Muthaiga Rd"
    created = db_session.scalars(select(AuditLog).where(AuditLog.event_type == "customer.created")).one()
    assert created.actor_user_id == member.user_id


def test_a_member_deactivates_reactivates_and_removes_a_customer(as_member, db_session, mary):
    customer_id = mary.customer_id

    assert as_member.post(f"/customers/{customer_id}/deactivate", headers=HTMX).status_code == 200
    db_session.refresh(mary)
    assert mary.status == "inactive"
    assert as_member.post(f"/customers/{customer_id}/reactivate", headers=HTMX).status_code == 200
    db_session.refresh(mary)
    assert mary.status == "active"
    assert as_member.post(f"/customers/{customer_id}/remove", headers=HTMX).status_code == 200

    assert db_session.get(Customer, customer_id) is None


def test_a_member_sees_the_same_empty_state_as_the_owner(as_member):
    html = as_member.get("/customers").text

    assert "No customers yet. Add your first customer to get started." in html


# ---- an unexpected failure is a safe 500 with a Reference ----

def test_an_unexpected_failure_on_the_list_is_a_safe_500(as_owner, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("database is down")

    monkeypatch.setattr(customers, "list_customers", broken)

    response = as_owner.get("/customers")

    assert response.status_code == 500
    assert "Something went wrong on our side." in response.text and "Reference" in response.text
    assert "database is down" not in response.text


def test_an_unexpected_failure_while_adding_keeps_the_form_filled_in(as_owner, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("database is down")

    monkeypatch.setattr(customers, "create_customer", broken)

    response = as_owner.post("/customers/new", data=form_data(location="Muthaiga Rd"))

    assert response.status_code == 500
    assert "Something went wrong on our side." in response.text and "Reference" in response.text
    assert 'value="Muthaiga Rd"' in response.text
    assert "database is down" not in response.text


@pytest.mark.parametrize("action", ["deactivate_customer", "reactivate_customer", "remove_customer"])
def test_an_unexpected_failure_on_a_row_action_is_a_safe_500(as_owner, mary, monkeypatch, action):
    def broken(*args, **kwargs):
        raise RuntimeError("database is down")

    monkeypatch.setattr(customers, action, broken)
    path = action.replace("_customer", "")

    response = as_owner.post(f"/customers/{mary.customer_id}/{path}", headers=HTMX)

    assert response.status_code == 500
    assert 'role="alert"' in response.text and "Reference" in response.text

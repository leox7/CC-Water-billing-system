"""Who may use which route (TASK-033, PRD section 3, SECURITY.md AUTHZ-1 to AUTHZ-5).

Every route in the app must be listed below as public, signed in, or owner only. A new
route that is not listed fails test_every_route_is_listed, so it cannot be forgotten.
Owner and member share all the billing work; only account management is owner only.
"""

import pathlib
from datetime import date
from decimal import Decimal

import pytest
from fastapi.routing import APIRoute
from sqlalchemy import select

from app.features.audit.service import EVENT_LEVELS
from app.models import AuditLog, Bill, Customer, MessageLog, MeterReading, User
from tests.conftest import TEST_CSRF_TOKEN

PUBLIC = {
    ("GET", "/health"),
    ("GET", "/sw.js"),
    ("GET", "/manifest.json"),
    ("GET", "/login"),
    ("POST", "/login"),
    ("GET", "/register"),
    ("POST", "/register"),
}
SIGNED_IN = {
    ("GET", "/account"),
    ("POST", "/account/password"),
    ("POST", "/logout"),
    ("GET", "/customers"),
    ("GET", "/customers/new"),
    ("POST", "/customers/new"),
    ("GET", "/customers/{customer_id}/edit"),
    ("POST", "/customers/{customer_id}/edit"),
    ("POST", "/customers/{customer_id}/deactivate"),
    ("POST", "/customers/{customer_id}/reactivate"),
    ("POST", "/customers/{customer_id}/remove"),
    ("GET", "/readings"),
    ("GET", "/readings/new/{customer_id}"),
    ("GET", "/readings/preview"),
    ("GET", "/readings/period"),
    ("POST", "/readings"),
    ("GET", "/readings/{reading_id}/edit"),
    ("POST", "/readings/{reading_id}/edit"),
    ("POST", "/readings/{reading_id}/delete"),
    ("GET", "/bills"),
    ("GET", "/bills/{bill_id}"),
    ("POST", "/bills/{bill_id}/resend"),
    ("GET", "/bills/{bill_id}/payments/preview"),
    ("POST", "/bills/{bill_id}/payments"),
    # the phone method (SMS-15, SMS-S4): owner and member both send messages
    ("GET", "/messages/to-send"),
    ("POST", "/messages/{message_id}/mark-sent"),
    ("GET", "/"),
    ("GET", "/reports"),
    ("GET", "/reports/arrears"),
    ("GET", "/reports/payments"),
    ("GET", "/reports/customers"),
    ("GET", "/reports/summary"),
}
# AUTHZ-1: account management, and nothing else
OWNER_ONLY = {
    ("POST", "/account/members"),
    ("POST", "/account/members/{member_id}/deactivate"),
    ("POST", "/account/members/{member_id}/reactivate"),
    ("POST", "/account/members/{member_id}/delete"),
    ("POST", "/account/members/{member_id}/password"),
}

HTMX = {"HX-Request": "true", "X-CSRF-Token": TEST_CSRF_TOKEN}


def app_routes(routes):
    """Every (method, path) the app answers. An included router keeps its own routes on
    original_router in this FastAPI version, so those are looked at too."""
    found = set()
    for route in routes:
        if isinstance(route, APIRoute):
            for method in route.methods:
                found.add((method, route.path))
        elif hasattr(route, "original_router"):
            found |= app_routes(route.original_router.routes)
    return found


def call(client, method, path, targets):
    url = path.replace("{member_id}", str(targets.member.user_id))
    url = url.replace("{customer_id}", str(targets.customer.customer_id))
    url = url.replace("{reading_id}", str(targets.reading.reading_id))
    url = url.replace("{bill_id}", str(targets.bill.bill_id))
    url = url.replace("{message_id}", str(targets.message.message_id))
    if path == "/readings/preview":
        url += f"?customer_id={targets.customer.customer_id}&current_reading=12"
    if path == "/bills/{bill_id}/payments/preview":
        url += "?amount=100"
    if method == "GET":
        return client.get(url, follow_redirects=False)
    # a valid CSRF token, so a 403 can only come from the role check
    return client.post(url, headers=HTMX, follow_redirects=False,
                       data={"first_name": "Amina", "last_name": "Hassan", "identifier": "0755123456",
                             "password": "lake-forest-77", "new_password": "lake-forest-77",
                             "current_password": "wrong-password-1", "phone": "0755123456",
                             "location": "Kiawara Estate", "meter_number": "MTR-0099",
                             "customer_id": str(targets.customer.customer_id), "current_reading": "12"})


class Targets:
    """The records a route acts on: a member for the account routes, a customer for the
    customer routes, a reading with its bill (not sent) for the reading and bill routes, and
    the bill's message waiting to be sent for the message routes."""

    def __init__(self, member, customer, reading, bill, message):
        self.member = member
        self.customer = customer
        self.reading = reading
        self.bill = bill
        self.message = message


@pytest.fixture
def targets(make_user, db_session, sms):
    member = make_user(role="member", first_name="Target", phone="254744000009")
    customer = Customer(first_name="Mary", last_name="Wanjiru", phone="254722999999",
                        location="Kiawara Estate", meter_number="MTR-0032")
    db_session.add(customer)
    db_session.flush()
    reading = MeterReading(customer_id=customer.customer_id, billing_period="2025-07", previous_reading=Decimal("0"),
                           current_reading=Decimal("10"), units_consumed=Decimal("10"),
                           reading_date=date(2025, 7, 30), recorded_by=member.user_id)
    db_session.add(reading)
    db_session.flush()
    bill = Bill(reading_id=reading.reading_id, customer_id=customer.customer_id, rate_per_unit=Decimal("130"),
                service_charge=Decimal("150"), units_consumed=Decimal("10"), consumption_charge=Decimal("1300"),
                total_due=Decimal("1450"))
    db_session.add(bill)
    db_session.flush()
    message = MessageLog(bill_id=bill.bill_id, customer_id=customer.customer_id, message_type="bill",
                         content="Hi Mary,", status="ready")
    db_session.add(message)
    db_session.flush()
    return Targets(member, customer, reading, bill, message)


def test_every_route_is_listed(web):
    # AUTH-S1: a route nobody thought about fails here before it can ship
    assert app_routes(web.app.routes) == PUBLIC | SIGNED_IN | OWNER_ONLY


def test_the_lists_do_not_overlap():
    assert not (PUBLIC & SIGNED_IN) and not (PUBLIC & OWNER_ONLY) and not (SIGNED_IN & OWNER_ONLY)


# ---- anonymous visitors are sent to login (AUTHZ-2) ----

@pytest.mark.parametrize("method,path", sorted(SIGNED_IN | OWNER_ONLY))
def test_an_anonymous_visitor_is_sent_to_login(web, targets, method, path):
    response = call(web, method, path, targets)

    if method == "POST" and response.status_code == 401:
        # an htmx request is redirected with a header instead
        assert response.headers["HX-Redirect"] == "/login"
    else:
        assert response.status_code == 303
        assert response.headers["location"] == "/login"


@pytest.mark.parametrize("path", sorted(path for method, path in PUBLIC if method == "GET"))
def test_the_public_pages_need_nobody_signed_in(web, path):
    # the public forms themselves are tested in test_auth_pages.py
    assert web.get(path, follow_redirects=False).status_code == 200


# ---- a member gets 403 on every owner route (AUTHZ-2, AUTHZ-5) ----

@pytest.mark.parametrize("method,path", sorted(OWNER_ONLY))
def test_a_member_gets_403_on_every_owner_route(web, make_user, sign_in_as, targets, db_session, method, path):
    member = make_user(role="member", first_name="Peter")
    sign_in_as(web, member)

    response = call(web, method, path, targets)

    assert response.status_code == 403
    denied = db_session.scalars(select(AuditLog).where(AuditLog.event_type == "auth.access_denied")).all()
    assert denied and denied[-1].actor_user_id == member.user_id
    # nothing was changed: no new account, the target member untouched
    db_session.refresh(targets.member)
    assert targets.member.status == "active"
    assert db_session.scalars(select(User).where(User.phone == "254755123456")).all() == []


# ---- owner and member both reach every billing route (PRD section 3) ----

@pytest.mark.parametrize("method,path", sorted(SIGNED_IN - {("POST", "/logout")}))
def test_a_member_reaches_every_signed_in_route(web, make_user, sign_in_as, targets, method, path):
    sign_in_as(web, make_user(role="member"))

    response = call(web, method, path, targets)

    # a save may redirect back to its list, but never to the login page
    assert response.status_code not in (401, 403)
    assert response.headers.get("location") != "/login"


@pytest.mark.parametrize("method,path", sorted((SIGNED_IN - {("POST", "/logout")}) | OWNER_ONLY))
def test_the_owner_reaches_every_route(web, make_user, sign_in_as, targets, method, path):
    sign_in_as(web, make_user(role="owner"))

    response = call(web, method, path, targets)

    # a save may redirect back to its list, but never to the login page
    assert response.status_code not in (401, 403)
    assert response.headers.get("location") != "/login"


def test_a_deactivated_owner_is_locked_out_too(web, make_user, sign_in_as):
    sign_in_as(web, make_user(role="owner", status="inactive"))

    response = web.get("/account", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/login"


# ---- every event built so far has a test that finds its row (AUDIT_LOG.md section 8) ----

def test_every_event_built_so_far_is_tested():
    tests_folder = pathlib.Path(__file__).parent
    all_test_code = " ".join(path.read_text(encoding="utf-8") for path in tests_folder.rglob("test_*.py")
                             if path.name != "test_permissions.py")
    events = [event for event in EVENT_LEVELS
              if event.startswith(("auth.", "user.", "customer.", "reading.", "bill.", "payment.", "sms.", "system."))]

    untested = [event for event in events if f'"{event}"' not in all_test_code]

    assert len(events) == 39
    assert untested == []

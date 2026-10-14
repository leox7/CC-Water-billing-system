"""customers/service.py (TASK-034, CUS-1 to CUS-4, IN-3 to IN-5)."""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.core.errors import BusinessRuleError, ConflictError, InternalError, NotFoundError
from app.features.customers import service as customers
from app.models import AuditLog, Bill, Customer, MessageLog, MeterReading


def audit_rows(db_session, event_type):
    return db_session.scalars(select(AuditLog).where(AuditLog.event_type == event_type)).all()


def typed(**changes):
    """What the owner typed into the customer form. Rate and service charge left empty."""
    values = {"first_name": "Mary", "last_name": "Wanjiru", "phone": "0722 123 456",
              "location": "Kiawara Estate", "meter_number": "MTR-0032", "rate_per_unit": "", "service_charge": ""}
    values.update(changes)
    return values


@pytest.fixture
def owner(make_user):
    return make_user(role="owner", first_name="Daniel", phone="254711000001")


@pytest.fixture
def member(make_user):
    return make_user(role="member", first_name="Peter", phone="254733000002")


@pytest.fixture
def mary(db_session, owner):
    return customers.create_customer(db_session, owner, typed())


def add_reading(db_session, customer, recorded_by):
    reading = MeterReading(customer_id=customer.customer_id, billing_period="2026-08",
                           previous_reading=Decimal("0"), current_reading=Decimal("10"),
                           units_consumed=Decimal("10"), reading_date=date(2026, 8, 20),
                           recorded_by=recorded_by.user_id)
    db_session.add(reading)
    db_session.flush()
    return reading


def add_bill(db_session, customer, reading):
    db_session.add(Bill(reading_id=reading.reading_id, customer_id=customer.customer_id,
                        rate_per_unit=Decimal("130"), service_charge=Decimal("150"),
                        units_consumed=Decimal("10"), consumption_charge=Decimal("1300"),
                        total_due=Decimal("1450")))
    db_session.flush()


def add_message(db_session, customer):
    # a message whose bill was deleted (COR-4): the bill link is empty
    db_session.add(MessageLog(customer_id=customer.customer_id, message_type="bill", content="C&C H2O"))
    db_session.flush()


# ---- adding a customer (CUS-1) ----

def test_the_owner_adds_a_customer(db_session, owner):
    customer = customers.create_customer(db_session, owner, typed(first_name="  Mary ", location=" Kiawara Estate "))

    assert customer.customer_id is not None
    assert customer.first_name == "Mary" and customer.last_name == "Wanjiru"
    assert customer.location == "Kiawara Estate"
    assert customer.status == "active"


def test_an_empty_rate_and_service_charge_take_the_defaults(db_session, owner):
    customer = customers.create_customer(db_session, owner, typed())

    assert customer.rate_per_unit == Decimal("130.00")
    assert customer.service_charge == Decimal("150.00")


def test_a_rate_and_service_charge_can_be_set(db_session, owner):
    customer = customers.create_customer(db_session, owner, typed(rate_per_unit="1,300.50", service_charge="0"))

    assert customer.rate_per_unit == Decimal("1300.50")
    assert customer.service_charge == Decimal("0")


def test_a_new_customer_is_recorded(db_session, owner):
    customer = customers.create_customer(db_session, owner, typed(rate_per_unit="140"))

    row = audit_rows(db_session, "customer.created")[0]
    assert row.entity_type == "customer" and row.entity_id == customer.customer_id
    assert row.customer_id == customer.customer_id
    assert row.actor_user_id == owner.user_id
    assert row.details == {"meter_number": "MTR-0032", "rate_per_unit": "140", "service_charge": "150.00"}
    assert row.summary == "Customer MTR-0032 added"


def test_the_status_cannot_be_set_from_the_form(db_session, owner):
    # IN-7: only the named fields are read
    customer = customers.create_customer(db_session, owner, typed(status="inactive", customer_id="999"))

    assert customer.status == "active"
    assert customer.customer_id != 999


# ---- phone numbers (IN-3) ----

@pytest.mark.parametrize("phone,stored", [
    ("0722123456", "254722123456"),
    ("0722 123 456", "254722123456"),
    ("0110 123 456", "254110123456"),
    ("+254 722 123 456", "254722123456"),
    ("254110123456", "254110123456"),
])
def test_07_and_01_phone_numbers_are_saved_in_one_format(db_session, owner, phone, stored):
    customer = customers.create_customer(db_session, owner, typed(phone=phone))

    assert customer.phone == stored


@pytest.mark.parametrize("phone", ["", "020 123 4567", "0722 123", "0822123456", "07221234567", "phone"])
def test_a_phone_that_is_not_a_kenyan_mobile_is_refused(db_session, owner, phone):
    with pytest.raises(BusinessRuleError) as raised:
        customers.create_customer(db_session, owner, typed(phone=phone))

    assert raised.value.field == "phone"
    assert "Kenyan mobile number" in raised.value.message


# ---- names and location (IN-4) ----

@pytest.mark.parametrize("field", ["first_name", "last_name", "location"])
def test_a_missing_name_or_location_is_refused(db_session, owner, field):
    with pytest.raises(BusinessRuleError) as raised:
        customers.create_customer(db_session, owner, typed(**{field: "   "}))

    assert raised.value.field == field
    assert raised.value.message.startswith("Please enter the")


@pytest.mark.parametrize("field,limit", [("first_name", 50), ("last_name", 50), ("location", 100)])
def test_a_name_or_location_that_is_too_long_is_refused(db_session, owner, field, limit):
    customers.create_customer(db_session, owner, typed(**{field: "a" * limit}))

    with pytest.raises(BusinessRuleError) as raised:
        customers.create_customer(db_session, owner, typed(**{field: "a" * (limit + 1)}, meter_number="MTR-2"))

    assert raised.value.field == field
    assert "too long" in raised.value.message


def test_line_breaks_and_control_characters_are_taken_out(db_session, owner):
    # IN-4: they would break the layout of the SMS
    customer = customers.create_customer(db_session, owner, typed(first_name="Ma\nry", location="Kiawara\r\nEstate\t"))

    assert customer.first_name == "Mary"
    assert customer.location == "KiawaraEstate"


# ---- meter numbers (IN-5) ----

@pytest.mark.parametrize("meter_number,stored", [
    ("mtr-0032", "MTR-0032"),
    (" 32 ", "32"),
    ("kia/07-b", "KIA/07-B"),
    ("A" * 20, "A" * 20),
])
def test_a_meter_number_is_saved_in_capitals(db_session, owner, meter_number, stored):
    customer = customers.create_customer(db_session, owner, typed(meter_number=meter_number))

    assert customer.meter_number == stored


@pytest.mark.parametrize("meter_number,words", [
    ("", "Please enter the meter number"),
    ("A" * 21, "too long"),
    ("MTR 0032", "letters, numbers, - and /"),
    ("MTR_0032", "letters, numbers, - and /"),
    ("MTR#32", "letters, numbers, - and /"),
])
def test_a_bad_meter_number_is_refused(db_session, owner, meter_number, words):
    with pytest.raises(BusinessRuleError) as raised:
        customers.create_customer(db_session, owner, typed(meter_number=meter_number))

    assert raised.value.field == "meter_number"
    assert words in raised.value.message


def test_a_meter_number_in_use_is_refused_and_names_who_has_it(db_session, owner, mary):
    with pytest.raises(ConflictError) as raised:
        customers.create_customer(db_session, owner, typed(first_name="John", meter_number="mtr-0032"))

    assert raised.value.status_code == 409
    assert raised.value.field == "meter_number"
    assert raised.value.message == "Meter number MTR-0032 already belongs to Mary Wanjiru."


# ---- rate and service charge (IN-2) ----

@pytest.mark.parametrize("field,value,words", [
    ("rate_per_unit", "0", "must be more than 0"),
    ("rate_per_unit", "-5", "cannot be less than 0"),
    ("rate_per_unit", "abc", "as a number"),
    ("rate_per_unit", "NaN", "as a number"),
    ("rate_per_unit", "Infinity", "as a number"),
    ("rate_per_unit", "130.505", "at most 2 decimal places"),
    ("rate_per_unit", "100000000", "too large"),
    ("service_charge", "-1", "cannot be less than 0"),
    ("service_charge", "Ksh 150", "as a number"),
])
def test_a_bad_rate_or_service_charge_is_refused(db_session, owner, field, value, words):
    with pytest.raises(BusinessRuleError) as raised:
        customers.create_customer(db_session, owner, typed(**{field: value}))

    assert raised.value.field == field
    assert words in raised.value.message


def test_a_refused_customer_is_not_saved(db_session, owner):
    with pytest.raises(BusinessRuleError):
        customers.create_customer(db_session, owner, typed(rate_per_unit="abc"))

    assert db_session.scalars(select(Customer)).all() == []
    assert audit_rows(db_session, "customer.created") == []


# ---- changing a customer (CUS-2) ----

def test_the_owner_changes_a_customers_details(db_session, owner, mary):
    customers.update_customer(db_session, owner, mary.customer_id,
                              typed(location="Muthaiga Rd", rate_per_unit="140", phone="0733 000 111"))

    db_session.refresh(mary)
    assert mary.location == "Muthaiga Rd"
    assert mary.rate_per_unit == Decimal("140.00")
    assert mary.phone == "254733000111"


def test_a_change_records_only_what_changed(db_session, owner, mary):
    customers.update_customer(db_session, owner, mary.customer_id, typed(location="Muthaiga Rd", rate_per_unit="140"))

    row = audit_rows(db_session, "customer.updated")[0]
    assert row.details == {
        "fields": ["location", "rate_per_unit"],
        "location": {"old": "Kiawara Estate", "new": "Muthaiga Rd"},
        "rate_per_unit": {"old": "130.00", "new": "140"},
    }
    assert row.customer_id == mary.customer_id and row.entity_id == mary.customer_id
    assert row.summary == "Customer MTR-0032 changed: location, rate_per_unit"


def test_a_new_phone_number_is_never_written_to_the_audit_log(db_session, owner, mary):
    customers.update_customer(db_session, owner, mary.customer_id, typed(phone="0733 000 111"))

    row = audit_rows(db_session, "customer.updated")[0]
    assert row.details == {"fields": ["phone"]}
    for number in ("254733000111", "0733000111", "254722123456", "0722123456"):
        assert number not in str(row.details) + row.summary


def test_saving_without_changes_records_nothing(db_session, owner, mary):
    # 130 and 130.00 are the same amount
    customers.update_customer(db_session, owner, mary.customer_id, typed(rate_per_unit="130", phone="+254722123456"))

    assert audit_rows(db_session, "customer.updated") == []


def test_a_customer_keeps_their_own_meter_number(db_session, owner, mary):
    customers.update_customer(db_session, owner, mary.customer_id, typed(location="Muthaiga Rd"))

    db_session.refresh(mary)
    assert mary.meter_number == "MTR-0032"


def test_a_change_to_another_customers_meter_number_is_refused(db_session, owner, mary):
    john = customers.create_customer(db_session, owner, typed(first_name="John", meter_number="MTR-0014"))

    with pytest.raises(ConflictError) as raised:
        customers.update_customer(db_session, owner, john.customer_id, typed(first_name="John", meter_number="MTR-0032"))

    assert "Mary Wanjiru" in raised.value.message
    db_session.refresh(john)
    assert john.meter_number == "MTR-0014"


def test_a_bad_change_leaves_the_customer_as_it_was(db_session, owner, mary):
    with pytest.raises(BusinessRuleError):
        customers.update_customer(db_session, owner, mary.customer_id, typed(location="Muthaiga Rd", phone="123"))

    db_session.refresh(mary)
    assert mary.location == "Kiawara Estate"


def test_an_inactive_customer_can_still_be_changed(db_session, owner, mary):
    customers.deactivate_customer(db_session, owner, mary.customer_id)

    customers.update_customer(db_session, owner, mary.customer_id, typed(location="Muthaiga Rd"))

    db_session.refresh(mary)
    assert mary.location == "Muthaiga Rd" and mary.status == "inactive"


def test_changing_an_unknown_customer_is_a_404(db_session, owner):
    with pytest.raises(NotFoundError):
        customers.update_customer(db_session, owner, 999999, typed())


# ---- deactivate and reactivate (CUS-3, CUS-4) ----

def test_the_owner_deactivates_and_reactivates_a_customer(db_session, owner, mary):
    customers.deactivate_customer(db_session, owner, mary.customer_id)
    assert mary.status == "inactive"

    customers.reactivate_customer(db_session, owner, mary.customer_id)
    assert mary.status == "active"

    deactivated = audit_rows(db_session, "customer.deactivated")
    reactivated = audit_rows(db_session, "customer.reactivated")
    assert len(deactivated) == 1 and len(reactivated) == 1
    assert deactivated[0].customer_id == mary.customer_id and deactivated[0].entity_type == "customer"
    assert reactivated[0].summary == "Customer MTR-0032 reactivated"


def test_deactivating_twice_records_it_once(db_session, owner, mary):
    customers.deactivate_customer(db_session, owner, mary.customer_id)
    customers.deactivate_customer(db_session, owner, mary.customer_id)
    customers.reactivate_customer(db_session, owner, mary.customer_id)
    customers.reactivate_customer(db_session, owner, mary.customer_id)

    assert len(audit_rows(db_session, "customer.deactivated")) == 1
    assert len(audit_rows(db_session, "customer.reactivated")) == 1


@pytest.mark.parametrize("action", ["deactivate_customer", "reactivate_customer", "remove_customer"])
def test_an_unknown_customer_is_a_404(db_session, owner, action):
    with pytest.raises(NotFoundError) as raised:
        getattr(customers, action)(db_session, owner, 999999)

    assert raised.value.message == "We could not find that customer."


# ---- removing a customer with no history (CUS-3) ----

def test_a_customer_with_no_history_is_removed(db_session, owner, mary):
    customer_id = mary.customer_id

    name = customers.remove_customer(db_session, owner, customer_id)

    assert name == "Mary Wanjiru"
    assert db_session.get(Customer, customer_id) is None
    row = audit_rows(db_session, "customer.removed")[0]
    assert row.details == {"meter_number": "MTR-0032"}
    assert row.customer_id == customer_id and row.entity_id == customer_id


@pytest.mark.parametrize("history", ["reading", "bill", "message"])
def test_a_customer_with_history_is_never_removed(db_session, owner, mary, history):
    if history == "message":
        add_message(db_session, mary)
    else:
        reading = add_reading(db_session, mary, owner)
        if history == "bill":
            add_bill(db_session, mary, reading)

    with pytest.raises(BusinessRuleError) as raised:
        customers.remove_customer(db_session, owner, mary.customer_id)

    assert "Deactivate the customer instead" in raised.value.message
    assert db_session.get(Customer, mary.customer_id) is not None
    assert audit_rows(db_session, "customer.removed") == []


# ---- a member does the same customer work as the owner (PRD section 3) ----

def test_a_member_adds_edits_deactivates_reactivates_and_removes_customers(db_session, member):
    customer = customers.create_customer(db_session, member, typed(meter_number="MTR-0099"))
    customers.update_customer(db_session, member, customer.customer_id, typed(meter_number="MTR-0099",
                                                                              location="Muthaiga Rd"))
    customers.deactivate_customer(db_session, member, customer.customer_id)
    customers.reactivate_customer(db_session, member, customer.customer_id)
    customer_id = customer.customer_id
    customers.remove_customer(db_session, member, customer_id)

    assert db_session.get(Customer, customer_id) is None
    for event in ("customer.created", "customer.updated", "customer.deactivated", "customer.reactivated",
                  "customer.removed"):
        row = audit_rows(db_session, event)[0]
        assert row.actor_user_id == member.user_id and row.actor_label == "Peter (member)"
        assert row.customer_id == customer_id
    assert audit_rows(db_session, "auth.access_denied") == []


# ---- the list and search ----

@pytest.fixture
def several(db_session, owner):
    made = {}
    for first, last, location, meter in [
        ("Mary", "Wanjiru", "Kiawara Estate", "MTR-0032"),
        ("John", "Mwangi", "Kiawara Estate", "MTR-0014"),
        ("Grace", "Njeri", "Muthaiga Rd", "MTR-0027"),
        ("Peter", "Kamau", "Muthaiga Rd", "MTR-0008"),
    ]:
        made[first] = customers.create_customer(
            db_session, owner, typed(first_name=first, last_name=last, location=location, meter_number=meter))
    customers.deactivate_customer(db_session, owner, made["Peter"].customer_id)
    return made


def names(rows):
    return [row.customer.first_name for row in rows]


def test_the_list_shows_active_customers_first_then_by_name(db_session, several):
    assert names(customers.list_customers(db_session)) == ["Grace", "John", "Mary", "Peter"]


@pytest.mark.parametrize("search,expected", [
    ("mary", ["Mary"]),
    ("Mary Wanjiru", ["Mary"]),
    ("  njeri ", ["Grace"]),
    ("mtr-0014", ["John"]),
    ("0008", ["Peter"]),
    ("kiawara", ["John", "Mary"]),
    ("Muthaiga", ["Grace", "Peter"]),
    ("nobody", []),
    ("%", []),
    ("_", []),
    ("", ["Grace", "John", "Mary", "Peter"]),
])
def test_search_matches_the_name_meter_number_or_location(db_session, several, search, expected):
    assert names(customers.list_customers(db_session, search)) == expected


def test_a_search_that_is_too_long_is_refused(db_session):
    with pytest.raises(BusinessRuleError) as raised:
        customers.list_customers(db_session, "a" * 101)

    assert raised.value.field == "search"


def test_the_list_is_limited_in_size(db_session, owner, monkeypatch, several):
    monkeypatch.setattr(customers, "LIST_LIMIT", 2)

    assert len(customers.list_customers(db_session)) == 2


def test_the_list_says_who_has_history(db_session, owner, several):
    add_reading(db_session, several["Mary"], owner)
    add_message(db_session, several["Grace"])

    history = {row.customer.first_name: row.has_history for row in customers.list_customers(db_session)}

    assert history == {"Grace": True, "John": False, "Mary": True, "Peter": False}
    assert customers.customer_row(db_session, several["Mary"]).has_history is True
    assert customers.customer_row(db_session, several["John"]).has_history is False


def test_the_active_customers_are_counted(db_session, several):
    assert customers.count_active_customers(db_session) == 3


def test_get_customer_finds_one_or_says_it_is_not_found(db_session, mary):
    assert customers.get_customer(db_session, mary.customer_id) is mary

    with pytest.raises(NotFoundError):
        customers.get_customer(db_session, 999999)


# ---- unexpected failures become a safe 500 ----

def test_a_database_failure_while_adding_is_a_safe_500(db_session, owner, monkeypatch):
    def broken_flush(*args, **kwargs):
        raise RuntimeError("database is down")

    monkeypatch.setattr(db_session, "flush", broken_flush)

    with pytest.raises(InternalError) as raised:
        customers.create_customer(db_session, owner, typed())

    assert raised.value.status_code == 500
    assert raised.value.message == "Something went wrong on our side. Please try again."


@pytest.mark.parametrize("action", [
    lambda db, owner, customer: customers.update_customer(db, owner, customer.customer_id, typed(location="X")),
    lambda db, owner, customer: customers.deactivate_customer(db, owner, customer.customer_id),
    lambda db, owner, customer: customers.remove_customer(db, owner, customer.customer_id),
])
def test_a_database_failure_while_changing_is_a_safe_500(db_session, owner, mary, monkeypatch, action):
    def broken_commit():
        raise RuntimeError("database is down")

    monkeypatch.setattr(db_session, "commit", broken_commit)

    with pytest.raises(InternalError) as raised:
        action(db_session, owner, mary)

    assert raised.value.status_code == 500


@pytest.mark.parametrize("reader", [
    lambda db: customers.list_customers(db),
    lambda db: customers.count_active_customers(db),
    lambda db: customers.get_customer(db, 1),
])
def test_a_database_failure_while_reading_is_a_safe_500(db_session, monkeypatch, reader):
    def broken(*args, **kwargs):
        raise RuntimeError("database is down")

    monkeypatch.setattr(db_session, "scalars", broken)
    monkeypatch.setattr(db_session, "scalar", broken)
    monkeypatch.setattr(db_session, "get", broken)

    with pytest.raises(InternalError) as raised:
        reader(db_session)

    assert raised.value.status_code == 500

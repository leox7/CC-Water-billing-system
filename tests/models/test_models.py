from app.core.database import Base
from app.models import AuditLog, Bill, Customer, MessageLog, MeterReading, Payment, User

EXPECTED_PRIMARY_KEYS = {
    "users": "user_id",
    "customers": "customer_id",
    "meter_readings": "reading_id",
    "bills": "bill_id",
    "payments": "payment_id",
    "messages_log": "message_id",
    "audit_log": "audit_id",
}


def test_there_are_exactly_the_seven_tables_in_the_prd():
    assert set(Base.metadata.tables) == set(EXPECTED_PRIMARY_KEYS)


def test_primary_keys_are_named_after_the_table():
    for table_name, key_name in EXPECTED_PRIMARY_KEYS.items():
        key_columns = [column.name for column in Base.metadata.tables[table_name].primary_key]
        assert key_columns == [key_name]


def test_money_columns_are_decimal_never_float():
    money_columns = {
        "customers": ["rate_per_unit", "service_charge"],
        "bills": [
            "rate_per_unit", "service_charge", "units_consumed", "consumption_charge",
            "previous_balance", "total_due", "amount_paid",
        ],
        "payments": ["amount"],
    }
    for table_name, column_names in money_columns.items():
        table = Base.metadata.tables[table_name]
        for column_name in column_names:
            assert table.c[column_name].type.__class__.__name__ == "Numeric"


def test_the_rules_the_database_backs_up_are_declared():
    names = {
        constraint.name
        for table in Base.metadata.tables.values()
        for constraint in table.constraints
        if constraint.name
    }
    assert {
        "uq_customers_meter_number",   # CUS-1
        "uq_readings_customer_period", # RD-4
        "ck_readings_not_lower",       # RD-3
        "uq_bills_reading",            # BILL-6
        "ck_payments_amount_positive", # PAY-4
        "uq_payments_mpesa_reference", # PAY-8
        "ck_users_phone_or_email",     # AUTH-1
    } <= names


def test_a_message_keeps_its_row_when_the_bill_is_deleted():
    # COR-4: the bill link is emptied, not the message deleted
    foreign_key = next(iter(MessageLog.__table__.c.bill_id.foreign_keys))
    assert foreign_key.ondelete == "SET NULL"
    assert MessageLog.__table__.c.bill_id.nullable is True


def test_audit_log_has_no_foreign_keys():
    assert AuditLog.__table__.foreign_keys == set()


def test_every_model_class_is_importable():
    assert all([AuditLog, Bill, Customer, MessageLog, MeterReading, Payment, User])

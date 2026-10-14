# Importing every model here is what lets Alembic see all the tables.
from app.models.audit_log import AuditLog
from app.models.bill import Bill
from app.models.customer import Customer
from app.models.message_log import MessageLog
from app.models.meter_reading import MeterReading
from app.models.payment import Payment
from app.models.user import User

__all__ = ["AuditLog", "Bill", "Customer", "MessageLog", "MeterReading", "Payment", "User"]

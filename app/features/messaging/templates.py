"""The text of the bill SMS and the correction SMS (PRD section 4.6). The wording and the
line order are decided, so keep them exactly as written. Also the sms: link and the
fingerprint the phone method uses (SMS-16, SMS-17).

Every figure comes from the saved reading and bill, never from the customer's current
rate, so an old bill always reads the way it was sent (SMS-10). Money uses the same
format as the screens, so the SMS and the bill page always agree. These are small pure
helpers with no database or network access.
"""

import hashlib
from decimal import Decimal
from urllib.parse import quote

from app.core.templating import ksh, money_figure, plain_number
from app.models import Bill, Customer, MeterReading

BUSINESS_LINE = "C&C H2O WATER SUPPLIES"
PAYMENT_LINE = "Payments via Mpesa: 0722995388"


def bill_sms(customer: Customer, reading: MeterReading, bill: Bill) -> str:
    """The bill SMS. SMS-13: the customer's first name, money with a thousands comma."""
    lines = [BUSINESS_LINE, f"Hi {customer.first_name},", *_bill_lines(reading, bill), PAYMENT_LINE]
    return "\n".join(lines)


def correction_sms(customer: Customer, reading: MeterReading, bill: Bill, balance: Decimal) -> str:
    """SMS-12: the full corrected bill, sent after a reading whose bill SMS already went out is
    corrected. `balance` is what is still owed, worked out by bills/service.py so the rule
    lives in one place."""
    lines = [BUSINESS_LINE, f"Hi {customer.first_name},", "We corrected your meter reading.",
             *_bill_lines(reading, bill)]
    # only when a payment is already recorded on the bill
    if bill.amount_paid > 0:
        lines.append(f"Paid so far: {ksh(bill.amount_paid)}")
        lines.append(f"Balance: {ksh(balance)}")
    lines.append(PAYMENT_LINE)
    return "\n".join(lines)


def _bill_lines(reading: MeterReading, bill: Bill) -> list[str]:
    # the lines both messages share, in the decided order
    lines = [
        f"Previous Reading:{plain_number(reading.previous_reading)}",
        f"Current Reading:{plain_number(reading.current_reading)}",
        f"Consumption (m3):{plain_number(bill.units_consumed)}",
        f"Consumption({money_figure(bill.rate_per_unit)}ksh/m3):{money_figure(bill.consumption_charge)}",
        # SMS-7: always shown, so the customer can see how the total is built
        f"Fixed Charge per month: {ksh(bill.service_charge)}",
    ]
    # SMS-8: only when something is carried over, otherwise the line is left out completely
    if bill.previous_balance > 0:
        lines.append(f"Previous Balance: {ksh(bill.previous_balance)}")
    # SMS-9: the full total
    lines.append(f"Amount Due: {ksh(bill.total_due)}")
    return lines


def sms_link(phone: str, text: str) -> str:
    """SMS-16: sms:+254722123456?&body=... opens the phone's messaging app with the number and
    the whole text filled in. `phone` is the number stored on the customer and `text` the
    message the server built from the saved bill, never anything from the browser (SMS-S2).
    Every character but letters, digits and -._~ is percent-encoded, so a line break
    becomes %0A and &, #, %, + and quotes cannot end the body early. "?&body=" rather than
    "?body=" is the form both Android and iPhone messaging apps read."""
    return f"sms:+{phone}?&body={quote(text, safe='')}"


def text_fingerprint(text: str) -> str:
    """SMS-17: a SHA-256 of the message text. The Messages to send form carries it, so Mark
    as sent can tell whether the text the server builds now is the one the user saw."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()

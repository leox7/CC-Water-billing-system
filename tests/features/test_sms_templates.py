"""messaging/templates.py (TASK-039, SMS-7 to SMS-13). No database needed: the texts are
built from records that are never saved."""

import math
from decimal import Decimal
from urllib.parse import unquote

import pytest

from app.features.messaging import templates
from app.models import Bill, Customer, MeterReading


def customer(**changes):
    values = {"first_name": "Mary", "last_name": "Wanjiru", "phone": "254722123456", "location": "Kiawara Estate",
              "meter_number": "MTR-0032", "rate_per_unit": Decimal("130.00"), "service_charge": Decimal("150.00")}
    values.update(changes)
    return Customer(**values)


def reading(previous="120.00", current="135.00"):
    return MeterReading(previous_reading=Decimal(previous), current_reading=Decimal(current),
                        units_consumed=Decimal(current) - Decimal(previous))


def bill(units="15.00", rate="130.00", service="150.00", balance="0.00", paid="0.00"):
    consumption = Decimal(units) * Decimal(rate)
    return Bill(units_consumed=Decimal(units), rate_per_unit=Decimal(rate), service_charge=Decimal(service),
                consumption_charge=consumption, previous_balance=Decimal(balance),
                total_due=consumption + Decimal(service) + Decimal(balance), amount_paid=Decimal(paid))


def segments(text):
    # GSM-7: one SMS holds 160 characters, a longer one is split into parts of 153
    return 1 if len(text) <= 160 else math.ceil(len(text) / 153)


# ---- the bill SMS ----

def test_the_bill_sms_without_a_balance_is_exactly_the_decided_text():
    # PRD 4.5 worked example: 120 to 135 is 15 units, 15 x 130 = 1,950, plus 150 = 2,100
    text = templates.bill_sms(customer(), reading(), bill())

    assert text == (
        "C&C H2O WATER SUPPLIES\n"
        "Hi Mary,\n"
        "Previous Reading:120\n"
        "Current Reading:135\n"
        "Consumption (m3):15\n"
        "Consumption(130ksh/m3):1,950\n"
        "Fixed Charge per month: Ksh 150\n"
        "Amount Due: Ksh 2,100\n"
        "Payments via Mpesa: 0722995388"
    )


def test_the_previous_balance_line_sits_between_the_fixed_charge_and_the_amount_due():
    text = templates.bill_sms(customer(), reading(), bill(balance="500.00"))

    lines = text.split("\n")
    assert lines[6:9] == ["Fixed Charge per month: Ksh 150", "Previous Balance: Ksh 500", "Amount Due: Ksh 2,600"]


def test_a_balance_of_zero_leaves_the_line_out_completely():
    text = templates.bill_sms(customer(), reading(), bill(balance="0.00"))

    assert "Previous Balance" not in text
    assert "Balance" not in text


def test_the_rate_and_service_charge_come_from_the_bill_not_the_customer():
    # SMS-10: the customer's rate changed after the bill was made
    today = customer(rate_per_unit=Decimal("160.00"), service_charge=Decimal("200.00"))

    text = templates.bill_sms(today, reading(), bill(rate="130.00", service="150.00"))

    assert "Consumption(130ksh/m3):1,950" in text
    assert "Fixed Charge per month: Ksh 150" in text
    assert "160" not in text and "200" not in text


def test_the_sms_greets_the_customer_by_first_name_only():
    text = templates.bill_sms(customer(first_name="Grace", last_name="Njeri"), reading(), bill())

    assert "Hi Grace,\n" in text
    assert "Njeri" not in text


def test_numbers_have_a_thousands_comma_and_cents_only_when_there_are_some():
    # SMS-13: readings as stored without trailing zeros, money with cents only when needed
    text = templates.bill_sms(customer(), reading("1200.50", "1300.75"),
                              bill(units="100.25", rate="130.50", balance="1234.40"))

    assert "Previous Reading:1200.5\n" in text
    assert "Current Reading:1300.75\n" in text
    assert "Consumption (m3):100.25\n" in text
    # 100.25 x 130.50 = 13,082.625, shown to the cent
    assert "Consumption(130.50ksh/m3):13,082.63\n" in text
    assert "Previous Balance: Ksh 1,234.40\n" in text


def test_zero_consumption_still_shows_every_line():
    text = templates.bill_sms(customer(), reading("135.00", "135.00"), bill(units="0.00"))

    assert "Consumption (m3):0\n" in text
    assert "Consumption(130ksh/m3):0\n" in text
    assert "Amount Due: Ksh 150\n" in text


def test_the_bill_sms_fits_in_two_segments_and_three_with_a_balance():
    # SMS-11: about 2 segments, and still 2 or 3 with the balance line, even with long figures
    long_name = customer(first_name="Christopher")
    plain = templates.bill_sms(long_name, reading("12345.50", "12999.75"), bill(units="654.25"))
    with_balance = templates.bill_sms(long_name, reading("12345.50", "12999.75"),
                                      bill(units="654.25", balance="123456.50"))

    assert segments(plain) <= 2
    assert segments(with_balance) <= 3


# ---- the correction SMS (SMS-12) ----

def test_the_correction_sms_is_the_full_corrected_bill():
    # SMS-12
    text = templates.correction_sms(customer(), reading(current="140.00"), bill(units="20.00"), Decimal("2750"))

    assert text == (
        "C&C H2O WATER SUPPLIES\n"
        "Hi Mary,\n"
        "We corrected your meter reading.\n"
        "Previous Reading:120\n"
        "Current Reading:140\n"
        "Consumption (m3):20\n"
        "Consumption(130ksh/m3):2,600\n"
        "Fixed Charge per month: Ksh 150\n"
        "Amount Due: Ksh 2,750\n"
        "Payments via Mpesa: 0722995388"
    )


def test_the_correction_sms_has_the_previous_balance_line_only_when_above_zero():
    with_balance = templates.correction_sms(customer(), reading(current="140.00"),
                                            bill(units="20.00", balance="500.00"), Decimal("3250"))

    lines = with_balance.split("\n")
    assert lines[7:10] == ["Fixed Charge per month: Ksh 150", "Previous Balance: Ksh 500", "Amount Due: Ksh 3,250"]


def test_the_correction_sms_shows_paid_so_far_and_the_balance_once_a_payment_is_recorded():
    text = templates.correction_sms(customer(), reading(current="140.00"),
                                    bill(units="20.00", paid="1000.00"), Decimal("1750"))

    lines = text.split("\n")
    assert lines[8:11] == ["Amount Due: Ksh 2,750", "Paid so far: Ksh 1,000", "Balance: Ksh 1,750"]
    assert lines[-1] == "Payments via Mpesa: 0722995388"


def test_the_longest_correction_sms_stays_within_three_segments():
    # SMS-12: with every line it is about 3 segments
    text = templates.correction_sms(customer(first_name="Christopher"), reading("12345.50", "12999.75"),
                                    bill(units="654.25", balance="123456.50", paid="1000.00"), Decimal("208521.13"))

    assert segments(text) <= 3


# ---- the sms: link for the phone method (SMS-16) ----

def link_parts(link):
    """The number and the decoded text of an sms: link."""
    assert link.startswith("sms:")
    number, body = link[len("sms:"):].split("?&body=", 1)
    return number, unquote(body)


def test_the_link_has_the_number_in_international_form_and_the_body_after_it():
    link = templates.sms_link("254722123456", "Hi")

    assert link == "sms:+254722123456?&body=Hi"


def test_the_whole_bill_sms_survives_the_link_with_its_line_breaks():
    text = templates.bill_sms(customer(), reading(), bill(balance="1200"))
    link = templates.sms_link("254722123456", text)

    number, body = link_parts(link)

    assert number == "+254722123456"
    assert body == text
    # each line break is kept as %0A, never as a raw break or a + that some apps read as a space
    assert link.count("%0A") == text.count("\n") == 9


@pytest.mark.parametrize("text", [
    "Line one\nLine two\r\nLine three",
    "C&C H2O & sons",
    "Meter #12",
    "100% paid",
    "1+1 = 2",
    "He said \"pay\" and 'now'",
    "a?b=c&body=evil",
    "Ksh 2,100.50 / month; thanks!",
])
def test_special_characters_survive_the_link(text):
    link = templates.sms_link("254722123456", text)

    assert link_parts(link) == ("+254722123456", text)
    # none of them can end the body early or start a second parameter
    body = link.split("?&body=", 1)[1]
    for raw in ("&", "#", "+", "\n", " ", '"', "'", "?", "="):
        assert raw not in body


def test_a_message_of_700_characters_survives_the_link():
    text = ("C&C H2O 100% #1 + \"x\"\n" * 40)[:700]
    assert len(text) == 700

    assert link_parts(templates.sms_link("254722123456", text))[1] == text


def test_the_longest_correction_sms_survives_the_link():
    text = templates.correction_sms(customer(first_name="Bartholomew"), reading("99999.50", "100234.75"),
                                    bill(units="235.25", rate="1250.50", service="1500", balance="1234567.89",
                                         paid="250000"), Decimal("1234567.89"))

    assert link_parts(templates.sms_link("254722123456", text))[1] == text


"""Cleaning and checking what people type (SECURITY.md IN-3, IN-4)."""

import pytest

from app.core.errors import BusinessRuleError
from app.core.validation import (
    clean_name,
    clean_text,
    normalise_email,
    normalise_phone,
    parse_phone_or_email,
)


@pytest.mark.parametrize(
    "typed,stored",
    [
        ("0722123456", "254722123456"),
        ("0112345678", "254112345678"),
        ("0722 123 456", "254722123456"),
        ("0722-123-456", "254722123456"),
        (" 0722123456 ", "254722123456"),
        ("254722123456", "254722123456"),
        ("+254722123456", "254722123456"),
        ("+254 722 123 456", "254722123456"),
        ("(0722) 123456", "254722123456"),
    ],
)
def test_kenyan_mobile_numbers_are_stored_in_one_format(typed, stored):
    assert normalise_phone(typed) == stored


@pytest.mark.parametrize(
    "bad",
    ["", "0722", "072212345", "07221234567", "0622123456", "0202123456", "+1 555 123 4567",
     "2547221234", "0722l23456", "0722123456\n0733000000"],
)
def test_anything_that_is_not_a_kenyan_mobile_is_refused(bad):
    with pytest.raises(BusinessRuleError, match="Kenyan mobile number") as raised:
        normalise_phone(bad, field="phone")

    assert raised.value.field == "phone"


def test_line_breaks_and_control_characters_are_removed_and_ends_trimmed():
    assert clean_text("  Mary\nWanjiru\t\x00 ") == "MaryWanjiru"


def test_a_name_is_cleaned():
    assert clean_name("  Mary  ", "first_name", "first name") == "Mary"


def test_an_empty_name_is_refused_beside_its_field():
    with pytest.raises(BusinessRuleError, match="Please enter the first name") as raised:
        clean_name("   \n ", "first_name", "first name")

    assert raised.value.field == "first_name"


def test_a_name_over_fifty_characters_is_refused():
    clean_name("a" * 50, "last_name", "last name")

    with pytest.raises(BusinessRuleError, match="50 characters or fewer"):
        clean_name("a" * 51, "last_name", "last name")


def test_an_email_is_stored_in_lower_case():
    assert normalise_email("  Mary.Wanjiru@Example.COM ") == "mary.wanjiru@example.com"


@pytest.mark.parametrize("bad", ["mary", "mary@", "@example.com", "mary@example", "mary wanjiru@example.com",
                                 "a@b.c" + "x" * 120])
def test_something_that_is_not_an_email_is_refused(bad):
    with pytest.raises(BusinessRuleError, match="email address"):
        normalise_email(bad)


def test_the_phone_or_email_box_reads_a_phone_number():
    assert parse_phone_or_email("0722 123 456") == ("254722123456", None)


def test_the_phone_or_email_box_reads_an_email():
    assert parse_phone_or_email("Owner@Example.com") == (None, "owner@example.com")


@pytest.mark.parametrize("bad", ["", "   ", "hello", "12345"])
def test_the_phone_or_email_box_refuses_anything_else_in_plain_words(bad):
    with pytest.raises(BusinessRuleError) as raised:
        parse_phone_or_email(bad)

    assert raised.value.field == "identifier"
    assert "number" in raised.value.message and "email" in raised.value.message

from decimal import Decimal

import pytest

from datetime import date, datetime

from app.core.templating import (
    create_templates,
    date_label,
    date_time_label,
    ksh,
    mask_phone,
    money_figure,
    period_label,
    plain_number,
)


@pytest.mark.parametrize(
    "value,expected",
    [
        (4290, "Ksh 4,290"),
        (Decimal("4290.00"), "Ksh 4,290"),
        ("2100", "Ksh 2,100"),
        (Decimal("150.50"), "Ksh 150.50"),
        (Decimal("1234567.5"), "Ksh 1,234,567.50"),
        (0, "Ksh 0"),
        (Decimal("999.999"), "Ksh 1,000"),     # rounded only when displayed
        (Decimal("0.005"), "Ksh 0.01"),
    ],
)
def test_ksh(value, expected):
    assert ksh(value) == expected


def test_ksh_never_writes_kes():
    assert "KES" not in ksh(100)


@pytest.mark.parametrize(
    "value,expected",
    [(Decimal("1950.00"), "1,950"), (Decimal("13082.625"), "13,082.63"), (130, "130"), (Decimal("0.00"), "0")],
)
def test_money_figure_is_ksh_without_the_word(value, expected):
    assert money_figure(value) == expected


@pytest.mark.parametrize(
    "value,expected",
    [(Decimal("120.00"), "120"), (Decimal("120.50"), "120.5"), (Decimal("100.00"), "100"),
     (Decimal("12345.75"), "12345.75"), (Decimal("0.00"), "0")],
)
def test_plain_number_drops_trailing_zeros_and_has_no_comma(value, expected):
    # SMS-13: readings and consumption are written as stored, without trailing zeros
    assert plain_number(value) == expected


@pytest.mark.parametrize(
    "stored,expected",
    [
        ("254722123456", "0722 XXX 456"),
        ("254112345678", "0112 XXX 678"),
    ],
)
def test_mask_phone(stored, expected):
    assert mask_phone(stored) == expected


@pytest.mark.parametrize("bad", ["", "0722123456", "25472212345", "2547221234567", "abc"])
def test_mask_phone_hides_everything_it_cannot_read(bad):
    assert mask_phone(bad) == "XXXX XXX XXX"


@pytest.mark.parametrize(
    "period,expected",
    [("2026-08", "August 2026"), ("2026-01", "January 2026"), ("2025-12", "December 2025")],
)
def test_period_label(period, expected):
    assert period_label(period) == expected


def test_date_label():
    assert date_label(date(2026, 10, 4)) == "4 Oct 2026"


@pytest.mark.parametrize("moment,expected", [
    (datetime(2026, 8, 29, 9, 14), "29 Aug 2026, 9:14am"),
    (datetime(2026, 8, 29, 0, 5), "29 Aug 2026, 12:05am"),
    (datetime(2026, 8, 29, 12, 0), "29 Aug 2026, 12:00pm"),
    (datetime(2026, 8, 29, 18, 30), "29 Aug 2026, 6:30pm"),
])
def test_date_time_label(moment, expected):
    assert date_time_label(moment) == expected


def test_the_filters_are_available_in_templates():
    environment = create_templates().env
    template = environment.from_string(
        "{{ 4290|ksh }} {{ '254722123456'|mask_phone }} {{ '2026-08'|period_label }} {{ '120.50'|plain_number }}"
    )

    assert template.render() == "Ksh 4,290 0722 XXX 456 August 2026 120.5"


def test_autoescape_is_on():
    environment = create_templates().env

    assert environment.from_string("{{ name }}").render(name="<script>alert(1)</script>") == (
        "&lt;script&gt;alert(1)&lt;/script&gt;"
    )


def test_templates_are_searched_in_shared_and_features_folders():
    search_paths = [path.replace("\\", "/") for path in create_templates().env.loader.searchpath]

    assert search_paths[0].endswith("app/shared/templates")
    assert search_paths[1].endswith("app/features")

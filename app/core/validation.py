"""Cleaning and checking what people type (SECURITY.md section 7). Shared by every service,
so each rule is written once. These are small pure helpers: they raise a BusinessRuleError
with a plain message, beside the field it belongs to.
"""

import re
from decimal import Decimal, InvalidOperation

from app.core.errors import BusinessRuleError

# IN-4: control characters and line breaks never belong in a name, a phone number or an
# email. They would also break the layout of an SMS.
CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]")
# IN-3: a Kenyan mobile number, 07... or 01... once the country code is taken off
KENYAN_MOBILE = re.compile(r"^0[17]\d{8}$")
# Something@something.something, with no spaces. Good enough to catch typing mistakes.
EMAIL_FORMAT = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

NAME_MAX_LENGTH = 50
EMAIL_MAX_LENGTH = 120
# IN-2: the money columns are DECIMAL(10, 2), so this is the most they can hold
MONEY_MAX = Decimal("99999999.99")


def clean_text(value: str) -> str:
    """Trim the ends and remove control characters and line breaks (IN-4)."""
    return CONTROL_CHARACTERS.sub("", value).strip()


def clean_name(value: str, field: str, label: str) -> str:
    """A first or last name: required, at most 50 characters. `label` is how the person sees it."""
    name = clean_text(value)
    if not name:
        raise BusinessRuleError(f"Please enter the {label}.", field=field)
    if len(name) > NAME_MAX_LENGTH:
        raise BusinessRuleError(f"The {label} is too long. Use {NAME_MAX_LENGTH} characters or fewer.", field=field)
    return name


def normalise_phone(value: str, field: str = "phone") -> str:
    """0722 123 456, 0722-123-456, 254722123456 and +254722123456 all become 254722123456 (IN-3)."""
    digits = re.sub(r"[\s\-()]", "", clean_text(value))
    if digits.startswith("+254"):
        digits = "0" + digits[4:]
    elif digits.startswith("254"):
        digits = "0" + digits[3:]
    if not KENYAN_MOBILE.match(digits):
        raise BusinessRuleError(
            "Please enter a Kenyan mobile number, for example 0722 123 456.", field=field
        )
    return "254" + digits[1:]


def normalise_email(value: str, field: str = "email") -> str:
    """Lower case, no spaces, and in the shape of an email address."""
    email = clean_text(value).lower()
    if len(email) > EMAIL_MAX_LENGTH or not EMAIL_FORMAT.match(email):
        raise BusinessRuleError("Please enter an email address, for example you@example.com.", field=field)
    return email


def parse_money(value: str, field: str, label: str, example: str) -> Decimal:
    """An amount in Ksh, such as "130", "1,300" or "130.50": zero or more, with at most two
    decimal places (IN-2). Always a Decimal, never a float. `example` goes in the messages."""
    typed = clean_text(value).replace(",", "").replace(" ", "")
    if not typed:
        raise BusinessRuleError(f"Please enter the {label}, for example {example}.", field=field)
    try:
        amount = Decimal(typed)
    except InvalidOperation as exc:
        raise BusinessRuleError(f"Please enter the {label} as a number, for example {example}.", field=field) from exc
    # Decimal also reads "NaN" and "Infinity" as numbers
    if not amount.is_finite():
        raise BusinessRuleError(f"Please enter the {label} as a number, for example {example}.", field=field)
    if amount < 0:
        raise BusinessRuleError(f"The {label} cannot be less than 0.", field=field)
    if amount.as_tuple().exponent < -2:
        raise BusinessRuleError(f"Use at most 2 decimal places for the {label}.", field=field)
    if amount > MONEY_MAX:
        raise BusinessRuleError(f"The {label} is too large. Please check the number.", field=field)
    return amount


def parse_phone_or_email(value: str, field: str = "identifier") -> tuple[str | None, str | None]:
    """The "Phone number or email" box (AUTH-1). Returns (phone, email); exactly one is set."""
    typed = clean_text(value)
    if not typed:
        raise BusinessRuleError("Please enter a phone number or an email address.", field=field)
    if "@" in typed:
        return None, normalise_email(typed, field)
    try:
        return normalise_phone(typed, field), None
    except BusinessRuleError as exc:
        raise BusinessRuleError(
            "Please enter a Kenyan mobile number (for example 0722 123 456) or an email address.",
            field=field,
        ) from exc

"""The Jinja environment and the template filters (ksh, mask_phone, period_label).

One environment finds every template by a predictable name: base.html and
partials/... come from app/shared/templates, and bills/views/detail.html and the
like come from app/features. The filters are tiny pure helpers, so they have no
try/except (AGENTS.md section 6).
"""

import re
import secrets
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from functools import lru_cache
from pathlib import Path

from fastapi import Request
from fastapi.templating import Jinja2Templates
from jinja2 import Environment, FileSystemLoader
from markupsafe import Markup, escape


APP_FOLDER = Path(__file__).resolve().parents[1]
ICONS_FOLDER = APP_FOLDER / "shared" / "static" / "icons"

# Where the CSRF token lives in the session, and the name of the hidden form field.
# The check itself is verify_csrf() in security.py (SECURITY.md section 4).
CSRF_SESSION_KEY = "csrf_token"
CSRF_FORM_FIELD = "csrf_token"

MONTH_NAMES = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)


def money_figure(value: Decimal | int | str) -> str:
    """4290 becomes "4,290" and 1950.5 becomes "1,950.50". Cents only when there are some (SMS-13)."""
    # Money is rounded only here, when it is displayed
    amount = Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if amount == amount.to_integral_value():
        return f"{amount:,.0f}"
    return f"{amount:,.2f}"


def ksh(value: Decimal | int | str) -> str:
    """4290 becomes "Ksh 4,290"."""
    return f"Ksh {money_figure(value)}"


def plain_number(value: Decimal | int | str) -> str:
    """A reading or units as stored, without trailing zeros: 120.00 becomes "120" and 120.50
    becomes "120.5" (SMS-13). No thousands comma, so it reads like the meter."""
    number = Decimal(str(value)).normalize()
    # "f" writes 1E+2 as 100
    return f"{number:f}"


def mask_phone(stored_phone: str) -> str:
    """254722123456 becomes "0722 XXX 456" (OUT-3)."""
    is_stored_format = len(stored_phone) == 12 and stored_phone.isdigit() and stored_phone.startswith("254")
    if not is_stored_format:
        return "XXXX XXX XXX"
    local_number = "0" + stored_phone[3:]
    return f"{local_number[:4]} XXX {local_number[7:]}"


def role_label(role: str) -> str:
    """member becomes "Member", owner becomes "Owner"."""
    return role.capitalize()


def is_htmx_request(request: Request) -> bool:
    """htmx marks every request it sends with this header. A controller uses it to choose
    between a whole page and a small fragment (architecture.md section 5)."""
    return request.headers.get("HX-Request") == "true"


def period_label(billing_period: str) -> str:
    """2026-08 becomes "August 2026"."""
    year, month = billing_period.split("-")
    return f"{MONTH_NAMES[int(month) - 1]} {year}"


def date_label(day: date) -> str:
    """2026-10-04 becomes "4 Oct 2026"."""
    return f"{day.day} {day:%b %Y}"


def date_time_label(moment: datetime) -> str:
    """A time saved in EAT, such as an SMS attempt: "29 Aug 2026, 9:14am"."""
    hour = moment.hour % 12 or 12
    return f"{date_label(moment)}, {hour}:{moment:%M}{'am' if moment.hour < 12 else 'pm'}"


@lru_cache
def icon_markup(name: str) -> Markup:
    """The drawing inside an icon file, without its outer <svg> tag.

    The macro in macros/icons.html wraps it. The files in static/icons/ stay the
    single copy of each icon. They are vendored files, not user input, so the
    result is safe to mark as trusted markup.
    """
    if not re.fullmatch(r"[a-z0-9-]+", name):
        raise ValueError(f"{name} is not a valid icon name.")
    try:
        svg_text = (ICONS_FOLDER / f"{name}.svg").read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ValueError(f"There is no icon called {name}.") from exc
    return Markup(svg_text[svg_text.index(">") + 1 : svg_text.rindex("</svg>")].strip())


def get_csrf_token(request: Request) -> str:
    """The person's token, made the first time it is needed and kept in their session."""
    if CSRF_SESSION_KEY not in request.session:
        request.session[CSRF_SESSION_KEY] = secrets.token_urlsafe(32)
    return request.session[CSRF_SESSION_KEY]


def csrf_field(request: Request) -> Markup:
    """The hidden field every form carries. Used in templates as {{ csrf_field(request) }}."""
    token = escape(get_csrf_token(request))
    return Markup(f'<input type="hidden" name="{CSRF_FORM_FIELD}" value="{token}">')


def create_templates() -> Jinja2Templates:
    environment = Environment(
        loader=FileSystemLoader([APP_FOLDER / "shared" / "templates", APP_FOLDER / "features"]),
        # OUT-1: autoescape stays on. Never use |safe on anything a user typed.
        autoescape=True,
        # a line holding only a {% tag %} leaves no blank line in the HTML
        trim_blocks=True,
        lstrip_blocks=True,
    )
    environment.filters["ksh"] = ksh
    environment.filters["plain_number"] = plain_number
    environment.filters["date_label"] = date_label
    environment.filters["date_time_label"] = date_time_label
    environment.filters["mask_phone"] = mask_phone
    environment.filters["period_label"] = period_label
    environment.filters["role_label"] = role_label
    environment.globals["icon_markup"] = icon_markup
    environment.globals["csrf_field"] = csrf_field
    environment.globals["csrf_token"] = get_csrf_token
    return Jinja2Templates(env=environment)


templates = create_templates()

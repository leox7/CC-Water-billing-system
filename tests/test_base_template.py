import re
from types import SimpleNamespace

import pytest

from app.core.templating import create_templates

PAGE = """{% extends "base.html" %}
{% block title %}Customers{% endblock %}
{% block head_css %}<link rel="stylesheet" href="/static/customers/customers.css">{% endblock %}
{% block content %}<h1>Customers page</h1>{% endblock %}"""


def render(source=PAGE, path="/", role="owner", first_name="Daniel", **extra):
    request = SimpleNamespace(url=SimpleNamespace(path=path), session={})
    context = {"request": request, **extra}
    if role is not None:
        context["user"] = SimpleNamespace(first_name=first_name, role=role)
    return create_templates().env.from_string(source).render(**context)


def links_in(html, nav_class):
    nav = re.search(rf'<nav class="{nav_class}".*?</nav>', html, re.S).group(0)
    return re.findall(r'href="([^"]+)"', nav)


def test_the_owner_has_five_main_links_and_an_account_link():
    html = render()

    assert links_in(html, "nav-list") == ["/", "/customers", "/readings", "/bills", "/reports", "/account"]
    assert links_in(html, "bottom-nav") == ["/", "/customers", "/readings", "/bills", "/reports"]


def test_a_member_has_the_same_five_main_links_as_the_owner():
    # PRD section 5: one menu for both roles
    html = render(role="member", first_name="Peter")

    assert links_in(html, "nav-list") == ["/", "/customers", "/readings", "/bills", "/reports", "/account"]
    assert links_in(html, "bottom-nav") == ["/", "/customers", "/readings", "/bills", "/reports"]


def test_the_sidebar_footer_shows_first_name_and_role():
    assert "Daniel (Owner)" in render()
    assert "Peter (Member)" in render(role="member", first_name="Peter")


def test_the_current_page_is_marked_and_only_that_one():
    html = render(path="/customers")

    assert html.count('aria-current="page"') == 2          # sidebar and bottom bar
    for nav_class in ("nav-list", "bottom-nav"):
        nav = re.search(rf'<nav class="{nav_class}".*?</nav>', html, re.S).group(0)
        current = re.search(r'<a [^>]*aria-current="page"[^>]*>', nav).group(0)
        assert 'href="/customers"' in current


def test_the_dashboard_link_is_current_only_on_the_home_page():
    assert 'href="/" aria-current="page"' in render(path="/")
    assert 'href="/" aria-current="page"' not in render(path="/customers")


def test_a_page_under_a_section_keeps_that_section_current():
    html = render(path="/bills/57")

    assert 'href="/bills" aria-current="page"' in html


def test_the_page_basics_are_in_place():
    html = render()

    assert '<html lang="en">' in html
    assert '<meta name="viewport" content="width=device-width, initial-scale=1">' in html
    assert '<title>Customers | C&amp;C H2O</title>' in html
    assert '<main id="main"' in html
    assert html.index('class="skip-link"') < html.index("<aside")
    assert 'href="#main"' in html


def test_stylesheets_load_in_the_fixed_order_with_the_feature_css_last():
    html = render()

    positions = [html.index(name) for name in (
        "css/tokens.css", "css/base.css", "css/components.css", "/static/customers/customers.css")]
    assert positions == sorted(positions)


def test_scripts_are_local_files():
    html = render()

    assert '<script src="/static/shared/js/htmx.min.js" defer></script>' in html
    assert '<script src="/static/shared/js/app.js" defer></script>' in html
    assert "http://" not in html and "https://" not in html


def test_a_page_without_a_user_has_no_shell():
    html = render(role=None)

    assert "<aside" not in html and "bottom-nav" not in html and "topbar" not in html
    assert html.count("Customers page") == 1
    assert '<main id="main">' in html


def test_the_content_is_rendered_once_with_the_shell():
    assert render().count("Customers page") == 1


def test_a_first_name_is_escaped():
    html = render(first_name="<script>alert(1)</script>")

    assert "<script>alert(1)</script>" not in html


@pytest.mark.parametrize("role", ["owner", "member", None])
def test_no_inline_styles_scripts_or_handlers(role):
    # The Content Security Policy blocks all three (AGENTS.md section 4)
    html = render(role=role)

    assert " style=" not in html
    assert "<style" not in html
    assert not re.search(r"<script(?![^>]*\bsrc=)", html)
    assert not re.search(r"\son[a-z]+\s*=", html)


# ---- the shared partials ----

@pytest.mark.parametrize(
    "status,word,icon_part",
    [
        ("paid", "Paid", "M9 12l2 2l4 -4"),            # circle-check
        ("partial", "Partial", None),
        ("unpaid", "Unpaid", None),
        ("active", "Active", None),
        ("inactive", "Inactive", None),
    ],
)
def test_a_status_badge_has_a_word_and_an_icon(status, word, icon_part):
    html = render('{% include "partials/status_badge.html" %}', status=status, role=None)

    assert f"badge-{status}" in html
    assert word in html
    assert "<svg" in html and 'aria-hidden="true"' in html
    assert "role=" not in html
    if icon_part:
        assert icon_part in html


def test_a_status_badge_swapped_in_by_htmx_is_announced():
    html = render('{% include "partials/status_badge.html" %}', status="partial", announce=True, role=None)

    assert 'role="status"' in html


def test_a_stat_card_shows_label_figure_and_note():
    html = render('{% include "partials/stat_card.html" %}', label="Active customers",
                  figure="16", note="Across 3 locations", role=None)

    assert "Active customers" in html and "16" in html and "Across 3 locations" in html
    assert 'class="stat-figure"' in html


def test_form_errors_with_one_error_link_to_its_field():
    error = SimpleNamespace(field="current_reading", message="This is lower than last month's reading (200).")
    html = render('{% include "partials/form_errors.html" %}', errors=[error], role=None)

    assert 'role="alert"' in html
    assert 'href="#field-current_reading"' in html
    assert "<ul" not in html


def test_form_errors_with_several_errors_make_a_list():
    errors = [SimpleNamespace(field="first_name", message="Please enter a first name."),
              SimpleNamespace(field=None, message="Something else is wrong.")]
    html = render('{% include "partials/form_errors.html" %}', errors=errors, role=None)

    assert html.count("<li>") == 2
    assert 'href="#field-first_name"' in html
    assert "Something else is wrong." in html


def test_form_errors_show_nothing_when_there_are_none():
    assert render('{% include "partials/form_errors.html" %}', errors=[], role=None).strip() == ""

"""A form that changes data cannot be sent twice by a double click or a second Enter
(SECURITY.md MON-2, DESIGN_SYSTEM.md section 9, Loading).

- A form sent by htmx (hx-post) disables its submit button while it works
  (hx-disabled-elt) and drops a second request until the first finishes (hx-sync="this:drop").
- Any other form that changes data is a plain post, and app.js guards it, with no inline script.

The tests read every template, so a new form that forgets the protection fails here.
"""

import re
from pathlib import Path

APP_FOLDER = Path(__file__).resolve().parents[1] / "app"
TEMPLATES = sorted(APP_FOLDER.rglob("*.html"))
FORM_TAG = re.compile(r"<form\b[^>]*>", re.S)


def form_tags():
    for template in TEMPLATES:
        for tag in FORM_TAG.findall(template.read_text(encoding="utf-8")):
            yield template.relative_to(APP_FOLDER).as_posix(), tag


def test_there_are_forms_to_check():
    assert sum(1 for _ in form_tags()) >= 20
    assert sum(1 for _, tag in form_tags() if "hx-post=" in tag) == 13


def test_every_htmx_form_that_changes_data_has_both_guards():
    unprotected = []
    for name, tag in form_tags():
        if "hx-post=" not in tag:
            continue
        if 'hx-disabled-elt="find button[type=submit]"' not in tag or 'hx-sync="this:drop"' not in tag:
            unprotected.append(name)

    assert unprotected == []


def test_no_form_changes_data_with_get_or_another_htmx_verb():
    # CSRF-1: data changes use POST only
    for name, tag in form_tags():
        for verb in ("hx-put", "hx-patch", "hx-delete"):
            assert verb not in tag, name
        if 'method="post"' not in tag:
            # a form that is not a post only reads (the searches and filters)
            assert "hx-post" not in tag and "csrf" not in tag, name


def test_each_htmx_form_also_works_as_a_plain_post_that_is_guarded_by_app_js():
    # without htmx the same form posts normally, so it has a method and an action too
    for name, tag in form_tags():
        if "hx-post=" in tag:
            assert 'method="post"' in tag and "action=" in tag, name


def test_plain_forms_are_guarded_from_app_js_without_an_inline_script():
    app_js = (APP_FOLDER / "shared" / "static" / "js" / "app.js").read_text(encoding="utf-8")

    # a second submit is stopped, and the button shows its working state
    assert 'addEventListener("submit"' in app_js
    assert 'form.dataset.submitting === "true"' in app_js and "event.preventDefault()" in app_js
    assert 'classList.add("is-loading")' in app_js
    # the guard leaves htmx forms to the attributes above
    assert 'form.hasAttribute("hx-post")' in app_js


def test_no_template_has_an_inline_script_or_event_handler():
    # the CSP blocks them, so a double-submit guard cannot depend on one
    for template in TEMPLATES:
        text = template.read_text(encoding="utf-8")
        assert "<script" not in text.lower() or 'src="' in text, template
        assert not re.search(r"\son(click|submit|change|input)=", text), template


def test_the_button_looks_busy_not_unavailable_while_htmx_disables_it():
    css = (APP_FOLDER / "shared" / "static" / "css" / "components.css").read_text(encoding="utf-8")

    assert ".button:disabled:not(.htmx-request .button)" in css

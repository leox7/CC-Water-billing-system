import pytest

from app.core.templating import ICONS_FOLDER, create_templates, icon_markup

# DESIGN_SYSTEM.md 7.8
ICONS_IN_THE_DESIGN_SYSTEM = [
    "home", "users", "gauge", "receipt", "chart-bar", "user-circle", "plus", "check", "eye",
    "pencil", "trash", "send", "arrow-left", "search", "logout", "circle-check", "circle-half",
    "alert-circle", "info-circle",
]


def render(source, **context):
    return create_templates().env.from_string(source).render(**context)


def test_every_icon_in_the_design_system_exists():
    for name in ICONS_IN_THE_DESIGN_SYSTEM:
        assert (ICONS_FOLDER / f"{name}.svg").exists(), name


def test_icon_markup_is_only_the_inside_of_the_svg():
    markup = icon_markup("home")

    assert "<path" in markup
    assert "<svg" not in markup
    assert "</svg>" not in markup


def test_an_icon_next_to_text_is_hidden_from_screen_readers():
    html = render('{% from "macros/icons.html" import icon %}{{ icon("plus") }}')

    assert 'aria-hidden="true"' in html
    assert "aria-label" not in html
    assert 'width="20"' in html and 'stroke-width="1.75"' in html
    assert 'stroke="currentColor"' in html


def test_an_icon_on_its_own_has_an_accessible_name():
    html = render('{% from "macros/icons.html" import icon %}{{ icon("pencil", 24, label="Edit reading") }}')

    assert 'aria-label="Edit reading"' in html
    assert 'role="img"' in html
    assert "aria-hidden" not in html
    assert 'width="24"' in html


def test_a_label_is_escaped():
    html = render('{% from "macros/icons.html" import icon %}{{ icon("plus", label=name) }}', name='"><script>')

    assert "<script>" not in html


@pytest.mark.parametrize("bad_name", ["nope", "../../../etc/passwd", "Home", ""])
def test_an_unknown_or_unsafe_icon_name_is_refused(bad_name):
    with pytest.raises(ValueError):
        icon_markup(bad_name)


def test_rendered_icons_contain_no_web_address():
    html = render('{% from "macros/icons.html" import icon %}{{ icon("home") }}')

    assert "http" not in html

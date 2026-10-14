"""The vendored files are served from this app, never from a CDN (AGENTS.md section 4)."""

import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
STATIC = PROJECT_ROOT / "app" / "shared" / "static"
WEB_ADDRESS = re.compile(r"https?://(?!www\.w3\.org)", re.I)


def test_htmx_is_vendored_and_pinned():
    htmx = (STATIC / "js" / "htmx.min.js").read_text(encoding="utf-8")

    assert 'version:"2.0.11"' in htmx
    assert (STATIC / "js" / "htmx-LICENSE.txt").exists()


def test_the_four_font_files_exist_and_are_woff2():
    for name in ("inter-latin-400-normal", "inter-latin-600-normal",
                 "inter-latin-700-normal", "jetbrains-mono-latin-600-normal"):
        assert (STATIC / "fonts" / f"{name}.woff2").read_bytes()[:4] == b"wOF2"


def test_font_and_icon_licences_travel_with_the_files():
    assert (STATIC / "fonts" / "Inter-LICENSE.txt").exists()
    assert (STATIC / "fonts" / "JetBrainsMono-LICENSE.txt").exists()
    assert (STATIC / "icons" / "Tabler-LICENSE.txt").exists()


def test_app_js_lets_htmx_swap_error_responses():
    app_js = (STATIC / "js" / "app.js").read_text(encoding="utf-8")

    assert "htmx:beforeSwap" in app_js
    assert "status < 400" in app_js
    assert "shouldSwap = true" in app_js
    # a form can send its error message to a spot of its choosing
    assert "data-error-target" in app_js


def test_our_own_scripts_and_styles_load_nothing_from_the_web():
    own_files = [STATIC / "js" / "app.js", *STATIC.joinpath("css").glob("*.css")]
    own_files += list((PROJECT_ROOT / "app" / "shared" / "templates").rglob("*.html"))
    own_files += list((PROJECT_ROOT / "app" / "features").rglob("*.html"))
    own_files += list((PROJECT_ROOT / "app" / "features").rglob("*.css"))

    offenders = [f.name for f in own_files if WEB_ADDRESS.search(f.read_text(encoding="utf-8"))]

    assert offenders == []


def test_the_logo_copies_exist_and_the_original_is_kept():
    for name in ("logo-full", "logo-mark", "icon-192", "icon-512", "icon-maskable-512"):
        assert (STATIC / "img" / f"{name}.png").exists(), name
    assert (PROJECT_ROOT / "docs" / "logo" / "C&C LOGO.jpeg").exists()

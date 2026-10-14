import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TOKENS_FILE = PROJECT_ROOT / "app" / "shared" / "static" / "css" / "tokens.css"
HEX_VALUE = re.compile(r"#[0-9a-fA-F]{3,8}\b")


def token_declarations(css_text):
    return dict(re.findall(r"(--[\w-]+)\s*:\s*([^;]+);", css_text))


def design_system_token_block():
    document = (PROJECT_ROOT / "docs" / "DESIGN_SYSTEM.md").read_text(encoding="utf-8")
    after_heading = document[document.index("### The token block"):]
    return re.search(r"```css\n(.*?)```", after_heading, re.S).group(1)


def test_tokens_match_the_design_system_exactly():
    in_file = token_declarations(TOKENS_FILE.read_text(encoding="utf-8"))
    in_document = token_declarations(design_system_token_block())

    assert in_file == in_document
    assert len(in_file) > 40


def test_no_other_css_file_holds_a_hex_value():
    css_files = (PROJECT_ROOT / "app").rglob("*.css")
    offenders = []
    for css_file in css_files:
        if css_file == TOKENS_FILE:
            continue
        if HEX_VALUE.search(css_file.read_text(encoding="utf-8")):
            offenders.append(css_file.name)

    assert offenders == []


def test_tokens_file_is_the_only_one_with_colours():
    assert HEX_VALUE.search(TOKENS_FILE.read_text(encoding="utf-8"))

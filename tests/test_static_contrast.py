"""The page's colour tokens keep WCAG AA contrast (4.5:1) in both themes."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

CSS = (Path(__file__).parent.parent / "app" / "static" / "styles.css").read_text()


def _palette(block: str) -> dict[str, str]:
    return dict(re.findall(r"--([a-z0-9-]+):\s*(#[0-9a-fA-F]{6})", block))


LIGHT = _palette(CSS.split("@media (prefers-color-scheme: dark)")[0])
DARK = {**LIGHT, **_palette(CSS.split("@media (prefers-color-scheme: dark)")[1].split("}\n}")[0])}


def _luminance(hex_colour: str) -> float:
    channels = [int(hex_colour[i : i + 2], 16) / 255 for i in (1, 3, 5)]
    linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _ratio(a: str, b: str) -> float:
    high, low = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (high + 0.05) / (low + 0.05)


# (text token, background token) pairs that appear on the page.
PAIRS = [
    ("ink", "paper"),
    ("ink-2", "paper"),
    ("ink-3", "paper"),
    ("ink-3", "sheet"),
    ("warn", "paper"),
    ("warn", "sheet"),
    ("warn", "warn-soft"),
    ("ok", "sheet"),
    ("bad", "sheet"),
    ("blue", "paper"),
    ("on-blue", "blue"),
    ("on-blue", "blue-deep"),
]


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize(("text", "background"), PAIRS)
def test_text_tokens_meet_wcag_aa(theme: str, text: str, background: str) -> None:
    palette = LIGHT if theme == "light" else DARK
    ratio = _ratio(palette[text], palette[background])
    assert ratio >= 4.5, f"{theme}: --{text} on --{background} is {ratio:.2f}:1"


# ----- API reference (/api/docs): stock Swagger UI recoloured by docs.css -----------------

DOCS_CSS = (Path(__file__).parent.parent / "app" / "static" / "docs.css").read_text()
DOCS = _palette(DOCS_CSS)
WHITE = "#ffffff"
SCHEMA_ROW = "#f2f2f2"  # Swagger UI's rgba(0, 0, 0, 0.05) model rows over white


def test_docs_tokens_mirror_the_app_tokens() -> None:
    assert DOCS, "docs.css defines no tokens"
    for name, value in DOCS.items():
        assert LIGHT[name].lower() == value.lower(), f"--{name} drifted from styles.css"


@pytest.mark.parametrize(
    ("text", "background"),
    [
        *[(WHITE, DOCS[fill]) for fill in ("blue", "ok", "warn", "bad", "ink-2")],  # badges
        (DOCS["blue-deep"], WHITE),  # schema link
        (DOCS["ink-3"], SCHEMA_ROW),  # "Expand all"
    ],
)
def test_docs_labels_meet_wcag_aa(text: str, background: str) -> None:
    ratio = _ratio(text, background)
    assert ratio >= 4.5, f"{text} on {background} is {ratio:.2f}:1"


def test_docs_keyboard_focus_stays_visible() -> None:
    docs_js = (Path(__file__).parent.parent / "app" / "static" / "docs.js").read_text()
    # Deep links nest an <a href> inside each operation's toggle button (axe nested-interactive).
    assert "deepLinking: false" in docs_js
    assert ".swagger-ui :focus-visible" in DOCS_CSS  # Swagger strips outlines from some buttons
    # "Copy path" is zero-width until hover; it must also open for keyboard focus, at once.
    assert ".opblock-summary:focus-within .view-line-link" in DOCS_CSS
    assert "transition: none" in DOCS_CSS


# ----- theme switch and animated backdrop -------------------------------------------------

STATIC = Path(__file__).parent.parent / "app" / "static"


def _block(selector: str) -> str:
    start = CSS.index(selector + " {")
    return CSS[start : CSS.index("\n}", start)]


def test_forced_dark_theme_matches_the_system_dark_theme() -> None:
    # The switch sets data-theme="dark" on a light system; it must look exactly like dark mode.
    forced = _palette(_block(':root[data-theme="dark"]'))
    assert forced == _palette(CSS.split("@media (prefers-color-scheme: dark)")[1].split("}\n}")[0])
    assert "color-scheme: dark" in _block(':root[data-theme="dark"]')


def _keyframes(name: str) -> str:
    start = CSS.index("{", CSS.index(f"@keyframes {name} "))
    depth = 0
    for end in range(start, len(CSS)):
        depth += {"{": 1, "}": -1}.get(CSS[end], 0)
        if depth == 0:
            return CSS[start + 1 : end]
    raise AssertionError(f"@keyframes {name} is not closed")


def test_loader_and_stage_motion_animate_only_compositor_properties() -> None:
    # transform and opacity skip layout and paint; anything else would repaint every frame.
    for name in ("read", "stage-rise", "badge-in"):
        props = set(re.findall(r"([a-z-]+)\s*:", _keyframes(name)))
        assert props <= {"transform", "opacity"}, f"@keyframes {name} animates {props}"


def test_no_yellow_highlighter_in_the_interface() -> None:
    # Selection and progress are blue; yellow stays only in the logo (.mark-highlight, splash).
    uses = [line.strip() for line in CSS.splitlines() if "var(--mark" in line]
    assert all(line.startswith((".mark-highlight", ".splash-highlight")) for line in uses), uses


@pytest.mark.parametrize("page", ["index.html", "404.html", "docs.html"])
def test_every_page_applies_the_stored_theme_before_its_styles(page: str) -> None:
    html = (STATIC / page).read_text()
    head = html[: html.index("</head>")]
    assert '<script src="/static/theme.js"></script>' in head
    styles = head.find('href="/static/styles.css"')
    assert styles == -1 or head.index("theme.js") < styles, "theme.js must run before styles"
    assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", html), "inline script found"


@pytest.mark.parametrize("page", ["index.html", "404.html"])
def test_theme_switch_is_a_labelled_button(page: str) -> None:
    html = (STATIC / page).read_text()
    assert re.search(
        r'<button class="theme-toggle" type="button" data-theme-toggle aria-label="', html
    )

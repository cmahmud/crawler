from __future__ import annotations

from dataclasses import dataclass

from selectolax.lexbor import LexborHTMLParser

_APP_ROOT_SELECTORS = ("#root", "#app", "#__next", "[data-reactroot]", "[data-v-app]")
_CONTENT_SELECTORS = "main, article, h1, h2, h3, p, li, button, label, td, th"
_JS_REQUIRED_PHRASES = (
    "enable javascript",
    "javascript is required",
    "javascript required",
    "requires javascript",
    "please turn on javascript",
)


@dataclass(frozen=True, slots=True)
class RenderingAssessment:
    requires_browser: bool
    score: int
    visible_text_length: int
    script_count: int
    reasons: tuple[str, ...]


def assess_rendering(html: bytes | str) -> RenderingAssessment:
    """Estimate whether a successful HTML response is only a client-rendered shell.

    This is intentionally conservative. A framework marker by itself is not enough:
    SSR pages should remain on the cheaper HTTP path when meaningful content exists.
    """

    parser = LexborHTMLParser(html)
    text_parts: list[str] = []
    for node in parser.css(_CONTENT_SELECTORS):
        text = node.text(separator=" ", strip=True)
        if text:
            text_parts.append(text)
    visible_text = " ".join(text_parts)
    visible_text_length = len(visible_text)

    scripts = parser.css("script")
    script_count = len(scripts)
    app_root = any(parser.css_first(selector) is not None for selector in _APP_ROOT_SELECTORS)

    noscript_text = " ".join(
        node.text(separator=" ", strip=True).lower() for node in parser.css("noscript")
    )
    js_required = any(phrase in noscript_text for phrase in _JS_REQUIRED_PHRASES)

    reasons: list[str] = []
    score = 0

    if visible_text_length < 80:
        score += 2
        reasons.append("very-little-visible-content")
    elif visible_text_length < 200:
        score += 1
        reasons.append("little-visible-content")

    if app_root:
        score += 2
        reasons.append("client-app-root")

    if js_required:
        score += 3
        reasons.append("javascript-required-message")

    if script_count >= 3:
        score += 1
        reasons.append("script-heavy-shell")

    if visible_text_length >= 300:
        score -= 3
        reasons.append("meaningful-server-rendered-content")

    requires_browser = score >= 4 and visible_text_length < 300
    return RenderingAssessment(
        requires_browser=requires_browser,
        score=score,
        visible_text_length=visible_text_length,
        script_count=script_count,
        reasons=tuple(reasons),
    )

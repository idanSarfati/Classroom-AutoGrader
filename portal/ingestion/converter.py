"""Convert exported Google Docs HTML into safe, offline portal HTML.

Google's export is rich but hostile to a student phone: hundreds of inline
styles, wrapper ``<span>`` elements, absolute ``googleusercontent`` image URLs
and scripts if the document ever contains one. This module keeps the useful
structure (headings, lists, tables, bold/italic, code) and removes everything
else.

Images are handled by a callback supplied by the importer. The callback may
download the bytes through the authenticated Google client and return a local
portal URL; when it returns ``None`` the image is replaced by a text
placeholder. An external URL never survives into stored HTML.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import nh3

from portal.ingestion.normalize import html_to_text

_MAX_HTML_CHARS = 400_000

_SCRIPT_RE = re.compile(r"<(script|style)\b[^>]*>.*?</\1\s*>", re.I | re.S)
_HEAD_RE = re.compile(r"<head\b[^>]*>.*?</head\s*>", re.I | re.S)
_COMMENT_RE = re.compile(r"<!--.*?-->", re.S)
_BODY_RE = re.compile(r"<body\b[^>]*>(.*?)</body\s*>", re.I | re.S)
_IMG_RE = re.compile(r"<img\b[^>]*>", re.I | re.S)
_ATTR_RE = re.compile(r'([a-zA-Z_:][-a-zA-Z0-9_:.]*)\s*=\s*"([^"]*)"')
_SPAN_RE = re.compile(r"<span\b([^>]*)>(.*?)</span\s*>", re.I | re.S)
_BOLD_RE = re.compile(r"<b\b[^>]*>(.*?)</b\s*>", re.I | re.S)
_ITALIC_RE = re.compile(r"<i\b[^>]*>(.*?)</i\s*>", re.I | re.S)
_STYLE_ATTR_RE = re.compile(r'style\s*=\s*"([^"]*)"', re.I)

_ALLOWED_TAGS = {
    "p",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "ul",
    "ol",
    "li",
    "table",
    "thead",
    "tbody",
    "tfoot",
    "tr",
    "td",
    "th",
    "strong",
    "em",
    "code",
    "pre",
    "blockquote",
    "br",
    "hr",
    "img",
}
_ALLOWED_ATTRIBUTES = {
    "td": {"colspan", "rowspan"},
    "th": {"colspan", "rowspan"},
    "img": {"src", "alt"},
}


@dataclass
class ConvertedDoc:
    html: str
    text: str
    images_kept: int = 0
    images_missing: int = 0
    warnings: list[str] = field(default_factory=list)


def _attributes(tag: str) -> dict[str, str]:
    return {
        key.lower(): value
        for key, value in _ATTR_RE.findall(tag or "")
    }


def _replace_images(raw: str, resolver, result: ConvertedDoc) -> str:
    """Rewrite or drop every image, never leaving an external ``src``."""
    def replace(match: re.Match[str]) -> str:
        attributes = _attributes(match.group(0))
        source = (attributes.get("src") or "").strip()
        alt = (attributes.get("alt") or "").strip()
        if not source:
            return ""
        local_url: str | None = None
        if resolver is not None:
            try:
                local_url = resolver(source, alt)
            except Exception as exc:  # noqa: BLE001 - one bad image must not abort
                result.warnings.append(f"לא ניתן היה להוריד תמונה: {exc}")
        if local_url:
            result.images_kept += 1
            alt_attr = alt.replace('"', "&quot;")
            return f'<img src="{local_url}" alt="{alt_attr}">'
        result.images_missing += 1
        label = alt or "תמונה"
        return f"<p><em>[{label} - התמונה לא זמינה במצב לא מקוון]</em></p>"

    return _IMG_RE.sub(replace, raw)


def _convert_spans(raw: str) -> str:
    """Turn styled spans into semantic strong/em and drop the wrapper."""
    def replace(match: re.Match[str]) -> str:
        style_match = _STYLE_ATTR_RE.search(match.group(1) or "")
        content = match.group(2)
        if not style_match:
            return content
        style = style_match.group(1).lower()
        weight = re.search(r"font-weight\s*:\s*([^;]+)", style)
        italic = re.search(r"font-style\s*:\s*([^;]+)", style)
        if weight:
            value = weight.group(1).strip()
            bold = "bold" in value or (
                value.isdigit() and int(value) >= 600
            )
            if bold:
                content = f"<strong>{content}</strong>"
        if italic and "italic" in italic.group(1):
            content = f"<em>{content}</em>"
        return content

    # Nested spans are common in Docs exports; three passes cover them.
    for _ in range(3):
        converted = _SPAN_RE.sub(replace, raw)
        if converted == raw:
            break
        raw = converted
    return raw


def convert_doc_html(
    raw_html: str,
    *,
    resolve_image=None,
    max_chars: int = _MAX_HTML_CHARS,
) -> ConvertedDoc:
    """Return sanitized body HTML plus a plain-text fallback.

    ``resolve_image(src, alt) -> local_url | None`` is optional. When omitted,
    every image becomes a placeholder, which is the safe default.
    """
    result = ConvertedDoc(html="", text="")
    raw = (raw_html or "")[:_MAX_HTML_CHARS]
    if not raw.strip():
        return result

    raw = _COMMENT_RE.sub("", raw)
    raw = _HEAD_RE.sub("", raw)
    raw = _SCRIPT_RE.sub("", raw)
    body_match = _BODY_RE.search(raw)
    if body_match:
        raw = body_match.group(1)

    raw = _replace_images(raw, resolve_image, result)
    raw = _convert_spans(raw)
    raw = _BOLD_RE.sub(r"<strong>\1</strong>", raw)
    raw = _ITALIC_RE.sub(r"<em>\1</em>", raw)
    raw = raw[:max_chars]

    cleaned = nh3.clean(
        raw,
        tags=_ALLOWED_TAGS,
        attributes=_ALLOWED_ATTRIBUTES,
        strip_comments=True,
        # No absolute schemes: every retained image URL is already local, and
        # any external link is reduced to its text.
        url_schemes=set(),
    )
    cleaned = cleaned.strip()
    result.html = cleaned
    result.text = html_to_text(cleaned)
    return result


def sanitize_fragment(raw_html: str) -> str:
    """Sanitize already-local HTML (for example a teacher's imported body)."""
    return nh3.clean(
        raw_html or "",
        tags=_ALLOWED_TAGS,
        attributes=_ALLOWED_ATTRIBUTES,
        strip_comments=True,
        url_schemes=set(),
    )

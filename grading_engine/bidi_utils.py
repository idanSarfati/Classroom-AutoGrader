"""Terminal display helpers for bidirectional (Hebrew) text.

Windows PowerShell lacks a Unicode BiDi display engine, so mixed
Hebrew/English strings appear in the wrong visual order. ``bidi_print``
pre-reorders a string with the Unicode Bidirectional Algorithm for
CONSOLE DISPLAY ONLY - never feed its result back into Google API
payloads, CSV files, or logs; always keep the original logical string
for anything that is stored or transmitted.
"""

from __future__ import annotations

from bidi.algorithm import get_display


def bidi_print(text: str) -> str:
    """Return ``text`` reordered for consoles without BiDi support.

    * ``None`` and other non-string inputs are handled safely
      (``None`` -> ``""``, anything else -> ``str(text)``).
    * Empty strings are returned unchanged.
    * The input is never mutated (strings are immutable) - callers keep
      using the original value for APIs, CSV writing, and logging.
    """
    if text is None:
        return ""
    if not isinstance(text, str):
        return str(text)
    if not text:
        return text
    return get_display(text)

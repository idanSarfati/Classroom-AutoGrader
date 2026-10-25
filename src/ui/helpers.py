"""Pure UI helper functions extracted from :mod:`app`.

These are small, side-effect-free (or Streamlit-presentational) helpers used
across the tabs: document URLs, text normalization for fuzzy matching, and the
rubric-config discovery/ranking wrappers that delegate to :mod:`src.config_match`.
"""

from __future__ import annotations

import streamlit as st
from pathlib import Path
from typing import Optional

from src import config_match
from src.main import _fallback_notice
from src.seams import st_proxy

# Project root is three levels up from this file: <root>/src/ui/helpers.py
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def doc_url(doc_id: Optional[str]) -> Optional[str]:
    """Direct Google Docs view URL for a stored Drive/Docs id."""
    if not doc_id or not str(doc_id).strip():
        return None
    return f"https://docs.google.com/document/d/{str(doc_id).strip()}/edit"


def _normalize_text(value: str) -> str:
    """Lowercase, accent-stripped text for fuzzy Hebrew/Latin matching."""
    return config_match.normalize_text(value)


def _text_tokens(value: str) -> set[str]:
    return config_match.text_tokens(value)


def _config_display_path(path: Path) -> str:
    try:
        return path.relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def _read_config_title(path: Path) -> str:
    return config_match.read_config_title(path)


def _score_config_for_assignment(
    config_path: Path, assignment_title: str
) -> tuple[float, str]:
    """Score a rubric file against a Classroom assignment title (0..1 + reason)."""
    return config_match.score_config_for_assignment(config_path, assignment_title)


def suggest_config_for_assignment(
    assignment_title: str, config_files: list[Path]
) -> tuple[Optional[Path], float, str]:
    """Pick the best rubric file for an assignment title, with score + reason."""
    return config_match.suggest_config_for_assignment(assignment_title, config_files)


def get_all_config_files() -> list[Path]:
    """Find rubric JSON files in assignments/, config/ and configs/ directories."""
    return config_match.list_config_files()


def _render_quota_notice() -> None:
    """Warn, in the app, when the batch had to leave Groq for the fallback.

    The router switches provider silently by design - stalling on an exhausted
    daily quota is worse than finishing on another model - but the teacher has
    to know which model graded their students. Same message as the CLI prints.
    """
    notice = _fallback_notice()
    if not notice:
        return
    st.warning(f"⚡ {notice}")


# ---------------------------------------------------------------------------
# Seam wiring
# ---------------------------------------------------------------------------
# Tests monkeypatch ``st`` on the ``app`` facade; resolve it at call time.
st = st_proxy("app", st)

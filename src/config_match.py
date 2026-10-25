"""Headless rubric-matching helpers shared by the UI and the daily check.

``app.py`` owns the Streamlit widgets, but the *logic* for "which rubric JSON
belongs to this Classroom assignment?" is pure and needed in two very different
places:

* the Evaluate tab and the Dashboard dry-run (both in ``app.py``), and
* the daily check's opt-in ``--grade`` mode (``src.daily_check``), which runs
  headless under Task Scheduler and must not import ``app`` (that would drag in
  Streamlit and the whole UI).

So the fuzzy matcher and a ready-made config resolver live here, with no
Streamlit and no network - unit-tested in ``tests/test_config_match.py``.

Resolution order (never guesses blindly): the remembered
``course_work_id -> config`` mapping first, then a fuzzy title match, and only
then does it give up (returning ``None``) so the caller skips the assignment
with a visible reason instead of grading against the wrong rubric.
"""
from __future__ import annotations

import json
import logging
import re
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path
from typing import Optional

from src.main import (
    ASSIGNMENTS_DIR,
    PROJECT_ROOT,
    load_rubric,
    resolve_mapped_config,
)
from src.models import AssignmentConfig, AssignmentSpec, CourseWork

logger = logging.getLogger(__name__)

# Same folders ``app.get_all_config_files`` scans, minus the Streamlit import.
CONFIG_DIR = PROJECT_ROOT / "config"
# The richer spec files (``AssignmentSpec``, sections/questions) live in their
# own folder; the resolver must see them or "תרגול יחידות 1-2" never matches.
SPECS_DIR = PROJECT_ROOT / "configs"
# Files in those folders that are settings/credentials, never rubrics.
_NON_RUBRIC_NAMES = {"assignment_mapping.json", "credentials.json", "token.json"}


def normalize_text(value: str) -> str:
    """Lowercase, accent-stripped text for fuzzy Hebrew/Latin matching."""
    text = unicodedata.normalize("NFKD", value or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return text.casefold()


def text_tokens(value: str) -> set[str]:
    return set(re.findall(r"[\w\u0590-\u05FF]+", normalize_text(value)))


def read_rubric_title(path: Path) -> tuple[str, bool]:
    """Return ``(title, is_spec)`` for a rubric config file.

    Legacy rubrics (``lesson_*.json``) use a top-level ``title``. The newer,
    richer spec files use ``assignment_title`` instead; the two shapes are
    otherwise identical for matching purposes.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "", False
    if not isinstance(data, dict):
        return "", False

    # Spec files carry assignment_title; legacy task rubrics carry title.
    is_spec = "assignment_title" in data
    title = data.get("assignment_title") or data.get("title") or ""
    return str(title), is_spec


def read_config_title(path: Path) -> str:
    """Backward-compatible title-only accessor (``app._read_config_title``)."""
    return read_rubric_title(path)[0]


def is_spec_file(path: Path) -> bool:
    """True when the config file uses the richer spec schema (sections/questions)."""
    title, is_spec = read_rubric_title(path)
    return is_spec or title.startswith("תרגול") or title.startswith("Units")


def score_config_for_assignment(
    config_path: Path, assignment_title: str
) -> tuple[float, str]:
    """Score a rubric file against a Classroom assignment title (0..1 + reason)."""
    assignment_norm = normalize_text(assignment_title)
    stem = config_path.stem  # e.g. lesson_2_worksheet
    stem_norm = normalize_text(stem)
    rubric_title, is_spec = read_rubric_title(config_path)
    rubric_norm = normalize_text(rubric_title)

    # 1. Exact assignment_id token inside the assignment title ("lesson_2", "lesson2").
    compact_stem = stem_norm.replace("_", "").replace("-", "").replace(" ", "")
    compact_title = assignment_norm.replace("_", "").replace("-", "").replace(" ", "")
    if compact_stem and compact_stem in compact_title:
        return 1.0, f"assignment id '{stem}' found in assignment title"

    # 2. Spec files carry an ``assignment_title``; match it directly against the
    #    Classroom coursework title. This must run *before* the number heuristic
    #    below: " units_1_2_spec" would otherwise settle for a shared "1"/"2"
    #    and tie with the lesson rubrics instead of winning outright.
    if is_spec and rubric_title:
        direct = rubric_norm and (
            rubric_norm in assignment_norm or assignment_norm in rubric_norm
        )
        if direct:
            # Exact match: the spec title is identical to the assignment title
            # (after normalization), an unambiguous hit. Substring/contained
            # matches (e.g. a practice sheet whose title contains the
            # assignment title) are weaker signals and must not hijack the spec.
            if rubric_norm == assignment_norm:
                return 1.0, f"exact spec title match: '{rubric_title}'"
            return 0.95, f"spec title match: '{rubric_title}'"
        # Word overlap, digits excluded: "1"/"2" are far too common to prove
        # anything on their own. Every content word of the spec title must
        # appear in the assignment title - a single shared word ("תרגול")
        # also appears in unrelated practice sheets and must not hijack them.
        spec_words = {t for t in text_tokens(rubric_title) if not t.isdigit()}
        title_words = {t for t in text_tokens(assignment_title) if not t.isdigit()}
        if spec_words and spec_words <= title_words:
            return (
                0.9,
                f"spec words match: {', '.join(sorted(spec_words))}",
            )

    # 3. Same explicit task/lesson number ("2" in "lesson_2" vs "שיעור 2").
    #    A bare digit match is a weak signal - record it and keep going so
    #    step 4 (word overlap) can break ties in favour of the more specific
    #    rubric (e.g. lesson_2_worksheet over lesson_2 when the title shares
    #    Hebrew words like "תרגול לפיצוח דף עבודה").
    best_score = 0.0
    best_reason = ""
    stem_number = (re.findall(r"\d+", stem_norm) or [None])[0]
    title_numbers = set(re.findall(r"\d+", assignment_norm))
    if stem_number and stem_number in title_numbers:
        best_score, best_reason = 0.85, f"matching number '{stem_number}' in title"

    # 4. Token overlap between rubric file title and assignment title.
    config_tokens = text_tokens(f"{stem} {rubric_title}")
    assignment_tokens = text_tokens(assignment_title)
    overlap = config_tokens & assignment_tokens
    if overlap and assignment_tokens:
        score = len(overlap) / max(len(assignment_tokens), 1)
        if score >= 0.2:
            score = min(0.95, score)  # allow word overlap to beat a bare digit match
            if score > best_score:
                best_score, best_reason = score, f"shared words: {', '.join(sorted(overlap))}"

    # 5. Fallback: raw string similarity on both stem and rubric title.
    candidates = [c for c in (rubric_norm, stem_norm) if c]
    best_sim = max(
        (SequenceMatcher(None, assignment_norm, c).ratio() for c in candidates),
        default=0.0,
    )
    if best_sim >= 0.6:
        score = best_sim * 0.7
        if score > best_score:
            best_score, best_reason = score, "similar title text"

    if best_score > 0:
        return best_score, best_reason
    return 0.0, ""


def suggest_config_for_assignment(
    assignment_title: str, config_files: list[Path]
) -> tuple[Optional[Path], float, str]:
    """Pick the best rubric file for an assignment title, with score + reason."""
    best_path: Optional[Path] = None
    best_score = 0.0
    best_reason = ""
    for config_path in config_files:
        score, reason = score_config_for_assignment(config_path, assignment_title)
        if score > best_score:
            best_path, best_score, best_reason = config_path, score, reason
    return best_path, best_score, best_reason


def list_config_files() -> list[Path]:
    """Find assignment rubric JSON files in ``assignments/``, ``config/`` and ``configs/``.

    Mirrors ``app.get_all_config_files`` but without importing Streamlit, so the
    headless daily-check ``--grade`` path resolves rubrics the same way the UI
    does. De-duplicated by file name (earlier folders win over later ones).
    ``configs/`` holds the richer spec files (``*_spec.json``).
    """
    configs: list[Path] = []
    seen_names: set[str] = set()
    for directory in (ASSIGNMENTS_DIR, CONFIG_DIR, SPECS_DIR):
        if not directory.is_dir():
            continue
        for file in sorted(directory.glob("*.json")):
            if file.name in _NON_RUBRIC_NAMES:
                continue
            if file.name not in seen_names:
                configs.append(file)
                seen_names.add(file.name)
    return configs


def build_config_resolver(config_files: Optional[list[Path]] = None):
    """Return ``(assignment) -> (config_path, rubric) | None`` for grading.

    ``rubric`` is either an :class:`AssignmentConfig` (legacy ``lesson_*.json``)
    or an :class:`AssignmentSpec` (the richer ``sections`` schema). The
    resolution order is: remembered ``course_work_id -> config`` mapping, then a
    fuzzy title match, then ``None`` (the caller skips the assignment with a
    visible reason rather than grading against the wrong rubric).
    """
    files = list_config_files() if config_files is None else config_files

    def _resolve(assignment: CourseWork) -> Optional[tuple[Path, AssignmentConfig | AssignmentSpec]]:
        path = resolve_mapped_config(assignment.id)
        if path is None:
            suggestion, _score, _reason = suggest_config_for_assignment(
                assignment.title, files
            )
            path = suggestion
        if path is None:
            return None
        try:
            return path, load_rubric(path)
        except (OSError, ValueError) as exc:
            logger.warning("Could not load rubric %s: %s", path, exc)
            return None

    return _resolve


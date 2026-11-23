"""Loading and validating an exam definition file.

The definition is deliberately small: it names the versions and points at the
paper and rubric files that already exist. Questions live in the worksheet,
answers live in the check file, so neither is duplicated here.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from portal.paths import RUBRIC_ROOTS

logger = logging.getLogger(__name__)

EXAM_DIR = Path(__file__).resolve().parent
CHECK_DIR = EXAM_DIR / "checks"
_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,60}$")
_VERSION_RE = re.compile(r"^[a-z][a-z0-9_]{0,20}$")


class ExamConfigError(ValueError):
    """The exam definition is missing data or contradicts itself."""


@dataclass(frozen=True)
class ExamVersion:
    key: str
    label: str
    worksheet: str
    rubric: str

    @property
    def rubric_path(self) -> Path:
        from portal.paths import PROJECT_ROOT

        return (PROJECT_ROOT / self.rubric).resolve()


@dataclass(frozen=True)
class ExamConfig:
    name: str
    title: str
    instructions: str
    total_points: int
    shuffle_questions: bool
    shuffle_options: bool
    versions: tuple[ExamVersion, ...]
    check_file: str

    def version(self, key: str) -> ExamVersion:
        for version in self.versions:
            if version.key == key:
                return version
        raise ExamConfigError(f"unknown exam version {key!r} in {self.name}")

    @property
    def version_keys(self) -> tuple[str, ...]:
        return tuple(version.key for version in self.versions)

    @property
    def check_path(self) -> Path:
        return (EXAM_DIR / self.check_file).resolve()


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ExamConfigError(message)


def _rubric_allowed(path: Path) -> bool:
    resolved = path.resolve()
    return any(
        resolved == root.resolve() or root.resolve() in resolved.parents
        for root in RUBRIC_ROOTS
    )


def parse_exam(
    data: Any, *, name: str, require_rubric_files: bool = True
) -> ExamConfig:
    """Validate a decoded definition (raises :class:`ExamConfigError`).

    ``require_rubric_files`` is off only while authoring: the rubric is
    generated from the paper, so it cannot exist before the first build.
    """
    _require(isinstance(data, dict), "the exam definition must be an object")
    _require(data.get("version") == 1, "unsupported exam definition version")
    _require(str(data.get("name") or "") == name, f"exam name must be {name!r}")

    versions_raw = data.get("versions")
    _require(
        isinstance(versions_raw, list) and versions_raw,
        "an exam needs at least one version",
    )
    versions: list[ExamVersion] = []
    seen: set[str] = set()
    for raw in versions_raw:
        _require(isinstance(raw, dict), "a version must be an object")
        key = str(raw.get("key") or "")
        _require(bool(_VERSION_RE.match(key)), f"invalid version key: {key!r}")
        _require(key not in seen, f"duplicate version key: {key}")
        seen.add(key)
        worksheet = str(raw.get("worksheet") or "")
        _require(
            bool(_NAME_RE.match(worksheet)),
            f"version {key}: invalid worksheet slug {worksheet!r}",
        )
        rubric = str(raw.get("rubric") or "").replace("\\", "/")
        _require(bool(rubric), f"version {key}: a rubric path is required")
        candidate = ExamVersion(
            key=key,
            label=str(raw.get("label") or "").strip() or key,
            worksheet=worksheet,
            rubric=rubric,
        )
        _require(
            _rubric_allowed(candidate.rubric_path),
            f"version {key}: rubric is outside the allowed directories",
        )
        if require_rubric_files:
            _require(
                candidate.rubric_path.is_file(),
                f"version {key}: rubric file not found: {rubric}",
            )
        versions.append(candidate)

    total_points = data.get("total_points")
    _require(
        isinstance(total_points, int) and 0 < total_points <= 1000,
        "total_points must be an integer between 1 and 1000",
    )
    check_file = str(data.get("check_file") or "").replace("\\", "/")
    _require(bool(check_file), "check_file is required")
    check_path = (EXAM_DIR / check_file).resolve()
    _require(
        CHECK_DIR.resolve() in check_path.parents,
        "check_file must live in portal/exams/checks",
    )
    _require(check_path.is_file(), f"check file not found: {check_file}")

    return ExamConfig(
        name=name,
        title=str(data.get("title") or "").strip(),
        instructions=str(data.get("instructions") or "").strip(),
        total_points=int(total_points),
        shuffle_questions=bool(data.get("shuffle_questions", True)),
        shuffle_options=bool(data.get("shuffle_options", True)),
        versions=tuple(versions),
        check_file=check_file,
    )


_CACHE: dict[str, tuple[float, ExamConfig]] = {}


def load_exam(name: str, *, require_rubric_files: bool = True) -> ExamConfig | None:
    """Load ``<name>.json``; ``None`` when absent or invalid.

    A broken definition must not take a class page down. The caller treats
    ``None`` as "this is not a working exam" and says so plainly to the teacher;
    students never see a half-rendered paper.
    """
    if not _NAME_RE.match(name or ""):
        return None
    path = EXAM_DIR / f"{name}.json"
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    cached = _CACHE.get(name)
    if (
        require_rubric_files
        and cached is not None
        and cached[0] == mtime
    ):
        return cached[1]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        config = parse_exam(
            data, name=name, require_rubric_files=require_rubric_files
        )
    except (OSError, ValueError) as exc:
        logger.warning("Inactive exam %s: %s", path.name, exc)
        return None
    if require_rubric_files:
        _CACHE[name] = (mtime, config)
    return config


def for_assignment(assignment: Any) -> ExamConfig | None:
    """The exam an assignment belongs to, or ``None`` for ordinary homework."""
    name = str(getattr(assignment, "exam_config", "") or "").strip()
    if not name:
        return None
    return load_exam(name)


def clear_cache() -> None:
    """Drop cached definitions (tests and the reload CLI)."""
    _CACHE.clear()


__all__ = [
    "CHECK_DIR",
    "EXAM_DIR",
    "ExamConfig",
    "ExamConfigError",
    "ExamVersion",
    "clear_cache",
    "for_assignment",
    "load_exam",
    "parse_exam",
]
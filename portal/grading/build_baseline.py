"""Regenerate ``portal/grading/baseline.json`` (Phase 0 fidelity baseline).

Run from the project root::

    python -m portal.grading.build_baseline

The baseline records:

* the git commit and whether the *existing* system's files are dirty;
* SHA-256 of every file the portal depends on, so a later change to the
  engine is visible in one diff;
* the evaluator constants that define grading behaviour;
* the measured existing test baseline (see ``docs/LMS_PORTAL_PLAN.md`` §1.9).

It never records secrets. The model id is read from the environment only for
information; no API key ever enters the file.
"""
from __future__ import annotations

import argparse
import json
import platform
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from grading_engine.settings import DEFAULT_LATE_PENALTY_POINTS, settings
from portal.paths import PORTAL_ROOT, PROJECT_ROOT
from portal.grading.adapter import hash_file

BASELINE_PATH = PORTAL_ROOT / "grading" / "baseline.json"

# Files the portal reads. Keep this list explicit: a new dependency on a
# changed file must show up in review, not be discovered by a grader drift.
CRITICAL_FILES: tuple[str, ...] = (
    "app.py",
    "config/settings.py",
    "grading_engine/code_checks.py",
    "grading_engine/grade_audit.py",
    "grading_engine/late_policy.py",
    "grading_engine/llm_evaluator.py",
    "grading_engine/llm_router.py",
    "grading_engine/main.py",
    "grading_engine/models.py",
    "grading_engine/seams.py",
)


def _git(*args: str) -> str | None:
    try:
        return subprocess.check_output(
            ["git", *args],
            cwd=PROJECT_ROOT,
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def _existing_system_dirty() -> bool:
    """Whether tracked files have uncommitted changes."""
    output = _git(
        "status",
        "--porcelain",
        "--",
        "grading_engine",
        "portal",
        "tests",
        "assignments",
        "configs",
    )
    return bool(output)


def _critical_file_hashes() -> dict[str, str]:
    # Baseline keys keep the ORIGINAL ``src/...`` / ``config/settings.py``
    # names so the drift-check mapping (check_baseline_drift.py) stays stable,
    # but the bytes hashed are the standalone ``grading_engine/`` copies.
    files: dict[str, str] = {
        "config/settings.py": hash_file(
            PROJECT_ROOT / "grading_engine" / "settings.py"
        )
    }
    _src_keys = {
        "code_checks.py": "src/code_checks.py",
        "grade_audit.py": "src/grade_audit.py",
        "late_policy.py": "src/late_policy.py",
        "llm_evaluator.py": "src/llm_evaluator.py",
        "llm_router.py": "src/llm_router.py",
        "main.py": "src/main.py",
        "models.py": "src/models.py",
        "seams.py": "src/seams.py",
    }
    for name, key in _src_keys.items():
        files[key] = hash_file(PROJECT_ROOT / "grading_engine" / name)
    for path in sorted((PROJECT_ROOT / "grading_engine" / "evaluator").glob("*.py")):
        files[f"src/evaluator/{path.name}"] = hash_file(path)
    return dict(sorted(files.items()))


def _constants() -> dict[str, Any]:
    from grading_engine.evaluator.config import (  # noqa: PLC0415 - intentional probe
        EVAL_PAUSE_SECONDS,
        MAX_DOC_CHARS,
        MAX_TOKENS,
        REASONING_EFFORT,
        TEMPERATURE,
    )
    from grading_engine.evaluator.schema import _STAGES  # noqa: PLC0415

    prompt_source = (
        PROJECT_ROOT / "grading_engine" / "evaluator" / "prompts.py"
    ).read_text(encoding="utf-8")
    return {
        "temperature": TEMPERATURE,
        "max_tokens": MAX_TOKENS,
        "max_doc_chars": MAX_DOC_CHARS,
        "reasoning_effort": REASONING_EFFORT,
        "stages": [stage[0] for stage in _STAGES],
        "eval_pause_seconds": EVAL_PAUSE_SECONDS,
        "default_late_penalty_points": DEFAULT_LATE_PENALTY_POINTS,
        "prompt_calls_submission_google_doc": "Google Doc" in prompt_source,
        "configured_model_for_information_only": settings.groq_model,
    }


def build_baseline(*, passed: int, failed: int, recorded_by: str) -> dict[str, Any]:
    return {
        "schema": "portal.grading.baseline/v1",
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "recorded_by": recorded_by,
        "git": {
            "commit": _git("rev-parse", "HEAD"),
            "branch": _git("rev-parse", "--abbrev-ref", "HEAD"),
            "existing_system_dirty": _existing_system_dirty(),
            "existing_system_dirty_note": (
                "These modifications pre-date the portal work and were not "
                "made or touched by portal/. The per-file hashes below pin "
                "the exact bytes the portal was built against."
            ),
        },
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "files": _critical_file_hashes(),
        "constants": _constants(),
        "test_baseline": {
            "command": "python -m pytest tests -q",
            "passed": passed,
            "failed": failed,
            "scope": "tests/ only; root pytest collection stops on "
            "src/test_script.py (null bytes) - see the plan §1.9",
            "note": "The 13 failures were measured before the portal existed "
            "and are pre-existing/environmental. They are not caused by "
            "portal/ and were not modified.",
        },
        "invariants": {
            "adapter": "portal/grading/adapter.py",
            "engine_entrypoint": "src.evaluator.core:"
            "evaluate_student_submission",
            "submission_text": "passed verbatim; no trimming or escaping",
            "prompt": "unchanged; still says 'Google Doc' by design",
            "late_policy": "src.late_policy.penalty_for with "
            "late_state=None, is_late=None, first_submitted_at locked at "
            "the first portal hand-in",
            "audit": "last_audit_for popped once, immediately, same thread",
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--passed",
        type=int,
        default=530,
        help="Passing tests in the measured existing-suite baseline.",
    )
    parser.add_argument(
        "--failed",
        type=int,
        default=13,
        help="Failing tests in the measured existing-suite baseline.",
    )
    parser.add_argument(
        "--recorded-by",
        default="phase-0",
        help="Free-text label stored in the JSON.",
    )
    args = parser.parse_args()

    payload = build_baseline(
        passed=args.passed,
        failed=args.failed,
        recorded_by=args.recorded_by,
    )
    BASELINE_PATH.parent.mkdir(parents=True, exist_ok=True)
    BASELINE_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"Wrote {BASELINE_PATH.relative_to(PROJECT_ROOT)}")
    print(
        f"  commit={payload['git']['commit']} "
        f"files={len(payload['files'])} "
        f"tests={args.passed} passed/{args.failed} failed"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

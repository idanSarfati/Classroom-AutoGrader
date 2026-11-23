"""One real grade through the engine, without touching the portal database.

Run it after moving the portal to a server. It proves, in one call, that

* the rubric files are where ``portal_assignments.rubric_path`` says they are
  (and their SHA-256 still matches the value recorded at publish time);
* the pinned model is reachable with the API keys in ``.env``;
* the deterministic audit still runs next to the model.

Nothing is written: no submission, no job, no grade. The score is printed and
discarded. This consumes one real model call.

Usage::

    python -m portal.scripts.check_engine
    python -m portal.scripts.check_engine --rubric assignments/lesson_3_worksheet.json
"""
from __future__ import annotations

import argparse

from portal.grading.adapter import (
    GradeArtifact,
    RubricBindingError,
    grade_submission,
    load_rubric,
)
from portal.grading.envguard import sanitize_no_proxy_env

DEFAULT_RUBRIC = "assignments/lesson_3.json"

# Deliberately incomplete: the point is that the engine answers, not that the
# answer earns a particular score. The heading shape matches what the portal
# composes, so the model sees a realistic document.
SAMPLE_TEXT = """## תרגיל 1: הדפסה
print("שלום עולם")
"""


def run(
    rubric_path: str = DEFAULT_RUBRIC, text: str = SAMPLE_TEXT
) -> GradeArtifact:
    binding = load_rubric(rubric_path)
    return grade_submission(
        student_id="deploy-check",
        student_name="בדיקת פריסה",
        submission_id="deploy-check",
        raw_doc_text=text,
        rubric=binding,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--rubric",
        default=DEFAULT_RUBRIC,
        help=f"rubric path relative to the project root (default: {DEFAULT_RUBRIC})",
    )
    args = parser.parse_args(argv)
    # The web app does this at startup; a plain shell may still export the
    # bracketed NO_PROXY form that makes httpx reject the grading client.
    sanitize_no_proxy_env()
    print("This makes one real model call; nothing is saved.")
    try:
        artifact = run(args.rubric)
    except RubricBindingError as exc:
        print(f"rubric problem: {exc}")
        return 1
    except Exception as exc:  # noqa: BLE001 - the message is the report
        print(f"grading failed: {type(exc).__name__}: {exc}")
        return 1
    print(
        f"ok: score {artifact.result.score}, model {artifact.configured_model}, "
        f"rubric {artifact.rubric_sha256[:12]}, "
        f"audit {'yes' if artifact.audit is not None else 'no'}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

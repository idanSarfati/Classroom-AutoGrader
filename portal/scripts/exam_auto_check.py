"""Run one automatic exam model-check pass now.

The portal service already runs the pass every 3 hours in the background (see
:mod:`portal.exams.auto_check`). This CLI runs the same single pass on demand:
useful to catch up right after enabling the feature, or from a cron entry on
hosts that do not run the app.

It never publishes results - it only writes the finished model verdicts into the
same ``Grade`` rows the teacher would get from the manual apply button.

Usage::

    python -m portal.scripts.exam_auto_check
    python -m portal.scripts.exam_auto_check --limit 5
    python -m portal.scripts.exam_auto_check --limit 5 --database-url sqlite:////tmp/portal.db
"""
from __future__ import annotations

import argparse
import sys

from portal.config import load_settings
from portal.db import build_engine, build_session_factory
from portal.exams import auto_check
from portal.exams import llm_review


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help=f"papers to process (default {auto_check.DEFAULT_BATCH_SIZE})",
    )
    parser.add_argument("--database-url", default=None)
    args = parser.parse_args(argv)

    if not llm_review.enabled():
        print("the exam model pass is off (EXAM_LLM_REVIEW); nothing to run.")
        return 2

    settings = load_settings()
    engine = build_engine(args.database_url or settings.database_url)
    factory = build_session_factory(engine)
    try:
        stats = auto_check.tick(
            factory,
            limit=args.limit or auto_check.DEFAULT_BATCH_SIZE,
            source="cli",
        )
        print(
            "automatic exam pass: "
            + ", ".join(f"{key}={value}" for key, value in stats.items())
        )
        return 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    sys.exit(main())

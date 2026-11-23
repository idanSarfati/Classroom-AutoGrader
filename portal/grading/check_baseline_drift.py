"""Phase 0 drift gate: engine files must match ``baseline.json``.

The grading engine was copied verbatim from ``src/`` + ``config/settings.py``
into ``grading_engine/`` in the standalone clone. ``portal/grading/baseline.json``
pins SHA-256 of the ORIGINAL live files (``src/...``, ``config/settings.py``,
``app.py``). This script maps those keys to the copied files and verifies
byte-identical content, so any accidental edit during migration is caught
before behaviour is assumed unchanged.

Run from the clone root::

    python -m portal.grading.check_baseline_drift

Exit code 0 means no drift. The pytest gate is
``portal/tests/test_baseline_drift.py``. After the import-rewrite phases the
baseline must be regenerated (``python -m portal.grading.build_baseline``)
because import paths change bytes; the parity tests
(``portal/tests/test_parity.py``) then prove behaviour is unchanged.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

# portal/grading/<this file> -> parents[2] is the clone root.
PROJECT_ROOT = Path(__file__).resolve().parents[2]
BASELINE_PATH = PROJECT_ROOT / "portal" / "grading" / "baseline.json"


def candidate_for(rel: str) -> Path:
    """Where the baseline-pinned file now lives in the standalone layout."""
    if rel == "config/settings.py":
        return PROJECT_ROOT / "grading_engine" / "settings.py"
    if rel.startswith("src/"):
        return PROJECT_ROOT / "grading_engine" / rel[len("src/"):]
    return PROJECT_ROOT / rel


def hash_file(path: Path) -> str:
    """SHA-256 of a file's exact bytes, lowercase hex."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def collect_drift(
    baseline_path: Path = BASELINE_PATH,
) -> list[tuple[str, bool, str, str, Path]]:
    """Return ``(baseline_key, ok, expected, actual, candidate)`` per file."""
    payload = json.loads(baseline_path.read_text(encoding="utf-8"))
    results: list[tuple[str, bool, str, str, Path]] = []
    for rel, expected in sorted(payload.get("files", {}).items()):
        candidate = candidate_for(rel)
        actual = hash_file(candidate) if candidate.is_file() else "MISSING"
        results.append((rel, actual == expected, str(expected), actual, candidate))
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, default=BASELINE_PATH)
    args = parser.parse_args(argv)
    if not args.baseline.is_file():
        print(f"baseline not found: {args.baseline}", file=sys.stderr)
        return 2
    results = collect_drift(args.baseline)
    drift = [row for row in results if not row[1]]
    for rel, ok, expected, actual, candidate in results:
        state = "OK" if ok else "DRIFT"
        print(f"{state} {rel} -> {candidate.relative_to(PROJECT_ROOT).as_posix()}")
        if not ok:
            print(f"     expected {expected}")
            print(f"     actual   {actual}")
    print(f"checked {len(results)} files, drift {len(drift)}")
    return 0 if not drift else 1


if __name__ == "__main__":
    raise SystemExit(main())

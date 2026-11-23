"""Phase 0 gate: engine bytes must match ``portal/grading/baseline.json``.

Copies were byte-identical at extraction time (``DRIFT 0``). If this test
goes red, someone edited the engine outside a deliberate change — stop and
diff before assuming behaviour is unchanged. To re-pin after a deliberate
engine change, regenerate the baseline and review the diff line by line:

    python -m portal.grading.build_baseline --recorded-by standalone-phase-0
"""
from __future__ import annotations

from portal.grading.check_baseline_drift import collect_drift


def test_engine_bytes_match_baseline() -> None:
    results = collect_drift()
    assert results, "baseline.json is missing or has no files"
    drift = [row for row in results if not row[1]]
    assert not drift, "\n".join(
        f"DRIFT {rel} -> {candidate}: expected {expected}, got {actual}"
        for rel, _ok, expected, actual, candidate in drift
    )

"""Prove the portal is independent of src/ and config/, then restore.

Two modes:

1. If ``src/`` / ``config/`` are present, they are temporarily renamed off so
   any lingering ``import src...`` / ``import config.settings`` fails loudly,
   then restored.
2. If they are already absent (the minimal standalone layout), there is
   nothing to rename — the imports below simply must succeed.

In both cases exit 0 means the portal needs no outer ``src``/``config``.
"""
from __future__ import annotations

import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

_PAIRS = [("src", "src_off"), ("config", "config_off")]


def _restore(a: str, b: str) -> None:
    if (ROOT / b).exists() and not (ROOT / a).exists():
        os.rename(ROOT / b, ROOT / a)


def _turn_off(a: str, b: str) -> bool:
    """Rename ``a`` to ``b``. Returns False if ``a`` is already absent."""
    if (ROOT / a).exists():
        if not (ROOT / b).exists():
            os.rename(ROOT / a, ROOT / b)
        return True
    return False


# Normalize any earlier partial state, then turn src/config OFF if present.
present = False
for a, b in _PAIRS:
    _restore(a, b)
for a, b in _PAIRS:
    if _turn_off(a, b):
        present = True

print(
    "proof state -> src present:", (ROOT / "src").exists(),
    "| config present:", (ROOT / "config").exists(),
)

ok = False
try:
    import portal.main  # noqa: F401 - the FastAPI web app
    import grading_engine.llm_evaluator  # noqa: F401 - the engine facade

    ok = True
    note = "they are not even on disk" if not present else "renamed off on disk"
    print(f"PROOF PASS: portal.main + grading_engine imported with no src/ or config/ ({note})")
except Exception as exc:  # noqa: BLE001
    import traceback

    traceback.print_exc()
    print("PROOF FAIL:", type(exc).__name__, exc)
finally:
    for a, b in _PAIRS:
        _restore(a, b)

print(
    "restored -> src present:", (ROOT / "src").exists(),
    "| config present:", (ROOT / "config").exists(),
)
sys.exit(0 if ok else 1)
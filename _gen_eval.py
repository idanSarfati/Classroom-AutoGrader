"""Generate src/evaluator/* + src/llm_evaluator.py shim from the monolith (temporary).

Extracts top-level function/class/assign bodies VERBATIM by line range and wraps
each in a generated module with:
  * the original import block (so every stdlib/third-party/src name resolves);
  * static imports for evaluator-defined names that moved to sibling modules;
  * seam-wiring that rebinds monkeypatchable names to facade-resolving proxies;
  * targeted rewrites that route module-level mutable state through the facade.

The facade (src/llm_evaluator.py) is rewritten LAST, only after every module
compiles, so a failed run never leaves the tree broken.

Prints a report of every generated module, the seams it wires, orphan names
(defined but placed nowhere), dependency cycles, and unclassified free names.
"""
from __future__ import annotations

import ast
import builtins
from pathlib import Path

SRC_FILE = Path("src/llm_evaluator.py")
SRC = SRC_FILE.read_text(encoding="utf-8")
LINES = SRC.splitlines()
TREE = ast.parse(SRC, filename="src/llm_evaluator.py")

FACADE = "src.llm_evaluator"
IMPORT_BLOCK_START = 22    # 'from __future__ ...'
IMPORT_BLOCK_END = 56      # ')' of src.models import


def node_span(node) -> tuple[int, int]:
    """Span of a node, extended upward over contiguous leading comment lines."""
    decs = [d.lineno for d in getattr(node, "decorator_list", [])]
    start = min(decs + [node.lineno])
    while start - 1 >= 1 and LINES[start - 2].lstrip().startswith("#"):
        start -= 1
    return start, node.end_lineno


def slice_lines(start: int, end: int) -> str:
    return "\n".join(LINES[start - 1 : end])


# Top-level defined names -> source span (in file order).
defined_span: dict[str, tuple[int, int]] = {}
defined_order: list[str] = []
for node in TREE.body:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        name = node.name
    elif isinstance(node, ast.Assign):
        names = [t.id for t in node.targets if isinstance(t, ast.Name)]
        if not names:
            continue
        name = names[0]
        for extra in names[1:]:
            defined_span[extra] = node_span(node)
            defined_order.append(extra)
    elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        name = node.target.id
    else:
        continue
    defined_span[name] = node_span(node)
    defined_order.append(name)

# Imported names -> source module (for reference classification only).
imported_from: dict[str, str] = {}
for node in TREE.body:
    if isinstance(node, ast.ImportFrom):
        mod = ("." * node.level) + (node.module or "")
        for a in node.names:
            if a.name != "*":
                imported_from[a.asname or a.name] = mod
    elif isinstance(node, ast.Import):
        for a in node.names:
            imported_from[a.asname or a.name.split(".")[0]] = a.name

IMPORTED_NAMES = set(imported_from)
DEFINED_NAMES = set(defined_span)
BUILTIN_NAMES = set(dir(builtins)) | {"__name__", "__file__", "__doc__"}

# ---------------------------------------------------------------------------
# Module layout: evaluator module -> (docstring, names in original file order).
# ---------------------------------------------------------------------------
LAYOUT: dict[str, tuple[str, list[str]]] = {
    "config": (
        "Evaluator configuration: sampling knobs, retry/pacing constants, "
        "prompt strings, rubric type alias, and shared helpers built on them.",
        ["Rubric", "TEMPERATURE", "MAX_TOKENS", "MAX_DOC_CHARS",
         "_TRIM_SEARCH_CHARS", "_TRIM_MARKER", "EVAL_PAUSE_SECONDS",
         "REASONING_EFFORT", "_RETRYABLE_STATUS_CODES", "_MAX_ATTEMPTS",
         "_BACKOFF_MULTIPLIER", "_MAX_BACKOFF_SECONDS", "_MAX_RETRY_AFTER_SECONDS",
         "_max_workers", "_RATE_LIMIT_RETRIES", "_RATE_LIMIT_BACKOFF_BASE",
         "_RATE_LIMIT_WAIT_CAP", "_REQUEST_TIMEOUT_SECONDS",
         "_STRICT_SCHEMA_KEYWORDS", "_SCHEMA_NAME", "SYSTEM_PROMPT"],
    ),
    "errors": (
        "Error hierarchy raised by the LLM evaluation pipeline.",
        ["LLMEvaluationError", "LLMJSONParsingError", "LLMAPIError"],
    ),
    "pacer": (
        "Shared adaptive pacer and the between-students pause used by every "
        "batch path.",
        ["_PACER", "_pacer", "paced_pause"],
    ),
    "audit": (
        "Deterministic audit verdicts attached to GradingResult via a sidecar "
        "map (GradingRow lives in src.main, importing it here would cycle).",
        ["_AUDIT_SIDECAR", "last_audit_for", "_run_audit"],
    ),
    "prompts": (
        "Prompt construction: document trimming, exempt/code/spec blocks, and "
        "the rubric->prompt assembly.",
        ["trim_document_text", "_snap_back", "_snap_forward", "_EXEMPT_HEADER",
         "_exempt_tasks_block", "_code_scan_block", "_spec_scan_block",
         "rubric_title", "rubric_tasks_json", "_build_prompt"],
    ),
    "schema": (
        "Strict JSON-Schema response format and the graded request stage "
        "ladder (json_schema -> json_object -> text).",
        ["_to_strict_json_schema", "_build_response_format", "_RESPONSE_FORMAT",
         "_JSON_OBJECT_FORMAT", "_JSON_ONLY_INSTRUCTION", "_CONCISE_REMINDER",
         "_STAGES"],
    ),
    "parsing": (
        "Response parsing: fence/prose/truncation repair, payload coercion, "
        "and the deduction-breakdown audit.",
        ["_ERROR_SNIPPET_CHARS", "_GRADING_KEY_ALIASES", "_DEDUCTION_KEY_ALIASES",
         "_TASK_KEY_ALIASES", "_STATUS_ALIASES", "_WRAPPER_KEYS",
         "_extract_content", "_FENCE_LINE_RE", "_TRAILING_COMMA_RE",
         "_CLOSE_TO_OPEN", "_strip_code_fences", "_json_span", "_close_truncated",
         "repair_json", "_repair_candidates", "_parse_payload", "_one_line",
         "_audit_deductions", "_parse_result", "_norm_key", "_canonical_key",
         "_rename_keys", "_coerce_score", "_coerce_points", "_coerce_status",
         "_coerce_task", "_coerce_deduction", "_coerce_deductions",
         "_coerce_payload"],
    ),
    "transport": (
        "LLM transport: shared router/client singletons, retries, response-"
        "format staging, and fallback serving.",
        ["_ROUTER", "_ROUTER_READY", "_router", "_client", "_get_client",
         "_is_retryable_error", "_retry_after_seconds", "_wait_before_retry",
         "_log_retry", "_create_completion", "_error_detail",
         "_is_json_generation_error", "_is_degradable", "_rate_limit_wait",
         "_call_completion", "_call_completion_once", "_serve_from_router",
         "_serve_decision", "_client_for", "_note_rate_limited"],
    ),
    "core": (
        "Single-submission evaluation: build prompt, walk the stage ladder, "
        "audit the result, apply the late penalty.",
        ["_first_working_stage", "evaluate_student_submission"],
    ),
    "batch": (
        "Concurrent batch grading: request/outcome dataclasses and the "
        "thread-pool runner.",
        ["StudentGradeRequest", "StudentGradeOutcome", "_grade_one_request",
         "grade_students_batch"],
    ),
}

# Names the test-suite monkeypatches on the facade with monkeypatch.setattr.
# Their bodies reference them as plain globals, so the owning module rebinds
# them at module bottom to facade-resolving proxies (Python resolves globals at
# call time). Owner must be the module that DEFINES the function.
SEAM_OWNERS: dict[str, str] = {
    "_pacer": "pacer",
    "_parse_result": "parsing",
    "_get_client": "transport",
    "_create_completion": "transport",
    "_call_completion": "transport",
    "evaluate_student_submission": "core",
}
ALL_SEAMS = set(SEAM_OWNERS)

OWNER: dict[str, str] = {}
for mod, (_doc, names) in LAYOUT.items():
    for n in names:
        if n in OWNER:
            raise SystemExit(f"DUPLICATE LAYOUT placement for {n!r}")
        OWNER[n] = mod

# ---------------------------------------------------------------------------
# Targeted rewrites: function name -> [(old, new), ...]. Each `old` MUST occur
# exactly once inside that function's extracted source or the run aborts.
# They route module-level mutable state through the facade so
# monkeypatch.setattr(evaluator, ...) stays visible to moved code, and make a
# patched EVAL_PAUSE_SECONDS readable where the pacer consumes it.
# ---------------------------------------------------------------------------
REWRITES: dict[str, list[tuple[str, str]]] = {
    "_router": [
        (
            "    global _ROUTER, _ROUTER_READY\n"
            "    if _ROUTER_READY:\n"
            "        return _ROUTER\n"
            "    _ROUTER_READY = True\n",
            "    global _ROUTER, _ROUTER_READY\n"
            '    if resolve(FACADE, "_ROUTER_READY", _ROUTER_READY):\n'
            '        return resolve(FACADE, "_ROUTER", _ROUTER)\n'
            '    _ROUTER_READY = set_on_facade(\n'
            '        FACADE, "_ROUTER_READY", True, _ROUTER_READY\n'
            "    )\n",
        ),
        (
            "    try:\n"
            "        _ROUTER = build_router_from_settings(settings)\n"
            "    except Exception as exc:\n"
            '        logger.warning("LLM router disabled (%s); single-key mode.", exc)\n'
            "        _ROUTER = None\n"
            "    if _ROUTER is not None:\n"
            '        logger.info("LLM routing: %s.", _ROUTER.describe())\n'
            "    return _ROUTER\n",
            "    try:\n"
            "        built = build_router_from_settings(settings)\n"
            "    except Exception as exc:\n"
            '        logger.warning("LLM router disabled (%s); single-key mode.", exc)\n'
            "        built = None\n"
            '    _ROUTER = set_on_facade(FACADE, "_ROUTER", built, _ROUTER)\n'
            "    if built is not None:\n"
            '        logger.info("LLM routing: %s.", built.describe())\n'
            "    return built\n",
        ),
    ],
    "_pacer": [
        (
            "    global _PACER\n"
            "    if _PACER is None:\n"
            "        try:\n"
            "            from src.llm_router import AdaptivePacer\n"
            "        except Exception:\n"
            "            return None\n"
            "        _PACER = AdaptivePacer(base_seconds=EVAL_PAUSE_SECONDS)\n"
            "    return _PACER\n",
            "    global _PACER\n"
            '    _cached = resolve(FACADE, "_PACER", _PACER)\n'
            "    if _cached is None:\n"
            "        try:\n"
            "            from src.llm_router import AdaptivePacer\n"
            "        except Exception:\n"
            "            return None\n"
            '        pause = resolve(FACADE, "EVAL_PAUSE_SECONDS", EVAL_PAUSE_SECONDS)\n'
            "        _cached = AdaptivePacer(base_seconds=pause)\n"
            '        _PACER = set_on_facade(FACADE, "_PACER", _cached, _PACER)\n'
            "    return _cached\n",
        ),
    ],
    "paced_pause": [
        (
            '        ticked_sleep(EVAL_PAUSE_SECONDS, on_tick, "pacing")\n',
            "        ticked_sleep(\n"
            '            resolve(FACADE, "EVAL_PAUSE_SECONDS", EVAL_PAUSE_SECONDS),\n'
            "            on_tick,\n"
            '            "pacing",\n'
            "        )\n",
        ),
    ],
    "_get_client": [
        (
            "    global _client\n"
            "    if _client is None:\n",
            "    global _client\n"
            '    if resolve(FACADE, "_client", _client) is None:\n',
        ),
        (
            "        _client = OpenAI(\n",
            "        built = OpenAI(\n",
        ),
        (
            "            max_retries=0,\n"
            "        )\n"
            "    return _client\n",
            "            max_retries=0,\n"
            "        )\n"
            '        _client = set_on_facade(FACADE, "_client", built, _client)\n'
            '    return resolve(FACADE, "_client", _client)\n',
        ),
    ],
    "evaluate_student_submission": [
        (
            "    for index in range(_first_working_stage, len(_STAGES)):\n",
            "    start_stage = resolve(\n"
            '        FACADE, "_first_working_stage", _first_working_stage\n'
            "    )\n"
            "    for index in range(start_stage, len(_STAGES)):\n",
        ),
        (
            "        _first_working_stage = index\n"
            "        break\n",
            "        _first_working_stage = set_on_facade(\n"
            '            FACADE, "_first_working_stage", index, _first_working_stage\n'
            "        )\n"
            "        break\n",
        ),
    ],
}


# ---------------------------------------------------------------------------
# Extraction helpers.
# ---------------------------------------------------------------------------
def free_names(node) -> set[str]:
    """Names loaded anywhere in a subtree (locals included - filtered later)."""
    loads: set[str] = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Load):
            loads.add(sub.id)
    return loads


def stored_names(node) -> set[str]:
    """Names assigned anywhere in a subtree (incl. for/with/comprehension)."""
    stored: set[str] = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Store):
            stored.add(sub.id)
        elif isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            for a in ast.walk(sub.args):
                if isinstance(a, ast.arg) and a.arg:
                    stored.add(a.arg)
            if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                stored.add(sub.name)
        elif isinstance(sub, (ast.Import, ast.ImportFrom)):
            for a in sub.names:
                if a.asname:
                    stored.add(a.asname)
                elif a.name != "*":
                    stored.add(a.name.split(".")[0])
        elif isinstance(sub, ast.ClassDef):
            stored.add(sub.name)
        elif isinstance(sub, ast.ExceptHandler) and sub.name:
            stored.add(sub.name)
    return stored


NODES_BY_NAME: dict[str, ast.AST] = {}
for node in TREE.body:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        NODES_BY_NAME[node.name] = node
    elif isinstance(node, ast.Assign):
        for t in node.targets:
            if isinstance(t, ast.Name):
                NODES_BY_NAME[t.id] = node
    elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        NODES_BY_NAME[node.target.id] = node


def extract(name: str) -> str:
    """Source slice for a name, with comment header, rewrites applied."""
    node = NODES_BY_NAME[name]
    start, end = node_span(node)
    text = slice_lines(start, end) + "\n"  # trailing \n so end-of-body anchors match
    for old, new in REWRITES.get(name, []):
        count = text.count(old)
        if count != 1:
            raise SystemExit(
                f"REWRITE ANCHOR FAILED for {name!r}: found {count} (want 1)\n"
                f"--- anchor ---\n{old}"
            )
        text = text.replace(old, new)
    return text


def build_module(mod: str, doc: str, names: list[str]) -> tuple[str, list[str], dict[str, set[str]], set[str]]:
    """Generate one evaluator module. Returns (text, local seams, cross, unclassified)."""
    nodes = [NODES_BY_NAME[n] for n in names]
    refs: set[str] = set()
    for node in nodes:
        refs |= free_names(node)
    local_seams = [n for n in names if n in ALL_SEAMS]

    # Static imports for evaluator-defined names that live in siblings
    # (includes seams whose default lives elsewhere: import the proxy directly).
    cross: dict[str, set[str]] = {}
    unclassified: set[str] = set()
    for name in sorted(refs & DEFINED_NAMES):
        if name in names:
            continue
        owner = OWNER.get(name)
        if owner is None:
            unclassified.add(name)
        elif owner != mod:
            cross.setdefault(f"src.evaluator.{owner}", set()).add(name)
    # Advisory: free refs that are neither local, imported, defined, nor obvious
    # locals/params - printed for eyeballing, never fatal (annotations/docs).
    stored: set[str] = set()
    for node in nodes:
        stored |= stored_names(node)
    unknown = (
        refs - DEFINED_NAMES - IMPORTED_NAMES - BUILTIN_NAMES - stored
        - {"annotations"}
    )

    slices = [extract(n) for n in names]
    bodies = "\n\n\n".join(b.rstrip("\n") for b in slices)
    has_state_wiring = any(
        "resolve(" in b or "set_on_facade(" in b
        for b in slices
    )

    out: list[str] = []
    out.append(f'"""{doc}')
    out.append("")
    out.append("Extracted verbatim from :mod:`src.llm_evaluator` (the former monolith).")
    out.append('"""')
    out.append("")
    out.append(slice_lines(IMPORT_BLOCK_START, IMPORT_BLOCK_END))
    out.append("")
    for srcmod in sorted(cross):
        out.append(f"from {srcmod} import (")
        for n in sorted(cross[srcmod]):
            out.append(f"    {n},")
        out.append(")")
    needs = []
    if local_seams:
        needs.append("seam_fn")
    if has_state_wiring:
        needs += ["resolve", "set_on_facade"]
    if needs:
        out.append(f"from src.seams import {', '.join(sorted(set(needs)))}")
    out.append("")
    out.append("logger = logging.getLogger(__name__)")
    out.append("")
    if mod == "config":
        # Original monolith side effects: silence SDK chatter at import time.
        out.append(slice_lines(66, 71))
        out.append("")
    if has_state_wiring:
        out.append(f'FACADE = "{FACADE}"')
        out.append("")
    out.append(bodies)
    out.append("")
    if local_seams:
        out.append("# " + "-" * 70)
        out.append("# Seam wiring")
        out.append("# " + "-" * 70)
        out.append(f"# Tests monkeypatch these on the ``{FACADE}`` facade. The bodies above")
        out.append("# reference them as plain globals; Python resolves globals at call time,")
        out.append("# so rebinding them here to facade-resolving proxies honours a patch")
        out.append("# without touching any call site.")
        for n in local_seams:
            out.append(f'{n} = seam_fn("{FACADE}", "{n}", {n})')
        out.append("")
    text = "\n".join(out)
    return text, local_seams, cross, unknown


def check_acyclic(edges: dict[str, set[str]]) -> list[str]:
    """DFS over the generated-module import graph; returns cycle paths found."""
    WHITE, GREY, BLACK = 0, 1, 2
    color = {m: WHITE for m in edges}
    cycles: list[str] = []
    stack_path: list[str] = []

    def visit(m: str) -> None:
        color[m] = GREY
        stack_path.append(m)
        for dep in sorted(edges.get(m, ())):
            if dep not in color:
                continue
            if color[dep] == GREY:
                idx = stack_path.index(dep)
                cycles.append(" -> ".join(stack_path[idx:] + [dep]))
            elif color[dep] == WHITE:
                visit(dep)
        stack_path.pop()
        color[m] = BLACK

    for m in sorted(edges):
        if color[m] == WHITE:
            visit(m)
    return cycles


def build_shim() -> str:
    """Rewrite src/llm_evaluator.py as a backward-compatible facade."""
    # Every name the monolith defined, grouped by its new home module.
    reexport: dict[str, list[str]] = {}
    for n in defined_order:
        if n == "logger":
            continue
        reexport.setdefault(f"src.evaluator.{OWNER[n]}", []).append(n)

    out: list[str] = []
    out.append(slice_lines(1, 20).rstrip())  # original module docstring
    out.append("")
    out.append("# " + "-" * 70)
    out.append("# Backward-compatible facade")
    out.append("# " + "-" * 70)
    out.append("# src/llm_evaluator.py is now a thin facade: every name the old monolith")
    out.append("# defined lives in a single-responsibility module under src/evaluator/ and")
    out.append("# is re-exported here, so ``import src.llm_evaluator`` and")
    out.append("# ``src.llm_evaluator.<name>`` - including test monkeypatching of seams and")
    out.append("# mutable state - keeps working unchanged.")
    out.append("")
    for srcmod in sorted(reexport):
        nlist = sorted(set(reexport[srcmod]))
        out.append(f"from {srcmod} import (")
        for n in nlist:
            out.append(f"    {n},")
        out.append(")")
    return "\n".join(out) + "\n"


def main() -> None:
    problems = 0
    # Orphans: defined in the monolith but placed in no LAYOUT bucket.
    orphans = [n for n in defined_order if n not in OWNER and n != "logger"]
    if orphans:
        print(f"!! ORPHAN names (not placed): {orphans}")
        raise SystemExit(1)

    ev_dir = Path("src/evaluator")
    ev_dir.mkdir(parents=True, exist_ok=True)
    edges: dict[str, set[str]] = {}

    for mod, (doc, names) in LAYOUT.items():
        text, seams, cross, unknown = build_module(mod, doc, names)
        edges[mod] = {src.split(".")[-1] for src in cross}
        (ev_dir / f"{mod}.py").write_text(text, encoding="utf-8")
        try:
            compile(text, f"{mod}.py", "exec")
        except SyntaxError as e:
            print(f"SYNTAX ERROR in evaluator/{mod}.py: {e}")
            problems += 1
        print(
            f"evaluator/{mod}.py  ({len(text.splitlines())} lines)  "
            f"wired={seams}  cross={sorted(cross)}"
        )
        if unknown:
            print(f"   advisory free refs: {sorted(unknown)}")

    cycles = check_acyclic(edges)
    if cycles:
        print(f"!! IMPORT CYCLES: {cycles}")
        problems += 1

    (ev_dir / "__init__.py").write_text(
        '"""Single-responsibility evaluator modules extracted from the former '
        'src/llm_evaluator.py monolith."""\n',
        encoding="utf-8",
    )

    shim = build_shim()
    # The shim is written last: a failed run must never leave a broken facade.
    SRC_FILE.write_text(shim, encoding="utf-8")
    try:
        compile(shim, "src/llm_evaluator.py", "exec")
    except SyntaxError as e:
        print(f"SYNTAX ERROR in src/llm_evaluator.py shim: {e}")
        problems += 1
    print(f"src/llm_evaluator.py shim ({len(shim.splitlines())} lines)")
    print("OK" if problems == 0 else f"{problems} PROBLEMS")
    raise SystemExit(1 if problems else 0)


if __name__ == "__main__":
    main()
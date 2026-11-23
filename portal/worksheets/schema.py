"""Schema, validation and answer composition for a mixed worksheet.

A worksheet is a list of *items* (one per exercise in the Doc). Each item has
one or more *parts*, and every part is one of six controls:

``code``
    The IDE editor with a starter program (complete-the-code exercises).
``code_plain``
    A multi-line code box with **no Run button**. Exams use it: a student who
    can execute the program beside a "what is the output?" question is no
    longer being asked anything.
``code_line``
    A single-line, LTR monospace input (``name = ____``).
``short``
    A small free-text box (for example "write the output you got").
``table``
    A fixed grid. Some columns are given and read-only, the rest are inputs
    (trace tables, code-to-output matching).
``choice``
    One or more statements, each with its own buttons (true/false, or the
    token choices inside a print/input line).

An item may also carry ``section`` and ``points``. Homework sheets leave them
empty; an exam paper groups its items into scored parts (חלק א', ב', ...).
They are display metadata only - field names, collection and composition are
unchanged, so a shuffled exam still composes the same canonical document.

A part may be marked ``"given": true``. It is a read-only snippet - the code a
student must trace, or the buggy program they must fix. It carries no input
field, is never collected, and is copied into the composed document once so
the reviewed hand-in is self-explanatory.

The same object does four jobs, so the form and the grader cannot drift:

* :meth:`Worksheet.questions` produces the headings used by the existing
  compose/split helpers in :mod:`portal.web.assignment_form`;
* :meth:`Worksheet.collect` turns submitted form fields into structured values;
* :meth:`Worksheet.compose` renders those values into the one labelled document
  the grading engine receives;
* :meth:`Worksheet.count_answered` drives the "answered N of M" counter.

Structured values are stored separately (see :mod:`portal.worksheets.store`) so
widgets reopen with the student's table cells and choices intact; the graded
text stays plain, readable Hebrew.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from portal.web.assignment_form import FormQuestion, compose_answers

logger = logging.getLogger(__name__)

SPEC_DIR = Path(__file__).resolve().parent
FIELD_PREFIX = "answer_"
KINDS = frozenset({"code", "code_plain", "code_line", "short", "table", "choice"})
_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,40}$")

MAX_CODE_CHARS = 200_000
MAX_LINE_CHARS = 500
MAX_SHORT_CHARS = 20_000
MAX_CELL_CHARS = 500

# Controls whose value is one plain string, and the cap that applies to it.
TEXT_LIMITS = {
    "code": MAX_CODE_CHARS,
    "code_plain": MAX_CODE_CHARS,
    "code_line": MAX_LINE_CHARS,
    "short": MAX_SHORT_CHARS,
}


class WorksheetSpecError(ValueError):
    """The worksheet file is missing data or contradicts itself."""


@dataclass(frozen=True)
class TableColumn:
    key: str
    label: str
    readonly: bool = False
    code: bool = False


@dataclass(frozen=True)
class TableRow:
    cells: tuple[str, ...]


@dataclass(frozen=True)
class ChoiceStatement:
    prompt: str
    options: tuple[str, ...] = ()
    code: str = ""


@dataclass(frozen=True)
class Part:
    key: str
    kind: str
    label: str = ""
    prompt: str = ""
    code: str = ""
    placeholder: str = ""
    options: tuple[str, ...] = ()
    statements: tuple[ChoiceStatement, ...] = ()
    columns: tuple[TableColumn, ...] = ()
    rows: tuple[TableRow, ...] = ()
    given: bool = False

    # ---------------------------------------------------------- field names
    def value_name(self, item_key: str) -> str:
        return f"{FIELD_PREFIX}{item_key}__{self.key}"

    def table_name(self, item_key: str, row_index: int, column_key: str) -> str:
        return f"{self.value_name(item_key)}__r{row_index}__{column_key}"

    def choice_name(self, item_key: str, statement_index: int) -> str:
        return f"{self.value_name(item_key)}__s{statement_index}"

    def options_for(self, statement: ChoiceStatement) -> tuple[str, ...]:
        return statement.options or self.options


@dataclass(frozen=True)
class Item:
    key: str
    number: int
    title: str
    intro: str
    parts: tuple[Part, ...]
    section: str = ""
    points: int = 0
    label_override: str = ""

    @property
    def label(self) -> str:
        return self.label_override or f"תרגיל {self.number}"


@dataclass(frozen=True)
class Worksheet:
    slug: str
    title: str
    note: str
    items: tuple[Item, ...]

    # ------------------------------------------------------------- grouping
    def sections(self) -> list[tuple[str, tuple[Item, ...]]]:
        """Items grouped by ``section``, in first-appearance order.

        A homework sheet has one unnamed group, so its rendering is unchanged.
        An exam paper gets one group per scored part (חלק א', ב', ...).
        """
        grouped: dict[str, list[Item]] = {}
        for item in self.items:
            grouped.setdefault(item.section, []).append(item)
        return [(name, tuple(items)) for name, items in grouped.items()]

    @property
    def total_points(self) -> int:
        return sum(item.points for item in self.items)

    # ------------------------------------------------------------- headings
    def questions(self) -> list[FormQuestion]:
        """One existing-style question per item, for compose/split.

        An item inside a named section is already numbered and labelled on the
        page ("שאלה 7"), so repeating "תרגיל 7" in the composed document would
        only add noise.
        """
        return [
            FormQuestion(
                key=item.key,
                number=item.number,
                id_label="" if item.section else item.label,
                title=item.title,
                prompt="",
                section="",
                weight=0,
                type="",
                code="",
                requirements=(),
                is_code=False,
                needs_stdin=False,
            )
            for item in self.items
        ]

    # ------------------------------------------------------------- collect
    def collect(self, form: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
        """Read the submitted fields into the structured payload shape."""
        values: dict[str, dict[str, Any]] = {}
        for item in self.items:
            item_values: dict[str, Any] = {}
            for part in item.parts:
                if part.given:
                    # Read-only question text: no field, nothing to store.
                    continue
                if part.kind in TEXT_LIMITS:
                    raw = form.get(part.value_name(item.key), "")
                    item_values[part.key] = (
                        _text(raw)[: TEXT_LIMITS[part.kind]].strip()
                    )
                elif part.kind == "table":
                    rows: list[dict[str, str]] = []
                    for row_index, row in enumerate(part.rows):
                        cells: dict[str, str] = {}
                        for column_index, column in enumerate(part.columns):
                            if column.readonly:
                                continue
                            raw = form.get(
                                part.table_name(
                                    item.key, row_index, column.key
                                ),
                                "",
                            )
                            cells[column.key] = (
                                _text(raw)[:MAX_CELL_CHARS].strip()
                            )
                        rows.append(cells)
                    item_values[part.key] = rows
                elif part.kind == "choice":
                    chosen: list[str] = []
                    for index, statement in enumerate(part.statements):
                        raw = _text(
                            form.get(
                                part.choice_name(item.key, index), ""
                            )
                        ).strip()
                        chosen.append(
                            raw if raw in part.options_for(statement) else ""
                        )
                    item_values[part.key] = chosen
            values[item.key] = item_values
        return values

    # ------------------------------------------------------------- compose
    def compose(self, values: Mapping[str, Mapping[str, Any]]) -> str:
        """Build the single document the grading engine receives."""
        answers = {
            item.key: self._render_item(item, values.get(item.key) or {})
            for item in self.items
        }
        return compose_answers(self.questions(), answers)

    def count_answered(self, values: Mapping[str, Mapping[str, Any]]) -> int:
        return sum(
            1
            for item in self.items
            if self._item_has_answer(item, values.get(item.key) or {})
        )

    def has_any(self, values: Mapping[str, Mapping[str, Any]]) -> bool:
        return self.count_answered(values) > 0

    # ------------------------------------------------------------- private
    def _render_item(self, item: Item, values: Mapping[str, Any]) -> str:
        chunks: list[str] = []
        multi = len(item.parts) > 1
        for part in item.parts:
            if part.given:
                # The snippet is part of the question. Copying it makes the
                # stored hand-in readable on its own; it is not an answer.
                code = part.code.strip()
                if code:
                    chunks.append(("(קטע הקוד) " if multi else "") + code)
                continue
            body = self._render_part(part, values.get(part.key))
            if not body:
                continue
            heading = ""
            if multi and part.label:
                heading = f"({part.label}) "
            if multi and part.prompt:
                heading += part.prompt.strip() + "\n"
            chunks.append(heading + body)
        return "\n\n".join(chunks).strip()

    def _render_part(self, part: Part, value: Any) -> str:
        if part.kind in TEXT_LIMITS:
            return _text(value).strip()
        if part.kind == "table":
            return self._render_table(part, value)
        if part.kind == "choice":
            return self._render_choice(part, value)
        return ""

    def _render_table(self, part: Part, value: Any) -> str:
        if not isinstance(value, list):
            return ""
        editable = [column for column in part.columns if not column.readonly]
        lines: list[str] = []
        for row_index, row in enumerate(part.rows):
            cells = value[row_index] if row_index < len(value) else {}
            if not isinstance(cells, dict):
                cells = {}
            answers = [
                f"{column.label}: {_text(cells.get(column.key)).strip()}"
                for column in editable
                if _text(cells.get(column.key)).strip()
            ]
            if not answers:
                continue
            given = " / ".join(
                _squash(row.cells[index])
                for index, column in enumerate(part.columns)
                if column.readonly
            )
            label = given or f"שורה {row_index + 1}"
            lines.append(f"{label}: {'; '.join(answers)}")
        return "\n".join(lines)

    def _render_choice(self, part: Part, value: Any) -> str:
        if not isinstance(value, list):
            return ""
        lines: list[str] = []
        for index, statement in enumerate(part.statements):
            chosen = _text(value[index] if index < len(value) else "").strip()
            if chosen:
                lines.append(f"{index + 1}. {statement.prompt} — {chosen}")
        return "\n".join(lines)

    def _item_has_answer(self, item: Item, values: Mapping[str, Any]) -> bool:
        for part in item.parts:
            if part.given:
                continue
            value = values.get(part.key)
            if part.kind in TEXT_LIMITS:
                if _text(value).strip():
                    return True
            elif part.kind == "table" and isinstance(value, list):
                if any(
                    _text(cell).strip()
                    for row in value
                    if isinstance(row, dict)
                    for cell in row.values()
                ):
                    return True
            elif part.kind == "choice" and isinstance(value, list):
                if any(_text(chosen).strip() for chosen in value):
                    return True
        return False


# ------------------------------------------------------------------ parsing
def _text(value: Any) -> str:
    if value is None or isinstance(value, (dict, list)):
        return ""
    return str(value)


def _squash(text: str) -> str:
    return " ".join(text.split())


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise WorksheetSpecError(message)


def _parse_part(raw: Any) -> Part:
    _require(isinstance(raw, dict), "a part must be an object")
    key = str(raw.get("key") or "")
    _require(bool(_KEY_RE.match(key)), f"invalid part key: {key!r}")
    kind = str(raw.get("kind") or "")
    _require(kind in KINDS, f"part {key}: unknown kind {kind!r}")
    given = bool(raw.get("given"))
    if given:
        _require(
            bool(str(raw.get("code") or "").strip()),
            f"part {key}: a given part needs code",
        )
        _require(
            kind in {"code", "code_plain"},
            f"part {key}: only a code part can be given",
        )

    columns_raw = raw.get("columns") or []
    rows_raw = raw.get("rows") or []
    statements_raw = raw.get("statements") or []
    options = tuple(str(option) for option in raw.get("options") or [])

    columns = tuple(
        TableColumn(
            key=str(column.get("key") or ""),
            label=str(column.get("label") or ""),
            readonly=bool(column.get("readonly")),
            code=bool(column.get("code")),
        )
        for column in columns_raw
        if isinstance(column, dict)
    )
    rows = tuple(
        TableRow(tuple(str(cell) for cell in row))
        for row in rows_raw
        if isinstance(row, list)
    )
    statements = tuple(
        ChoiceStatement(
            prompt=str(statement.get("prompt") or ""),
            options=tuple(
                str(option) for option in statement.get("options") or []
            ),
            code=str(statement.get("code") or ""),
        )
        for statement in statements_raw
        if isinstance(statement, dict)
    )

    if kind == "table":
        _require(len(columns) >= 1, f"part {key}: a table needs columns")
        _require(len(rows) >= 1, f"part {key}: a table needs rows")
        column_keys = [column.key for column in columns]
        _require(
            all(_KEY_RE.match(column.key) for column in columns),
            f"part {key}: invalid column key",
        )
        _require(
            len(set(column_keys)) == len(column_keys),
            f"part {key}: duplicate column key",
        )
        _require(
            any(not column.readonly for column in columns),
            f"part {key}: a table needs an editable column",
        )
        for row in rows:
            _require(
                len(row.cells) == len(columns),
                f"part {key}: every row needs {len(columns)} cells",
            )
    elif kind == "choice":
        _require(
            len(statements) >= 1, f"part {key}: a choice needs statements"
        )
        for statement in statements:
            effective = statement.options or options
            _require(
                len(effective) >= 2,
                f"part {key}: every statement needs at least two options",
            )

    return Part(
        key=key,
        kind=kind,
        label=str(raw.get("label") or ""),
        prompt=str(raw.get("prompt") or ""),
        code=str(raw.get("code") or ""),
        placeholder=str(raw.get("placeholder") or ""),
        options=options,
        statements=statements,
        columns=columns,
        rows=rows,
        given=given,
    )


def _parse_item(raw: Any, position: int, seen_keys: set[str]) -> Item:
    _require(isinstance(raw, dict), "an item must be an object")
    key = str(raw.get("key") or "")
    _require(bool(_KEY_RE.match(key)), f"invalid item key: {key!r}")
    _require(key not in seen_keys, f"duplicate item key: {key}")
    number = raw.get("number")
    _require(
        isinstance(number, int) and number > 0,
        f"item {key}: number must be a positive integer",
    )
    parts_raw = raw.get("parts") or []
    _require(isinstance(parts_raw, list) and parts_raw, f"item {key}: no parts")
    parts = tuple(_parse_part(part) for part in parts_raw)
    part_keys = [part.key for part in parts]
    _require(
        len(set(part_keys)) == len(part_keys),
        f"item {key}: duplicate part key",
    )
    seen_keys.add(key)
    points = raw.get("points") or 0
    _require(
        isinstance(points, int) and 0 <= points <= 1000,
        f"item {key}: points must be an integer between 0 and 1000",
    )
    return Item(
        key=key,
        number=int(number),
        title=str(raw.get("title") or "").strip(),
        intro=str(raw.get("intro") or "").strip(),
        parts=parts,
        section=str(raw.get("section") or "").strip(),
        points=int(points),
        label_override=str(raw.get("label") or "").strip(),
    )


def parse_worksheet(data: Any, *, slug: str) -> Worksheet:
    """Validate a decoded worksheet file (raises :class:`WorksheetSpecError`)."""
    _require(isinstance(data, dict), "the worksheet must be an object")
    _require(
        data.get("version") == 1, "unsupported worksheet version"
    )
    _require(
        str(data.get("rubric") or "") == slug,
        f"worksheet rubric must be {slug!r}",
    )
    items_raw = data.get("items")
    _require(
        isinstance(items_raw, list) and items_raw,
        "a worksheet needs at least one item",
    )
    seen: set[str] = set()
    items = tuple(
        _parse_item(raw, index, seen) for index, raw in enumerate(items_raw)
    )
    numbers = [item.number for item in items]
    _require(
        len(set(numbers)) == len(numbers), "duplicate exercise number"
    )
    return Worksheet(
        slug=slug,
        title=str(data.get("title") or "").strip(),
        note=str(data.get("note") or "").strip(),
        items=items,
    )


# ------------------------------------------------------------------- loading
def load_by_slug(slug: str) -> Worksheet | None:
    """Load ``<slug>.json`` next to this module; ``None`` when absent/invalid."""
    path = SPEC_DIR / f"{slug}.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return parse_worksheet(data, slug=slug)
    except (OSError, ValueError) as exc:
        # ValueError covers JSONDecodeError and WorksheetSpecError. A teacher
        # typo must not take the assignment page down; the old form is used.
        logger.warning("Inactive worksheet %s: %s", path.name, exc)
        return None


def for_rubric(rubric_path: str | Path | None) -> Worksheet | None:
    """The worksheet for an assignment's rubric file, if one is defined."""
    if not rubric_path:
        return None
    return load_by_slug(Path(str(rubric_path)).stem)


__all__ = [
    "FIELD_PREFIX",
    "KINDS",
    "TEXT_LIMITS",
    "ChoiceStatement",
    "Item",
    "Part",
    "TableColumn",
    "TableRow",
    "Worksheet",
    "WorksheetSpecError",
    "for_rubric",
    "load_by_slug",
    "parse_worksheet",
]

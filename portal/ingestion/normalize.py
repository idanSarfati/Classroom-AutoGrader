"""Title cleanup, content fingerprints and conservative duplicate detection.

Classroom content in this project contains many automation clones tagged
``[auto]`` (sometimes repeated). Two rules keep the import safe:

* The tag is removed from the **display title** (and from the dedupe key), so
  the portal does not inherit names such as ``"מטלה 3 [auto] [auto]"``.
* Items are skipped as duplicates only when they are genuinely the same
  content: same cleaned title and the same content fingerprint, or the same
  title, due date, work type and attachment set where one description is
  empty. Items with the same title but different real content are all kept -
  losing a differently-written assignment is worse than importing a duplicate.
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass

from portal.ingestion.types import CourseworkItem, DriveItem

# Automation markers left on copies: ``[auto]`` in Latin or Hebrew, and the
# ``[Template]`` label Drive puts in front of worksheets and clones.
_AUTO_TAG_RE = re.compile(
    r"[\[(]\s*(?:auto|אוטומטי|template|תבנית)\s*[\])]", re.IGNORECASE
)
# Separators left behind after removing a trailing tag.
_TRAILING_SEPARATOR_RE = re.compile(r"[\s\-–—|:;,]+$")
# Drive "עותק של X" / "עותק של עותק של X" copies; shown and matched as X.
_COPY_PREFIX_RE = re.compile(r"^\s*עותק\s+של\s+")
_WHITESPACE_RE = re.compile(r"\s+")
_TAG_RE = re.compile(r"<[^>]+>")
_NON_WORD_RE = re.compile(r"[^\w\u0590-\u05FF]+", re.UNICODE)
_NATURAL_TOKEN_RE = re.compile(r"(\d+)")
# A leading lesson number: "1 | ...", "10| ...", "2.3 - ...", "20 ...".
_TOPIC_NUMBER_RE = re.compile(r"^\s*(\d+)(?:\s*[|\-–—.:/]|\s|$)")
# "פייתון ב'" but not "פייתון באלגוריתמים": the letter after ב must not be a
# Hebrew letter. Drive folder names carry the marker even when the deck title
# does not ("2.1 | פעולות ותנאים | חזרה").
_PYTHON_B_RE = re.compile(r"פייתון\s*ב(?![\u05d0-\u05ea])")


# "פייתון א'" / "פייתון ב" inside an item title. The course prefix does not
# identify a material: the same deck or worksheet is titled with and without
# it across Classroom attachments and Drive copies.
_COURSE_MARKER_RE = re.compile(r"פייתון\s*[אב]['׳’]?")


def strip_control_chars(value: str) -> str:
    """Drop invisible bidi marks (Cf) Google puts inside Drive folder names."""
    text = "".join(
        ch for ch in str(value or "") if unicodedata.category(ch) != "Cf"
    )
    return _WHITESPACE_RE.sub(" ", text).strip()


def is_python_b_material(title: str = "", folder_name: str = "") -> bool:
    """Whether a Drive material belongs to the unused פייתון ב' course.

    The parent folder is the reliable signal: several ב' decks have plain
    titles such as "1.1 | היכרות", and the same title can exist in both
    courses. A title marker still catches decks cached before folder
    provenance was stored.
    """
    for value in (folder_name, title):
        if _PYTHON_B_RE.search(strip_control_chars(value)):
            return True
    return False


# Personal quarterly-exam results sheets ("אופק פרץ - תוצאות בוחן רבעוני").
# These are grade reports about a pupil, not teaching material, so they never
# belong in the course catalog or in the picker's "no topic" group.
_RESULTS_SHEET_RE = re.compile(r"תוצאות\s+בוחן\s+רבעוני")


def is_results_sheet(title: str = "") -> bool:
    """Whether a Drive document is a personal exam-results report.

    Such sheets list pupils' grades; they are not course content and must not
    appear among the teaching materials the teacher browses or imports.
    """
    return bool(_RESULTS_SHEET_RE.search(strip_control_chars(title)))


def natural_key(value: str) -> tuple:
    """Case-insensitive sort key where ``2.2`` comes before ``2.10``.

    Digits compare as numbers, so a class library ordered by this key reads
    1.1, 1.2, 1.10, 2.1 - not the lexicographic 1.1, 1.10, 1.2. Text chunks
    sort after numeric tokens, which keeps numbered coursework first and
    titles such as "תרגול יחידות 1-2" at the end of a topic.
    """
    text = _WHITESPACE_RE.sub(" ", str(value or "")).strip().casefold()
    return tuple(
        (0, int(part)) if part.isdigit() else (1, part)
        for part in _NATURAL_TOKEN_RE.split(text)
        if part
    )


def leading_number(value: str) -> int | None:
    """The curriculum number a topic or material title starts with.

    ``"3 | פקודת קלט"``, ``"3.0 _ פקודת קלט"`` and ``"20 | ..."`` yield 3, 3
    and 20. A title without a leading number (``"תרגול יחידות 1-2"``) yields
    ``None`` so it can be left where it is.
    """
    match = _TOPIC_NUMBER_RE.match(str(value or "").strip())
    return int(match.group(1)) if match else None


def topic_sort_key(name: str) -> tuple:
    """Curriculum order: numbered topics ascending, then the rest, then none.

    Classroom returns topics newest-first, which is not how a teacher reads a
    syllabus. A leading number in the topic name ("1 | מבוא...") is the
    primary key; topics without one follow alphabetically.
    """
    text = str(name or "").strip()
    if not text:
        return (2, 0, ())
    number = leading_number(text)
    if number is not None:
        return (0, number, natural_key(text))
    return (1, 0, natural_key(text))


def strip_auto_tags(title: str) -> str:
    """Remove every automation marker: ``[auto]``, ``[אוטומטי]``, ``[Template]``.

    Drive stacks these in front of clones ("עותק של [Template] 11.3 | ..."),
    so removing them anywhere in the title leaves the real lesson name and
    lets the leading number assign the item to its topic.
    """
    cleaned = _AUTO_TAG_RE.sub(" ", title or "")
    return _TRAILING_SEPARATOR_RE.sub("", _WHITESPACE_RE.sub(" ", cleaned)).strip()


def strip_copy_prefix(title: str) -> str:
    """Drop Drive's "עותק של" copy prefix, including nested copies."""
    text = str(title or "")
    while True:
        stripped = _COPY_PREFIX_RE.sub("", text, count=1)
        if stripped == text:
            return text
        text = stripped


def clean_title(title: str, *, fallback: str = "ללא כותרת") -> str:
    """Display title: copy/auto tags removed, whitespace collapsed, 160 chars."""
    cleaned = strip_auto_tags(strip_copy_prefix(title))
    if not cleaned:
        return fallback
    return cleaned[:160].strip()


def title_key(title: str) -> str:
    """Normalized identity used only for duplicate detection."""
    text = unicodedata.normalize("NFKD", clean_title(title, fallback=""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch)).casefold()
    return _WHITESPACE_RE.sub(" ", _NON_WORD_RE.sub(" ", text)).strip()


def material_key(title: str) -> str:
    """Identity shared by a Classroom item and its Drive copy.

    The same worksheet or deck is often listed once as Classroom coursework
    and once as the Drive file it attaches, with titles that differ only by
    automation tags, copy prefixes or the course marker ("פייתון א'"). Those
    do not identify the material, so they are ignored here. Only the picker
    uses this key; importing still follows the explicit source id.
    """
    cleaned = _COURSE_MARKER_RE.sub(" ", clean_title(title, fallback=""))
    return title_key(cleaned)


def html_to_text(value: str) -> str:
    """Best-effort plain text for previews and fallback storage."""
    import html as html_module

    text = _TAG_RE.sub(" ", value or "")
    text = html_module.unescape(text)
    return _WHITESPACE_RE.sub(" ", text).strip()


def content_fingerprint(item: CourseworkItem) -> str:
    """SHA-256 over the fields that define a coursework's content.

    The cleaned title is used so ``[auto]`` clones collapse, but the raw
    description is normalized to text first: markup-only differences do not
    create a false "different content" result.
    """
    file_ids = "|".join(sorted(a.file_id for a in item.attachments if a.file_id))
    due = item.due_at.isoformat() if item.due_at else ""
    payload = "\n".join(
        (
            title_key(item.title),
            html_to_text(item.description_html),
            file_ids,
            due,
            item.work_type or "",
        )
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class DedupeInfo:
    duplicate_of: str = ""
    duplicate_count: int = 0


def _score(item: CourseworkItem) -> tuple[int, int, int]:
    """Pick the richest copy of a duplicate group."""
    return (
        1 if (item.state or "").upper() == "PUBLISHED" else 0,
        len(html_to_text(item.description_html)),
        len(item.attachments),
    )


def dedupe_coursework(
    items: list[CourseworkItem],
) -> tuple[list[CourseworkItem], dict[str, DedupeInfo]]:
    """Return the items to keep and a ``source id -> dedupe info`` map.

    The canonical item keeps a ``duplicate_count`` so the preview can say
    "3 copies collapsed". Duplicates are returned in the map but not in the
    keep list; the caller shows them as skipped/duplicate rows.
    """
    groups: dict[str, list[CourseworkItem]] = {}
    order: dict[str, int] = {}
    for index, item in enumerate(items):
        key = title_key(item.title)
        if not key:
            key = f"__untitled__{item.id}"
        groups.setdefault(key, []).append(item)
        order.setdefault(key, index)

    keep: list[CourseworkItem] = []
    info: dict[str, DedupeInfo] = {}
    for key, group in groups.items():
        if len(group) == 1:
            keep.append(group[0])
            info[group[0].id] = DedupeInfo()
            continue

        # Fingerprint first: identical content is always a duplicate.
        by_fingerprint: dict[str, list[CourseworkItem]] = {}
        for item in group:
            by_fingerprint.setdefault(content_fingerprint(item), []).append(item)

        for fingerprint, same_content in by_fingerprint.items():
            if len(same_content) == 1:
                continue
            canonical = max(same_content, key=_score)
            for item in same_content:
                if item is canonical:
                    continue
                info[item.id] = DedupeInfo(
                    duplicate_of=canonical.id, duplicate_count=0
                )

        # Same title, same attachment set, same due/work type: if a copy has
        # no description at all while another has one, the empty copy is a
        # broken clone, not a different assignment.
        remaining = [
            item for item in group if item.id not in info
        ]
        by_shape: dict[tuple, list[CourseworkItem]] = {}
        for item in remaining:
            shape = (
                tuple(sorted(a.file_id for a in item.attachments if a.file_id)),
                item.due_at.isoformat() if item.due_at else "",
                item.work_type or "",
            )
            by_shape.setdefault(shape, []).append(item)
        for shape, same_shape in by_shape.items():
            if len(same_shape) < 2:
                continue
            non_empty = [
                item
                for item in same_shape
                if html_to_text(item.description_html)
            ]
            if len(non_empty) == len(same_shape):
                # Every copy has content, but fingerprints differed. Keep all.
                continue
            canonical = max(same_shape, key=_score)
            for item in same_shape:
                if item is canonical:
                    continue
                info[item.id] = DedupeInfo(
                    duplicate_of=canonical.id, duplicate_count=0
                )

        # Choose the canonical for each shape/fingerprint group and count.
        for item in group:
            if item.id in info:
                continue
            keep.append(item)
            info[item.id] = DedupeInfo()

        counts: dict[str, int] = {}
        for duplicate in group:
            marker = info.get(duplicate.id)
            if marker and marker.duplicate_of:
                counts[marker.duplicate_of] = counts.get(marker.duplicate_of, 0) + 1
        for canonical_id, count in counts.items():
            current = info.get(canonical_id, DedupeInfo())
            info[canonical_id] = DedupeInfo(
                duplicate_of=current.duplicate_of, duplicate_count=count
            )

    keep.sort(key=lambda item: order.get(title_key(item.title) or f"__untitled__{item.id}", 0))
    return keep, info


def dedupe_drive_items(
    items: list[DriveItem],
) -> tuple[list[DriveItem], dict[str, DedupeInfo]]:
    """De-duplicate directly selected Drive decks by id, then by title."""
    info: dict[str, DedupeInfo] = {}
    seen_ids: dict[str, DriveItem] = {}
    for item in items:
        seen_ids.setdefault(item.file_id, item)
    unique = list(seen_ids.values())

    by_title: dict[str, list[DriveItem]] = {}
    for item in unique:
        by_title.setdefault(title_key(item.title), []).append(item)
    keep: list[DriveItem] = []
    for group in by_title.values():
        canonical = max(group, key=lambda item: len(item.title))
        keep.append(canonical)
        info[canonical.file_id] = DedupeInfo(duplicate_count=len(group) - 1)
        for item in group:
            if item is not canonical:
                info[item.file_id] = DedupeInfo(duplicate_of=canonical.file_id)
    keep.sort(key=lambda item: title_key(item.title))
    return keep, info

"""Render the mapping report as an RTL Excel workbook.

The workbook is written directly as OOXML from the exact JSON produced by
``build_mapping_report``. Bulk text is never retyped, so Hebrew names and
national ids reach the file byte-for-byte.

Usage::

    python -m portal.scripts.write_mapping_workbook
"""
from __future__ import annotations

import argparse
import json
import sys
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

DATA_DIR = Path(__file__).resolve().parents[1] / "data"
DEFAULT_GRIDS = DATA_DIR / "student_mapping_report_grids.json"
DEFAULT_OUT = DATA_DIR / "student_mapping_report.xlsx"

# Cell style indexes into styles.xml.
S_TITLE = 1
S_SUBTITLE = 2
S_SECTION = 3
S_HEADER = 4
S_HEADER2 = 5
S_TEXT = 6
S_TEXT_BOLD = 7
S_NUMBER = 8
S_PERCENT = 9
S_TOTAL_TEXT = 10
S_TOTAL_NUMBER = 11
S_TOTAL_PERCENT = 12
S_LEGEND_LABEL = 13
S_LEGEND_DESC = 14
S_NOTE_INDEX = 15
S_NOTE_TEXT = 16
S_GREEN = 17
S_AMBER = 18
S_ORANGE = 19
S_RED = 20
S_GRAY = 21
S_NUMBER_BOLD = 22
S_CENTER_BOLD = 23
S_GREEN_BOLD = 24

STATUS_STYLES = {
    "מיובא": S_GREEN,
    "לבדיקה": S_AMBER,
    "התאמה חלשה": S_ORANGE,
    "לא נמצא ב-Classroom": S_RED,
    "שורת כפילות": S_GRAY,
    "שורה ללא התאמה": S_RED,
    "ודאות גבוהה": S_GREEN,
    "ודאות גבוהה (מאושר)": S_GREEN,
    "ודאות בינונית": S_AMBER,
    "ודאות נמוכה": S_ORANGE,
    "ללא התאמה": S_RED,
}


def _styles_xml() -> str:
    bordered = (
        '<left style="thin"><color rgb="FFBFBFBF"/></left>'
        '<right style="thin"><color rgb="FFBFBFBF"/></right>'
        '<top style="thin"><color rgb="FFBFBFBF"/></top>'
        '<bottom style="thin"><color rgb="FFBFBFBF"/></bottom>'
    )
    fills = [
        ("none", None),
        ("gray125", None),
        ("solid", "FF1F3864"),
        ("solid", "FFD6E4F0"),
        ("solid", "FFBDD7EE"),
        ("solid", "FFD9E2F3"),
        ("solid", "FFE2EFDA"),
        ("solid", "FFFFF2CC"),
        ("solid", "FFFCE4D6"),
        ("solid", "FFF8CBAD"),
        ("solid", "FFEDEDED"),
    ]
    fill_xml = []
    for pattern, color in fills:
        if color:
            fill_xml.append(
                f'<fill><patternFill patternType="{pattern}">'
                f'<fgColor rgb="{color}"/><bgColor indexed="64"/>'
                "</patternFill></fill>"
            )
        else:
            fill_xml.append(f'<fill><patternFill patternType="{pattern}"/></fill>')

    def xf(
        font=0,
        fill=0,
        border=0,
        numfmt=0,
        horizontal=None,
        vertical=None,
        wrap=False,
        bold_fill=False,
    ) -> str:
        attrs = [
            f'numFmtId="{numfmt}"',
            f'fontId="{font}"',
            f'fillId="{fill}"',
            f'borderId="{border}"',
            'xfId="0"',
        ]
        if numfmt:
            attrs.append('applyNumberFormat="1"')
        if font:
            attrs.append('applyFont="1"')
        if fill:
            attrs.append('applyFill="1"')
        if border:
            attrs.append('applyBorder="1"')
        align = []
        if horizontal:
            align.append(f'horizontal="{horizontal}"')
        if vertical:
            align.append(f'vertical="{vertical}"')
        if wrap:
            align.append('wrapText="1"')
        if align:
            attrs.append('applyAlignment="1"')
            return f'<xf {" ".join(attrs)}><alignment {" ".join(align)}/></xf>'
        return f'<xf {" ".join(attrs)}/>'

    cell_xfs = [
        xf(),  # 0 default
        xf(font=2, fill=2, horizontal="center", vertical="center"),  # 1 title
        xf(font=3, horizontal="right"),  # 2 subtitle
        xf(font=4, fill=3, horizontal="right"),  # 3 section
        xf(font=5, fill=2, border=1, horizontal="center", vertical="center", wrap=True),  # 4
        xf(font=1, fill=4, border=1, horizontal="center"),  # 5 header2
        xf(border=1, horizontal="right", vertical="top", wrap=True),  # 6 text
        xf(font=1, border=1, horizontal="right", vertical="top", wrap=True),  # 7 bold text
        xf(border=1, numfmt=165, horizontal="center", vertical="center"),  # 8 number
        xf(border=1, numfmt=164, horizontal="center", vertical="center"),  # 9 percent
        xf(font=1, fill=5, border=1, horizontal="right"),  # 10 total text
        xf(font=1, fill=5, border=1, numfmt=165, horizontal="center"),  # 11 total number
        xf(font=1, fill=5, border=1, numfmt=164, horizontal="center"),  # 12 total percent
        xf(font=1, border=1, horizontal="right", vertical="center"),  # 13 legend label
        xf(border=1, horizontal="right", vertical="top", wrap=True),  # 14 legend desc
        xf(font=1, horizontal="center"),  # 15 note index
        xf(horizontal="right", vertical="top", wrap=True),  # 16 note text
        xf(fill=6, border=1, horizontal="center", vertical="center"),  # 17 green
        xf(fill=7, border=1, horizontal="center", vertical="center"),  # 18 amber
        xf(fill=8, border=1, horizontal="center", vertical="center"),  # 19 orange
        xf(fill=9, border=1, horizontal="center", vertical="center"),  # 20 red
        xf(fill=10, border=1, horizontal="center", vertical="center"),  # 21 gray
        xf(font=1, border=1, numfmt=165, horizontal="center"),  # 22 bold number
        xf(font=1, border=1, horizontal="center", vertical="center"),  # 23 bold center
        xf(font=1, fill=6, border=1, horizontal="center", vertical="center"),  # 24 green bold
    ]

    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<numFmts count="2">'
        '<numFmt numFmtId="164" formatCode="0.0%"/>'
        '<numFmt numFmtId="165" formatCode="#,##0"/>'
        "</numFmts>"
        '<fonts count="7">'
        '<font><sz val="11"/><name val="Calibri"/></font>'
        '<font><b/><sz val="11"/><name val="Calibri"/></font>'
        '<font><b/><sz val="16"/><color rgb="FFFFFFFF"/><name val="Calibri"/></font>'
        '<font><i/><sz val="10"/><color rgb="FF595959"/><name val="Calibri"/></font>'
        '<font><b/><sz val="13"/><color rgb="FF1F3864"/><name val="Calibri"/></font>'
        '<font><b/><sz val="11"/><color rgb="FFFFFFFF"/><name val="Calibri"/></font>'
        '<font><b/><sz val="11"/><color rgb="FF375623"/><name val="Calibri"/></font>'
        "</fonts>"
        f'<fills count="{len(fill_xml)}">' + "".join(fill_xml) + "</fills>"
        '<borders count="2">'
        "<border><left/><right/><top/><bottom/><diagonal/></border>"
        f"<border>{bordered}<diagonal/></border>"
        "</borders>"
        '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
        f'<cellXfs count="{len(cell_xfs)}">' + "".join(cell_xfs) + "</cellXfs>"
        '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
        "</styleSheet>"
    )


def _cell(ref: str, value, style: int | None = None) -> str:
    if value is None or value == "":
        return ""
    style_attr = f' s="{style}"' if style is not None else ""
    if isinstance(value, bool):
        value = int(value)
    if isinstance(value, (int, float)):
        return f'<c r="{ref}"{style_attr}><v>{value}</v></c>'
    text = escape(str(value))
    return f'<c r="{ref}"{style_attr} t="inlineStr"><is><t>{text}</t></is></c>'


def _col(index: int) -> str:
    letters = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def _sheet_xml(
    rows: list[list[object]],
    styles: list[list[int | None]],
    widths: list[int],
    merges: list[str] | None = None,
    freeze: tuple[int, int] | None = None,
    autofilter: str | None = None,
    selected: bool = False,
    heights: dict[int, float] | None = None,
) -> str:
    heights = heights or {}
    max_cols = max((len(row) for row in rows), default=1)
    dimension = f"A1:{_col(max_cols)}{max(len(rows), 1)}"
    view_attrs = ['rightToLeft="1"', 'workbookViewId="0"']
    if selected:
        view_attrs.insert(0, 'tabSelected="1"')
    pane = ""
    if freeze:
        x_split, y_split = freeze
        top_left = f"{_col(x_split + 1)}{y_split + 1}"
        if x_split and y_split:
            active_pane = "bottomRight"
        elif y_split:
            active_pane = "bottomLeft"
        else:
            active_pane = "topRight"
        pane = (
            f'<pane xSplit="{x_split}" ySplit="{y_split}" '
            f'topLeftCell="{top_left}" activePane="{active_pane}" state="frozen"/>'
        )
    parts = [
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">',
        f'<dimension ref="{dimension}"/>',
        f"<sheetViews><sheetView {' '.join(view_attrs)}>{pane}</sheetView></sheetViews>",
        '<sheetFormatPr defaultRowHeight="15"/>',
        "<cols>",
    ]
    for index, width in enumerate(widths, start=1):
        parts.append(
            f'<col min="{index}" max="{index}" width="{width}" customWidth="1"/>'
        )
    parts.append("</cols><sheetData>")
    for row_index, row in enumerate(rows, start=1):
        height = heights.get(row_index)
        height_attr = (
            f' ht="{height}" customHeight="1"' if height is not None else ""
        )
        cells = []
        for column_index, value in enumerate(row, start=1):
            style = None
            if row_index - 1 < len(styles) and column_index - 1 < len(styles[row_index - 1]):
                style = styles[row_index - 1][column_index - 1]
            cells.append(_cell(f"{_col(column_index)}{row_index}", value, style))
        parts.append(f'<row r="{row_index}"{height_attr}>' + "".join(cells) + "</row>")
    parts.append("</sheetData>")
    if autofilter:
        parts.append(f'<autoFilter ref="{autofilter}"/>')
    if merges:
        parts.append(f'<mergeCells count="{len(merges)}">')
        for ref in merges:
            parts.append(f'<mergeCell ref="{ref}"/>')
        parts.append("</mergeCells>")
    parts.append(
        '<pageMargins left="0.3" right="0.3" top="0.5" bottom="0.5" header="0.3" footer="0.3"/>'
    )
    parts.append("</worksheet>")
    return "".join(parts)


def _summary_styles(row_count: int) -> list[list[int | None]]:
    styles: list[list[int | None]] = [[None] * 11 for _ in range(row_count)]
    if row_count >= 1:
        styles[0][0] = S_TITLE
    if row_count >= 2:
        styles[1][0] = S_SUBTITLE
    if row_count >= 4:
        styles[3][0] = S_SECTION
    for column in range(3):
        styles[4][column] = S_HEADER2
    for row in range(5, 15):
        styles[row][0] = S_TEXT_BOLD
        styles[row][1] = S_NUMBER_BOLD
        styles[row][2] = S_TEXT
    styles[16][0] = S_SECTION
    for column in range(11):
        styles[17][column] = S_HEADER
    for row in range(18, 26):
        styles[row][0] = S_TEXT_BOLD
        for column in range(1, 10):
            styles[row][column] = S_NUMBER
        styles[row][10] = S_PERCENT
    for column in range(11):
        styles[26][column] = S_TOTAL_NUMBER
    styles[26][0] = S_TOTAL_TEXT
    styles[26][10] = S_TOTAL_PERCENT
    styles[28][0] = S_SECTION
    for row in range(29, 34):
        styles[row][0] = S_LEGEND_LABEL
        styles[row][1] = S_LEGEND_DESC
    styles[35][0] = S_SECTION
    for row in range(36, row_count):
        styles[row][0] = S_NOTE_INDEX
        styles[row][1] = S_NOTE_TEXT
    return styles


def _table_styles(
    rows: list[list[object]],
    status_column: int,
    imported_column: int | None = None,
) -> list[list[int | None]]:
    styles: list[list[int | None]] = []
    for row_index, row in enumerate(rows):
        row_styles: list[int | None] = []
        for column_index, value in enumerate(row):
            if row_index == 0:
                row_styles.append(S_HEADER)
            elif column_index == status_column:
                row_styles.append(STATUS_STYLES.get(str(value), S_CENTER_BOLD))
            elif imported_column is not None and column_index == imported_column:
                row_styles.append(
                    S_GREEN_BOLD if str(value) == "כן" else S_GRAY
                )
            elif isinstance(value, (int, float)) and not isinstance(value, bool):
                row_styles.append(S_NUMBER)
            else:
                row_styles.append(S_TEXT)
        styles.append(row_styles)
    return styles


def build(grids_path: Path, out_path: Path) -> None:
    grids = json.loads(grids_path.read_text(encoding="utf-8"))
    summary = grids["summary"]
    students = grids["students"]
    matches = grids["matches"]
    issues = grids["issues"]

    summary_styles = _summary_styles(len(summary))
    students_styles = _table_styles(students, status_column=3)
    matches_styles = _table_styles(matches, status_column=6, imported_column=7)
    issues_styles = _table_styles(issues, status_column=0)

    summary_merges = [
        "A1:K1",
        "A2:K2",
        "A4:K4",
        "A17:K17",
        "A29:K29",
        "A36:K36",
    ] + [
        f"B{row}:K{row}"
        for row in list(range(30, 35)) + list(range(37, len(summary) + 1))
    ]

    sheets = [
        (
            "סיכום",
            summary,
            summary_styles,
            [28, 16, 55, 20, 12, 12, 16, 14, 15, 16, 10],
            summary_merges,
            None,
            "A18:K26",
            {
                1: 32,
                18: 34,
                **{row: 30 for row in range(37, len(summary) + 1)},
            },
        ),
        (
            "תלמידים",
            students,
            students_styles,
            [10, 14, 26, 20, 30, 24, 12, 12],
            None,
            (0, 1),
            f"A1:H{len(students)}",
            {1: 30},
        ),
        (
            "התאמות",
            matches,
            matches_styles,
            [10, 30, 8, 30, 14, 26, 20, 16],
            None,
            (0, 1),
            f"A1:H{len(matches)}",
            {1: 30},
        ),
        (
            "בעיות",
            issues,
            issues_styles,
            [22, 10, 28, 14, 32, 20, 70],
            None,
            (0, 1),
            f"A1:G{len(issues)}",
            {1: 30},
        ),
    ]

    sheet_xml = [
        _sheet_xml(
            rows,
            styles,
            widths,
            merges,
            freeze,
            autofilter,
            selected=index == 0,
            heights=heights,
        )
        for index, (
            _,
            rows,
            styles,
            widths,
            merges,
            freeze,
            autofilter,
            heights,
        ) in enumerate(sheets)
    ]

    sheets_xml = "".join(
        f'<sheet name="{escape(name)}" sheetId="{index}" r:id="rId{index}"/>'
        for index, (name, *_rest) in enumerate(sheets, start=1)
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        + "".join(
            f'<Override PartName="/xl/worksheets/sheet{index}.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            for index in range(1, len(sheets) + 1)
        )
        + '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
        "</Types>"
    )
    root_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
        "</Relationships>"
    )
    workbook = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<bookViews><workbookView activeTab="0"/></bookViews>'
        f"<sheets>{sheets_xml}</sheets>"
        "</workbook>"
    )
    workbook_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        + "".join(
            f'<Relationship Id="rId{index}" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
            f'Target="worksheets/sheet{index}.xml"/>'
            for index in range(1, len(sheets) + 1)
        )
        + f'<Relationship Id="rId{len(sheets) + 1}" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" '
        'Target="styles.xml"/>'
        "</Relationships>"
    )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", root_rels)
        archive.writestr("xl/workbook.xml", workbook)
        archive.writestr("xl/_rels/workbook.xml.rels", workbook_rels)
        archive.writestr("xl/styles.xml", _styles_xml())
        for index, xml in enumerate(sheet_xml, start=1):
            archive.writestr(f"xl/worksheets/sheet{index}.xml", xml)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--grids", type=Path, default=DEFAULT_GRIDS)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args(argv)
    try:
        build(args.grids, args.out)
    except (OSError, ValueError, KeyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"נכתב {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

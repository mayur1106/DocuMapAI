from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime

import fitz


PAGE_LABEL_RE = re.compile(
    r"^(?:TOC-\d+|LEP-\d+|[A-Z]{2,6}-\d+[A-Z]?|\d{1,3}-\d+[A-Z]?|\d{2}(?:-\d{2}){1,6}[A-Z]?)$",
    re.IGNORECASE,
)
REV_WORD_RE = re.compile(r"^REV(?:[-\s:]?.*)?$", re.IGNORECASE)
REVISION_TOKEN_RE = re.compile(r"^(?:\d{1,2}[A-Z]?|R\d+[A-Z]?|[A-Z]\d+)$", re.IGNORECASE)
DATE_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")
HEADER_REV_RE = re.compile(r"^REV[-\s:]*(?P<revision>\d{1,3}[A-Z]?)?$", re.IGNORECASE)
MONTH_WORDS = {
    "JAN",
    "FEB",
    "MAR",
    "APR",
    "MAY",
    "JUN",
    "JUL",
    "AUG",
    "SEP",
    "OCT",
    "NOV",
    "DEC",
}


@dataclass(frozen=True)
class RevisionPageChange:
    page_index: int
    page_label: str
    change_type: str


@dataclass
class RevisionUpdateResult:
    changed_page_count: int = 0
    header_updates: int = 0
    lep_rows_updated: int = 0
    lep_rows_added: int = 0
    lep_rows_unplaced: list[str] = field(default_factory=list)
    lep_pages_modified: list[int] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "changed_page_count": self.changed_page_count,
            "header_updates": self.header_updates,
            "lep_rows_updated": self.lep_rows_updated,
            "lep_rows_added": self.lep_rows_added,
            "lep_rows_unplaced": self.lep_rows_unplaced,
            "lep_pages_modified": self.lep_pages_modified,
            "warnings": self.warnings,
        }


@dataclass(frozen=True)
class _LepEntry:
    label: str
    page_index: int
    label_rect: fitz.Rect
    date_rect: fitz.Rect
    rev_rect: fitz.Rect
    revision_value: str
    y0: float


@dataclass(frozen=True)
class _LepOutputRow:
    label: str
    date_value: str
    revision_value: str
    is_new_toc_row: bool = False


@dataclass(frozen=True)
class _LepChapterInsertionResult:
    added: int
    modified_pages: list[int]
    handled_labels: set[str]
    overflow_rows: list[_LepOutputRow] = field(default_factory=list)


@dataclass(frozen=True)
class _RevisionStamp:
    revision_value: str
    date_value: str


@dataclass(frozen=True)
class _ResolvedRevisionChange:
    change: RevisionPageChange
    stamp: _RevisionStamp


@dataclass(frozen=True)
class _HeaderCells:
    issue_rect: fitz.Rect | None
    rev_rect: fitz.Rect
    date_rect: fitz.Rect
    font_size: float
    draw_borders: bool = False


def apply_revision_updates(
    document: fitz.Document,
    changes: list[RevisionPageChange],
    *,
    revision: str | None = None,
    revision_date: str | None = None,
) -> RevisionUpdateResult:
    """Apply revision/date markings for visible TOC page changes and LEP rows.

    This intentionally avoids broad page rewrites. It updates detected revision
    text in page headers and only changes LEP rows that can be matched by page
    label. Missing LEP rows are added to an intentionally blank LEP page when
    one exists.
    """

    result = RevisionUpdateResult(changed_page_count=len(changes))
    unique_changes = _dedupe_changes(changes)
    if not unique_changes:
        return result

    date_value = _normalize_revision_date(revision_date) if revision_date else _current_revision_date()
    revision_override = _normalize_revision_value(revision) if revision else None
    lep_pages = _find_lep_pages(document)
    lep_entries = _parse_lep_entries(document, lep_pages)
    chapter_revisions = _chapter_revision_index(lep_entries)
    resolved_changes: list[_ResolvedRevisionChange] = []

    for change in unique_changes:
        if change.page_index < 0 or change.page_index >= len(document):
            result.warnings.append(f"Skipped revision header for out-of-range page label {change.page_label}.")
            continue

        page = document[change.page_index]
        revision_value = revision_override or _next_revision_value(
            page,
            fallback_current_revision=chapter_revisions.get(_chapter_from_label(change.page_label) or ""),
        )
        stamp = _RevisionStamp(revision_value=revision_value, date_value=date_value)
        resolved_changes.append(_ResolvedRevisionChange(change=change, stamp=stamp))

        if _write_revision_header(page, stamp):
            result.header_updates += 1
        else:
            result.warnings.append(
                f"No revision header was detected on page label {change.page_label}; fallback text was inserted."
            )
            result.header_updates += 1

    _apply_lep_updates(document, resolved_changes, result, lep_pages=lep_pages, lep_entries=lep_entries)
    return result


def _dedupe_changes(changes: list[RevisionPageChange]) -> list[RevisionPageChange]:
    seen: set[tuple[int, str]] = set()
    unique: list[RevisionPageChange] = []
    for change in changes:
        key = (change.page_index, _normalize_label(change.page_label))
        if key in seen:
            continue
        seen.add(key)
        unique.append(change)
    return unique


def _write_revision_header(page: fitz.Page, stamp: _RevisionStamp) -> bool:
    cells = _detect_revision_header_cells(page)
    if cells is None:
        page.insert_text(
            (max(36.0, page.rect.width - 180.0), min(92.0, page.rect.height * 0.15)),
            f"Rev-{stamp.revision_value} {stamp.date_value}",
            fontsize=9.0,
            fontname="helv",
            color=(0, 0, 0),
        )
        return False

    _overwrite_rect_text(
        page,
        cells.rev_rect,
        f"Rev-{stamp.revision_value}",
        fontsize=cells.font_size,
        align=fitz.TEXT_ALIGN_CENTER,
    )
    _overwrite_rect_text(
        page,
        cells.date_rect,
        stamp.date_value,
        fontsize=cells.font_size,
        align=fitz.TEXT_ALIGN_CENTER,
    )
    if cells.draw_borders:
        _draw_cell_borders(page, [cells.issue_rect, cells.rev_rect, cells.date_rect])
    return True


def _detect_revision_header_cells(page: fitz.Page) -> _HeaderCells | None:
    words = [word for word in page.get_text("words") if float(word[1]) <= min(145.0, page.rect.height * 0.2)]
    sorted_words = sorted(words, key=lambda item: (float(item[1]), float(item[0])))
    rev_word = next((word for word in sorted_words if REV_WORD_RE.match(str(word[4]).strip())), None)
    if rev_word is None:
        return None

    line_words = sorted(_same_visual_line(words, rev_word), key=lambda item: float(item[0]))
    if not line_words:
        return None

    rev_index = line_words.index(rev_word)
    rev_words = [rev_word]
    after_rev_index = rev_index + 1
    rev_text = str(rev_word[4]).strip()
    if HEADER_REV_RE.match(rev_text) and not HEADER_REV_RE.match(rev_text).group("revision"):
        if after_rev_index < len(line_words):
            next_word_text = str(line_words[after_rev_index][4]).strip()
            if re.fullmatch(r"\d{1,3}[A-Z]?", next_word_text, re.IGNORECASE):
                rev_words.append(line_words[after_rev_index])
                after_rev_index += 1

    date_words = _date_words_after(line_words, after_rev_index)
    grid_cells = _header_cells_from_drawings(page, line_words, rev_words, date_words)
    if grid_cells is not None:
        return grid_cells

    line_rect = _union_rect(line_words)
    rev_rect = _union_rect(rev_words) + (-1.5, -1.5, 2.5, 2.5)
    if date_words:
        date_rect = _union_rect(date_words) + (-1.5, -1.5, 2.5, 2.5)
        if rev_rect.width < 34.0:
            rev_rect.x1 = min(date_rect.x0 - 2.0, rev_rect.x0 + 36.0)
    else:
        rev_rect.x1 = min(page.rect.width - 88.0, max(rev_rect.x1, rev_rect.x0 + 38.0))
        date_rect = fitz.Rect(
            min(page.rect.width - 84.0, rev_rect.x1 + 4.0),
            rev_rect.y0,
            min(page.rect.width - 8.0, rev_rect.x1 + 78.0),
            rev_rect.y1,
        )

    date_rect.y0 = min(date_rect.y0, line_rect.y0 - 1.5)
    date_rect.y1 = max(date_rect.y1, line_rect.y1 + 2.5)
    rev_rect.y0 = min(rev_rect.y0, line_rect.y0 - 1.5)
    rev_rect.y1 = max(rev_rect.y1, line_rect.y1 + 2.5)
    font_size = max(7.0, min(9.5, line_rect.height * 0.68))
    return _HeaderCells(issue_rect=None, rev_rect=rev_rect, date_rect=date_rect, font_size=font_size)


def _header_cells_from_drawings(
    page: fitz.Page,
    line_words: list[tuple],
    rev_words: list[tuple],
    date_words: list[tuple],
) -> _HeaderCells | None:
    rev_text_rect = _union_rect(rev_words)
    date_text_rect = _union_rect(date_words) if date_words else None
    issue_words = _issue_words_before_revision(line_words, rev_words[0])
    issue_text_rect = _union_rect(issue_words) if issue_words else None
    focus_rect = rev_text_rect
    if date_text_rect is not None:
        focus_rect |= date_text_rect
    if issue_text_rect is not None:
        focus_rect |= issue_text_rect

    verticals, horizontals = _header_drawing_lines(page)
    if not verticals:
        return None

    rev_center_y = (rev_text_rect.y0 + rev_text_rect.y1) / 2.0
    row_verticals = [
        rect
        for rect in verticals
        if rect.y0 - 5.0 <= rev_center_y <= rect.y1 + 5.0
    ]
    overlap_verticals = [
        rect
        for rect in row_verticals
        if _vertical_overlap(rect, rev_text_rect) >= min(4.5, rev_text_rect.height * 0.35)
    ]
    if len(overlap_verticals) >= 3:
        row_verticals = overlap_verticals
    if row_verticals:
        max_row_height = max(rect.height for rect in row_verticals)
        row_verticals = [
            rect
            for rect in row_verticals
            if rect.height >= max(8.0, max_row_height * 0.6)
        ]
    if len(row_verticals) < 3:
        return None

    y_top, y_bottom = _revision_row_bounds(row_verticals, horizontals, rev_text_rect)
    if y_bottom - y_top < 8.0 or y_bottom - y_top > 32.0:
        y_top = focus_rect.y0 - 2.0
        y_bottom = focus_rect.y1 + 3.0

    x_focus_left = focus_rect.x0 - 55.0
    x_focus_right = focus_rect.x1 + 90.0
    x_positions = _merge_positions(
        [
            (rect.x0 + rect.x1) / 2.0
            for rect in row_verticals
            if x_focus_left <= (rect.x0 + rect.x1) / 2.0 <= x_focus_right
        ],
        tolerance=2.2,
    )
    if len(x_positions) < 3:
        return None

    rev_rect = _cell_rect_around_text(rev_text_rect, x_positions, y_top, y_bottom)
    if rev_rect is None:
        return None

    if date_text_rect is not None:
        date_rect = _cell_rect_around_text(date_text_rect, x_positions, y_top, y_bottom)
    else:
        date_rect = _next_cell_after(rev_rect, x_positions, y_top, y_bottom)
    if date_rect is None:
        return None

    issue_rect = None
    if issue_text_rect is not None:
        issue_rect = _cell_rect_around_text(issue_text_rect, x_positions, y_top, y_bottom)
    if issue_rect is None:
        issue_rect = _previous_cell_before(rev_rect, x_positions, y_top, y_bottom)

    font_size = max(7.0, min(9.2, (y_bottom - y_top) * 0.48))
    return _HeaderCells(
        issue_rect=issue_rect,
        rev_rect=rev_rect,
        date_rect=date_rect,
        font_size=font_size,
        draw_borders=True,
    )


def _vertical_overlap(rect: fitz.Rect, target: fitz.Rect) -> float:
    return max(0.0, min(rect.y1, target.y1) - max(rect.y0, target.y0))


def _issue_words_before_revision(line_words: list[tuple], rev_word: tuple) -> list[tuple]:
    try:
        rev_index = line_words.index(rev_word)
    except ValueError:
        return []

    for index in range(rev_index - 1, -1, -1):
        text = str(line_words[index][4]).strip().upper()
        if text.startswith("ISSUE"):
            return [line_words[index]]
    return []


def _header_drawing_lines(page: fitz.Page) -> tuple[list[fitz.Rect], list[fitz.Rect]]:
    verticals: list[fitz.Rect] = []
    horizontals: list[fitz.Rect] = []
    top_limit = min(150.0, page.rect.height * 0.22)
    for drawing in page.get_drawings():
        for item in drawing.get("items", []):
            rect = _line_rect_from_drawing_item(item)
            if rect is None or rect.y0 > top_limit:
                continue
            if rect.height >= 6.0 and rect.width <= 4.0:
                verticals.append(rect)
            elif rect.width >= 8.0 and rect.height <= 4.0:
                horizontals.append(rect)
    return verticals, horizontals


def _line_rect_from_drawing_item(item: tuple) -> fitz.Rect | None:
    kind = item[0]
    if kind == "re" and len(item) >= 2:
        return fitz.Rect(item[1])
    if kind == "l" and len(item) >= 3:
        start = item[1]
        end = item[2]
        x0 = min(float(start.x), float(end.x))
        x1 = max(float(start.x), float(end.x))
        y0 = min(float(start.y), float(end.y))
        y1 = max(float(start.y), float(end.y))
        if abs(x1 - x0) <= 0.5:
            x0 -= 0.5
            x1 += 0.5
        if abs(y1 - y0) <= 0.5:
            y0 -= 0.5
            y1 += 0.5
        return fitz.Rect(x0, y0, x1, y1)
    return None


def _revision_row_bounds(
    row_verticals: list[fitz.Rect],
    horizontals: list[fitz.Rect],
    rev_text_rect: fitz.Rect,
) -> tuple[float, float]:
    top_candidates = [
        rect
        for rect in horizontals
        if rect.y1 <= rev_text_rect.y0 + 3.0 and abs(rect.y1 - rev_text_rect.y0) <= 10.0
    ]
    bottom_candidates = [
        rect
        for rect in horizontals
        if rect.y0 >= rev_text_rect.y1 - 3.0 and abs(rect.y0 - rev_text_rect.y1) <= 12.0
    ]

    if top_candidates:
        top_line = min(top_candidates, key=lambda rect: abs(rect.y1 - rev_text_rect.y0))
        y_top = top_line.y0
    else:
        y_top = min(rect.y0 for rect in row_verticals)

    if bottom_candidates:
        bottom_line = min(bottom_candidates, key=lambda rect: abs(rect.y0 - rev_text_rect.y1))
        y_bottom = bottom_line.y1
    else:
        y_bottom = max(rect.y1 for rect in row_verticals)

    return y_top, y_bottom


def _merge_positions(positions: list[float], *, tolerance: float) -> list[float]:
    if not positions:
        return []
    sorted_positions = sorted(positions)
    merged: list[list[float]] = [[sorted_positions[0]]]
    for position in sorted_positions[1:]:
        if abs(position - (sum(merged[-1]) / len(merged[-1]))) <= tolerance:
            merged[-1].append(position)
        else:
            merged.append([position])
    return [sum(group) / len(group) for group in merged]


def _cell_rect_around_text(
    text_rect: fitz.Rect,
    x_positions: list[float],
    y_top: float,
    y_bottom: float,
) -> fitz.Rect | None:
    left_candidates = [x for x in x_positions if x <= text_rect.x0 - 0.5]
    right_candidates = [x for x in x_positions if x >= text_rect.x1 + 0.5]
    if not left_candidates or not right_candidates:
        return None
    left = max(left_candidates)
    right = min(right_candidates)
    if right - left < max(14.0, text_rect.width + 2.0):
        return None
    return fitz.Rect(left, y_top, right, y_bottom)


def _next_cell_after(rect: fitz.Rect, x_positions: list[float], y_top: float, y_bottom: float) -> fitz.Rect | None:
    rights = [x for x in x_positions if x > rect.x1 + 0.5]
    if not rights:
        return None
    right = min(rights)
    if right - rect.x1 < 12.0:
        return None
    return fitz.Rect(rect.x1, y_top, right, y_bottom)


def _previous_cell_before(rect: fitz.Rect, x_positions: list[float], y_top: float, y_bottom: float) -> fitz.Rect | None:
    lefts = [x for x in x_positions if x < rect.x0 - 0.5]
    if not lefts:
        return None
    left = max(lefts)
    if rect.x0 - left < 12.0:
        return None
    return fitz.Rect(left, y_top, rect.x0, y_bottom)


def _same_visual_line(words: list[tuple], anchor: tuple) -> list[tuple]:
    anchor_center = (float(anchor[1]) + float(anchor[3])) / 2.0
    return [
        word
        for word in words
        if abs(((float(word[1]) + float(word[3])) / 2.0) - anchor_center) <= 4.0
    ]


def _date_words_after(line_words: list[tuple], start_index: int) -> list[tuple]:
    date_words: list[tuple] = []
    for word in line_words[start_index:]:
        text = str(word[4]).strip().upper().rstrip(".,")
        if not date_words:
            if _is_month_word(text) or _is_year_word(text):
                date_words.append(word)
            continue
        if len(date_words) >= 3:
            break
        if _is_month_word(text) or _is_year_word(text):
            date_words.append(word)
            if _is_year_word(text):
                break
            continue
        break
    return date_words


def _is_month_word(text: str) -> bool:
    return text[:3] in MONTH_WORDS


def _is_year_word(text: str) -> bool:
    return re.fullmatch(r"(?:19|20)\d{2}", text) is not None


def _apply_lep_updates(
    document: fitz.Document,
    changes: list[_ResolvedRevisionChange],
    result: RevisionUpdateResult,
    *,
    lep_pages: list[int],
    lep_entries: list[_LepEntry],
) -> None:
    if not lep_pages:
        result.lep_rows_unplaced.extend(change.change.page_label for change in changes)
        result.warnings.append("No LEP pages were detected; LEP rows were not updated.")
        return

    entries_by_label = {_normalize_label(entry.label): entry for entry in lep_entries}
    handled_labels: set[str] = set()
    missing: list[_ResolvedRevisionChange] = []
    inserted = _LepChapterInsertionResult(added=0, modified_pages=[], handled_labels=set())

    for change in changes:
        entry = entries_by_label.get(_normalize_label(change.change.page_label))
        if entry is None:
            missing.append(change)
            continue

        _write_lep_row(
            document[entry.page_index],
            entry,
            _LepOutputRow(
                label=entry.label,
                date_value=change.stamp.date_value,
                revision_value=change.stamp.revision_value,
                is_new_toc_row=True,
            ),
        )
        result.lep_rows_updated += 1
        handled_labels.add(_normalize_label(change.change.page_label))
        _track_modified_lep_page(result, entry.page_index)

    if missing:
        inserted, missing = _insert_missing_lep_rows_in_chapter_blocks(document, lep_entries, missing)
        result.lep_rows_added += inserted.added
        for modified_page in inserted.modified_pages:
            _track_modified_lep_page(result, modified_page)
        handled_labels.update(inserted.handled_labels)

    overflow_rows = list(inserted.overflow_rows)
    overflow_rows.extend(_overflow_rows_from_changes(missing))
    if overflow_rows:
        added, unplaced, modified_pages = _add_overflow_lep_rows(document, lep_pages, overflow_rows)
        result.lep_rows_added += added
        result.lep_rows_unplaced.extend(unplaced)
        for modified_page in modified_pages:
            _track_modified_lep_page(result, modified_page)
        if unplaced:
            result.warnings.append(
                f"{len(unplaced)} LEP rows could not be placed automatically; manual LEP review is required."
            )

    for page_number in result.lep_pages_modified:
        page_index = page_number - 1
        if 0 <= page_index < len(document):
            lep_page = document[page_index]
            _write_revision_header(
                lep_page,
                _RevisionStamp(
                    revision_value=_next_revision_value(lep_page),
                    date_value=_current_revision_date(),
                ),
            )


def _overflow_rows_from_changes(changes: list[_ResolvedRevisionChange]) -> list[_LepOutputRow]:
    return [
        _LepOutputRow(
            label=change.change.page_label,
            date_value=change.stamp.date_value,
            revision_value=change.stamp.revision_value,
            is_new_toc_row=True,
        )
        for change in changes
    ]


def _find_lep_pages(document: fitz.Document) -> list[int]:
    pages: list[int] = []
    for page_index, page in enumerate(document):
        text = page.get_text("text").upper()
        if "LIST OF EFFECTIVE PAGES" in text:
            pages.append(page_index)
            continue
        if "EFFECTIVE PAGES" in text and ("PAGE" in text and "REV" in text):
            pages.append(page_index)
    return pages


def _parse_lep_entries(document: fitz.Document, lep_pages: list[int]) -> list[_LepEntry]:
    entries: list[_LepEntry] = []
    for page_index in lep_pages:
        page = document[page_index]
        for line in _word_lines(page):
            entries.extend(_parse_lep_line(page, line, page_index))
    return entries


def _parse_lep_line(page: fitz.Page, words: list[tuple], page_index: int) -> list[_LepEntry]:
    entries: list[_LepEntry] = []
    if not words:
        return entries

    starts: list[tuple[int, str, int]] = []
    index = 0
    while index < len(words):
        text = str(words[index][4]).strip()
        upper_text = text.upper()
        if upper_text == "TOC" and index + 1 < len(words):
            next_text = str(words[index + 1][4]).strip()
            if PAGE_LABEL_RE.match(next_text):
                starts.append((index, f"TOC {next_text}", 2))
                index += 2
                continue
        if PAGE_LABEL_RE.match(text):
            starts.append((index, text, 1))
        index += 1

    for position, (start, label, token_count) in enumerate(starts):
        value_start = start + token_count
        value_end = starts[position + 1][0] if position + 1 < len(starts) else len(words)
        value_words = words[value_start:value_end]
        if not value_words:
            continue

        rev_offset = _revision_word_offset(value_words)
        if rev_offset is None:
            continue

        date_words = value_words[:rev_offset]
        rev_word = value_words[rev_offset]
        label_words = words[start : start + token_count]
        if date_words:
            date_rect = _union_rect(date_words) + (-1, -1, 2, 2)
        else:
            date_rect = _fallback_date_rect(words[start])
        rev_rect = fitz.Rect(rev_word[:4]) + (-2, -1, 3, 2)
        label_rect = _union_rect(label_words) + (-2, -1, 3, 2)
        row_cells = _detect_lep_row_cells(page, label_words, date_words, rev_word)
        if row_cells is not None:
            label_rect, date_rect, rev_rect = row_cells
        entries.append(
            _LepEntry(
                label=label,
                page_index=page_index,
                label_rect=label_rect,
                date_rect=date_rect,
                rev_rect=rev_rect,
                revision_value=_normalize_revision_value(str(rev_word[4]).strip()),
                y0=float(words[start][1]),
            )
        )

    return entries


def _revision_word_offset(words: list[tuple]) -> int | None:
    for offset in range(len(words) - 1, -1, -1):
        text = str(words[offset][4]).strip()
        if REVISION_TOKEN_RE.match(text):
            return offset
    return None


def _detect_lep_row_cells(
    page: fitz.Page,
    label_words: list[tuple],
    date_words: list[tuple],
    rev_word: tuple,
) -> tuple[fitz.Rect, fitz.Rect, fitz.Rect] | None:
    if not label_words:
        return None

    label_text_rect = _union_rect(label_words)
    date_text_rect = _union_rect(date_words) if date_words else _fallback_date_rect(label_words[-1])
    rev_text_rect = fitz.Rect(rev_word[:4])
    row_text_rect = label_text_rect | date_text_rect | rev_text_rect
    verticals, horizontals = _page_drawing_lines(page)
    if not verticals:
        return None

    center_y = (row_text_rect.y0 + row_text_rect.y1) / 2.0
    row_verticals = [
        rect
        for rect in verticals
        if rect.y0 - 4.0 <= center_y <= rect.y1 + 4.0
    ]
    if len(row_verticals) < 4:
        return None

    focus_left = label_text_rect.x0 - 18.0
    focus_right = rev_text_rect.x1 + 28.0
    x_positions = _merge_positions(
        [
            (rect.x0 + rect.x1) / 2.0
            for rect in row_verticals
            if focus_left <= (rect.x0 + rect.x1) / 2.0 <= focus_right
        ],
        tolerance=2.0,
    )
    if len(x_positions) < 4:
        return None

    y_top, y_bottom = _lep_row_bounds(row_verticals, horizontals, row_text_rect)
    label_rect = _cell_rect_around_text(label_text_rect, x_positions, y_top, y_bottom)
    date_rect = _cell_rect_around_text(date_text_rect, x_positions, y_top, y_bottom)
    rev_rect = _cell_rect_around_text(rev_text_rect, x_positions, y_top, y_bottom)
    if label_rect is None or date_rect is None or rev_rect is None:
        return None
    return label_rect, date_rect, rev_rect


def _page_drawing_lines(page: fitz.Page) -> tuple[list[fitz.Rect], list[fitz.Rect]]:
    verticals: list[fitz.Rect] = []
    horizontals: list[fitz.Rect] = []
    for drawing in page.get_drawings():
        for item in drawing.get("items", []):
            rect = _line_rect_from_drawing_item(item)
            if rect is None:
                continue
            if rect.height >= 6.0 and rect.width <= 4.0:
                verticals.append(rect)
            elif rect.width >= 8.0 and rect.height <= 4.0:
                horizontals.append(rect)
    return verticals, horizontals


def _lep_row_bounds(
    row_verticals: list[fitz.Rect],
    horizontals: list[fitz.Rect],
    row_text_rect: fitz.Rect,
) -> tuple[float, float]:
    top_candidates = [
        rect
        for rect in horizontals
        if rect.y1 <= row_text_rect.y0 + 3.0 and abs(rect.y1 - row_text_rect.y0) <= 8.0
    ]
    bottom_candidates = [
        rect
        for rect in horizontals
        if rect.y0 >= row_text_rect.y1 - 3.0 and abs(rect.y0 - row_text_rect.y1) <= 8.0
    ]
    if top_candidates:
        y_top = min(top_candidates, key=lambda rect: abs(rect.y1 - row_text_rect.y0)).y0
    else:
        y_top = min(rect.y0 for rect in row_verticals)
    if bottom_candidates:
        y_bottom = min(bottom_candidates, key=lambda rect: abs(rect.y0 - row_text_rect.y1)).y1
    else:
        y_bottom = max(rect.y1 for rect in row_verticals)
    return y_top, y_bottom


def _insert_missing_lep_rows_in_chapter_blocks(
    document: fitz.Document,
    lep_entries: list[_LepEntry],
    missing: list[_ResolvedRevisionChange],
) -> tuple[_LepChapterInsertionResult, list[_ResolvedRevisionChange]]:
    missing_by_chapter: dict[str, list[_ResolvedRevisionChange]] = {}
    leftovers: list[_ResolvedRevisionChange] = []
    for change in missing:
        chapter = _chapter_from_label(change.change.page_label)
        if chapter is None:
            leftovers.append(change)
            continue
        missing_by_chapter.setdefault(chapter, []).append(change)

    entries_by_chapter: dict[str, list[_LepEntry]] = {}
    for entry in lep_entries:
        chapter = _chapter_from_label(entry.label)
        if chapter is not None and not _normalize_label(entry.label).startswith("TOC "):
            entries_by_chapter.setdefault(chapter, []).append(entry)

    added = 0
    modified_pages: list[int] = []
    handled_labels: set[str] = set()
    overflow_rows_for_blank_pages: list[_LepOutputRow] = []

    for chapter, chapter_changes in missing_by_chapter.items():
        chapter_entries = entries_by_chapter.get(chapter)
        if not chapter_entries:
            leftovers.extend(chapter_changes)
            continue

        slots = sorted(chapter_entries, key=lambda entry: _natural_label_key(entry.label))
        original_rows = [
            _LepOutputRow(label=entry.label, date_value=_date_text_from_entry(document, entry), revision_value=entry.revision_value)
            for entry in slots
        ]
        toc_rows = [
            _LepOutputRow(
                label=change.change.page_label,
                date_value=change.stamp.date_value,
                revision_value=change.stamp.revision_value,
                is_new_toc_row=True,
            )
            for change in sorted(chapter_changes, key=lambda item: _natural_label_key(item.change.page_label))
        ]
        desired_rows = toc_rows + original_rows
        rows_for_slots = desired_rows[: len(slots)]
        overflow_rows = desired_rows[len(slots) :]

        for slot, row in zip(slots, rows_for_slots, strict=False):
            _write_lep_row(document[slot.page_index], slot, row)
            if slot.page_index not in modified_pages:
                modified_pages.append(slot.page_index)

        added += sum(1 for row in rows_for_slots if row.is_new_toc_row)
        handled_labels.update(_normalize_label(row.label) for row in toc_rows)
        overflow_rows_for_blank_pages.extend(overflow_rows)

    result = _LepChapterInsertionResult(
        added=added,
        modified_pages=modified_pages,
        handled_labels=handled_labels,
        overflow_rows=overflow_rows_for_blank_pages,
    )
    return result, leftovers


def _date_text_from_entry(document: fitz.Document, entry: _LepEntry) -> str:
    text = document[entry.page_index].get_text("text", clip=entry.date_rect)
    normalized = re.sub(r"\s+", " ", text).strip()
    return normalized or ""


def _write_lep_row(page: fitz.Page, slot: _LepEntry, row: _LepOutputRow) -> None:
    _overwrite_rect_text(page, slot.label_rect, row.label, fontsize=7.8)
    _overwrite_rect_text(page, slot.date_rect, row.date_value, fontsize=7.8, align=fitz.TEXT_ALIGN_CENTER)
    _overwrite_rect_text(page, slot.rev_rect, row.revision_value, fontsize=7.8, align=fitz.TEXT_ALIGN_CENTER)
    _draw_cell_borders(page, [slot.label_rect, slot.date_rect, slot.rev_rect])


def _add_overflow_lep_rows(
    document: fitz.Document,
    lep_pages: list[int],
    rows: list[_LepOutputRow],
) -> tuple[int, list[str], list[int]]:
    target_page_indices = _blank_lep_pages(document, lep_pages)
    if not target_page_indices:
        return 0, [row.label for row in rows], []

    sorted_rows = sorted(rows, key=lambda row: _natural_label_key(row.label))

    added = 0
    unplaced: list[str] = []
    modified_pages: list[int] = []
    cursor = 0

    for target_page_index in target_page_indices:
        if cursor >= len(sorted_rows):
            break
        page = document[target_page_index]
        layout = _prepare_blank_lep_page(page)
        page_capacity = layout["capacity"]
        page_rows = sorted_rows[cursor: cursor + page_capacity]
        if not page_rows:
            break

        for index, row in enumerate(page_rows):
            column_index = index // layout["max_rows_per_column"]
            row_index = index % layout["max_rows_per_column"]
            label_x, date_x, rev_x = layout["columns"][column_index]
            y = layout["row_y"] + row_index * layout["line_height"]
            if row_index == 0:
                _draw_lep_headers(page, label_x, date_x, rev_x, layout["header_y"])
            _write_text_in_cell(page, fitz.Rect(label_x, y - 9.0, date_x - 5.0, y + 2.5), row.label, fontsize=7.8)
            _write_text_in_cell(page, fitz.Rect(date_x, y - 9.0, rev_x - 5.0, y + 2.5), row.date_value, fontsize=7.8)
            _write_text_in_cell(page, fitz.Rect(rev_x, y - 9.0, rev_x + 24.0, y + 2.5), row.revision_value, fontsize=7.8)
            if row.is_new_toc_row:
                added += 1

        cursor += len(page_rows)
        modified_pages.append(target_page_index)

    unplaced.extend(row.label for row in sorted_rows[cursor:])
    return added, unplaced, modified_pages


def _blank_lep_pages(document: fitz.Document, lep_pages: list[int]) -> list[int]:
    pages: list[int] = []
    for page_index in reversed(lep_pages):
        text = document[page_index].get_text("text").upper()
        if "INTENTIONALLY LEFT BLANK" in text:
            pages.append(page_index)
    return sorted(pages)


def _prepare_blank_lep_page(page: fitz.Page) -> dict:
    body_top = 96.0
    body_bottom = min(590.0, page.rect.height - 175.0)
    _remove_existing_text(page, fitz.Rect(70.0, body_top, page.rect.width - 70.0, body_bottom + 10.0))
    title = "GENERATED LEP CONTINUATION"
    title_rect = fitz.Rect(70.0, body_top + 9.0, page.rect.width - 70.0, body_top + 24.0)
    _write_text_in_cell(page, title_rect, title, fontsize=9.0, align=fitz.TEXT_ALIGN_CENTER, redact=False)

    columns = _lep_columns(page.rect.width)
    row_y = body_top + 44.0
    line_height = 12.0
    max_rows_per_column = max(1, int((body_bottom - row_y) / line_height))
    return {
        "columns": columns,
        "row_y": row_y,
        "line_height": line_height,
        "max_rows_per_column": max_rows_per_column,
        "capacity": max_rows_per_column * len(columns),
        "header_y": body_top + 33.0,
    }


def _draw_lep_headers(page: fitz.Page, label_x: float, date_x: float, rev_x: float, y: float) -> None:
    _write_text_in_cell(page, fitz.Rect(label_x, y - 8.0, date_x - 5.0, y + 2.0), "PAGE NO.", fontsize=6.8, redact=False)
    _write_text_in_cell(page, fitz.Rect(date_x, y - 8.0, rev_x - 5.0, y + 2.0), "DATE", fontsize=6.8, redact=False)
    _write_text_in_cell(page, fitz.Rect(rev_x, y - 8.0, rev_x + 24.0, y + 2.0), "REV", fontsize=6.8, redact=False)


def _lep_columns(page_width: float) -> list[tuple[float, float, float]]:
    if page_width >= 550:
        return [(80.0, 142.0, 202.0), (230.0, 292.0, 352.0), (380.0, 442.0, 502.0)]

    usable_width = max(360.0, page_width - 120.0)
    step = usable_width / 3.0
    start = 60.0
    return [
        (start + step * index, start + step * index + 42.0, start + step * index + 100.0)
        for index in range(3)
    ]


def _overwrite_rect_text(
    page: fitz.Page,
    rect: fitz.Rect,
    text: str,
    *,
    fontsize: float,
    align: int = fitz.TEXT_ALIGN_LEFT,
) -> None:
    _write_text_in_cell(page, rect, text, fontsize=fontsize, align=align)


def _write_text_in_cell(
    page: fitz.Page,
    rect: fitz.Rect,
    text: str,
    *,
    fontsize: float,
    align: int = fitz.TEXT_ALIGN_LEFT,
    redact: bool = True,
) -> None:
    cell = fitz.Rect(rect)
    if redact:
        _remove_existing_text(page, _inner_rect(cell, x_pad=0.6, y_pad=0.35))
    cell = _inner_rect(cell, x_pad=2.0, y_pad=0.8)
    fitted_size = _fit_font_size(text, cell.width, fontsize)
    text_width = fitz.get_text_length(text, fontname="helv", fontsize=fitted_size)
    if align == fitz.TEXT_ALIGN_CENTER:
        x = cell.x0 + max(0.0, (cell.width - text_width) / 2.0)
    elif align == fitz.TEXT_ALIGN_RIGHT:
        x = cell.x1 - text_width
    else:
        x = cell.x0
    y = min(cell.y1 - 1.0, cell.y0 + fitted_size + 1.0)
    page.insert_text(
        (x, y),
        text,
        fontsize=fitted_size,
        fontname="helv",
        color=(0, 0, 0),
    )


def _inner_rect(rect: fitz.Rect, *, x_pad: float, y_pad: float) -> fitz.Rect:
    inner = fitz.Rect(rect)
    if inner.width > x_pad * 2.0 + 1.0:
        inner.x0 += x_pad
        inner.x1 -= x_pad
    if inner.height > y_pad * 2.0 + 1.0:
        inner.y0 += y_pad
        inner.y1 -= y_pad
    return inner


def _draw_cell_borders(page: fitz.Page, rects: list[fitz.Rect | None]) -> None:
    cells = [fitz.Rect(rect) for rect in rects if rect is not None and not rect.is_empty]
    if not cells:
        return

    x_positions = _merge_positions(
        [position for rect in cells for position in (rect.x0, rect.x1)],
        tolerance=1.8,
    )
    y_top = min(rect.y0 for rect in cells)
    y_bottom = max(rect.y1 for rect in cells)
    x_left = min(x_positions)
    x_right = max(x_positions)
    width = 0.55

    page.draw_line(fitz.Point(x_left, y_top), fitz.Point(x_right, y_top), color=(0, 0, 0), width=width, overlay=True)
    page.draw_line(
        fitz.Point(x_left, y_bottom),
        fitz.Point(x_right, y_bottom),
        color=(0, 0, 0),
        width=width,
        overlay=True,
    )
    for x in x_positions:
        page.draw_line(fitz.Point(x, y_top), fitz.Point(x, y_bottom), color=(0, 0, 0), width=width, overlay=True)


def _fit_font_size(text: str, width: float, initial_size: float) -> float:
    size = initial_size
    while size > 5.5 and fitz.get_text_length(text, fontname="helv", fontsize=size) > max(width - 1.0, 1.0):
        size -= 0.25
    return size


def _remove_existing_text(page: fitz.Page, rect: fitz.Rect) -> None:
    page.add_redact_annot(rect, fill=(1, 1, 1))
    page.apply_redactions(
        images=fitz.PDF_REDACT_IMAGE_NONE,
        graphics=fitz.PDF_REDACT_LINE_ART_NONE,
        text=fitz.PDF_REDACT_TEXT_REMOVE,
    )


def _word_lines(page: fitz.Page) -> list[list[tuple]]:
    lines: list[list[tuple]] = []
    for word in page.get_text("words"):
        text = str(word[4]).strip()
        if not text:
            continue
        y0 = float(word[1])
        if y0 < 85.0 or y0 > min(600.0, page.rect.height - 120.0):
            continue
        center_y = (float(word[1]) + float(word[3])) / 2.0
        matched_line: list[tuple] | None = None
        for line in lines:
            line_center = sum((float(item[1]) + float(item[3])) / 2.0 for item in line) / len(line)
            if abs(line_center - center_y) <= 4.0:
                matched_line = line
                break
        if matched_line is None:
            lines.append([word])
        else:
            matched_line.append(word)

    return [
        sorted(words, key=lambda item: float(item[0]))
        for words in sorted(lines, key=lambda words: (min(float(word[1]) for word in words), min(float(word[0]) for word in words)))
    ]


def _union_rect(words: list[tuple]) -> fitz.Rect:
    rect = fitz.Rect(words[0][:4])
    for word in words[1:]:
        rect |= fitz.Rect(word[:4])
    return rect


def _fallback_date_rect(label_word: tuple) -> fitz.Rect:
    label_rect = fitz.Rect(label_word[:4])
    return fitz.Rect(label_rect.x1 + 10.0, label_rect.y0 - 1.0, label_rect.x1 + 74.0, label_rect.y1 + 2.0)


def _normalize_revision_value(revision: str) -> str:
    value = revision.strip()
    value = re.sub(r"^REV\s*[-:]?\s*", "", value, flags=re.IGNORECASE)
    return value or revision.strip()


def _normalize_revision_date(revision_date: str) -> str:
    value = revision_date.strip()
    if DATE_ISO_RE.match(value):
        try:
            return datetime.fromisoformat(value[:10]).strftime("%b %Y").upper()
        except ValueError:
            pass
    return re.sub(r"\s+", " ", value.upper())


def _current_revision_date() -> str:
    return datetime.now().strftime("%b %Y").upper()


def _next_revision_value(page: fitz.Page, *, fallback_current_revision: int | None = None) -> str:
    current = _current_revision_value(page)
    if current is None:
        if fallback_current_revision is not None:
            return str(fallback_current_revision + 1)
        return "1"

    match = re.match(r"^(?P<number>\d+)(?P<suffix>[A-Z]?)$", current, re.IGNORECASE)
    if match:
        number = int(match.group("number")) + 1
        return f"{number}{match.group('suffix').upper()}"

    return "1"


def _current_revision_value(page: fitz.Page) -> str | None:
    words = [word for word in page.get_text("words") if float(word[1]) <= min(145.0, page.rect.height * 0.2)]
    sorted_words = sorted(words, key=lambda item: (item[1], item[0]))
    for index, word in enumerate(sorted_words):
        text = str(word[4]).strip()
        match = HEADER_REV_RE.match(text)
        if match is None:
            continue
        inline_revision = match.group("revision")
        if inline_revision:
            return inline_revision.upper()
        if index + 1 < len(sorted_words):
            next_text = str(sorted_words[index + 1][4]).strip()
            if re.fullmatch(r"\d{1,3}[A-Z]?", next_text, re.IGNORECASE):
                return next_text.upper()
    return None


def _chapter_revision_index(entries: list[_LepEntry]) -> dict[str, int]:
    index: dict[str, int] = {}
    for entry in entries:
        chapter = _chapter_from_label(entry.label)
        if not chapter:
            continue
        match = re.match(r"^(?P<number>\d+)", entry.revision_value)
        if match is None:
            continue
        revision_number = int(match.group("number"))
        index[chapter] = max(index.get(chapter, -1), revision_number)
    return index


def _chapter_from_label(label: str) -> str | None:
    normalized = _normalize_label(label)
    match = re.search(r"\b(?:TOC\s+)?(?P<chapter>\d{2})-", normalized)
    return match.group("chapter") if match else None


def _normalize_label(label: str) -> str:
    normalized = (
        label.upper()
        .replace("\u00a0", " ")
        .replace("\u2013", "-")
        .replace("\u2014", "-")
    )
    normalized = re.sub(r"\s+", " ", normalized)
    normalized = re.sub(r"\bTOC\s*-\s*(\d+)\b", r"TOC-\1", normalized)
    normalized = re.sub(r"\s*-\s*", "-", normalized)
    normalized = re.sub(r"\bTOC(\d{2}-)", r"TOC \1", normalized)
    return normalized.strip()


def _natural_label_key(label: str) -> tuple:
    normalized = _normalize_label(label)
    parts = re.split(r"(\d+)", normalized)
    key: list[int | str] = []
    for part in parts:
        key.append(int(part) if part.isdigit() else part)
    return tuple(key)


def _track_modified_lep_page(result: RevisionUpdateResult, page_index: int) -> None:
    page_number = page_index + 1
    if page_number not in result.lep_pages_modified:
        result.lep_pages_modified.append(page_number)

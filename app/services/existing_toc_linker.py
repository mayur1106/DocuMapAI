from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import fitz

from app.config import get_settings
from app.services.bookmark_namer import bookmark_title_for_page


GLOBAL_TOC_RE = re.compile(r"\bTOC-\d+\b", re.IGNORECASE)
LOCAL_TOC_RE = re.compile(r"\bTOC\s+\d{2}-\d+\b", re.IGNORECASE)
ATA_RE = re.compile(r"\bATA\s*[-\u2013\u2014]?\s*(\d{2})\b", re.IGNORECASE)
EM_PAGE_RE = re.compile(r"\bEM\s*[-\u2013]\s*(\d+)\b", re.IGNORECASE)
ITEM_LABEL_RE = re.compile(r"^\d{2}(?:-\d{2}){1,6}[A-Z]?$")
ITEM_LABEL_SEARCH_RE = re.compile(r"\b\d{2}\s*-\s*\d{2}\s*-\s*\d{2}(?:\s*-\s*\d{2}){0,4}[A-Z]?\b")
SECTION_ZERO_RE = re.compile(r"\bSECTION\s*-\s*0\b", re.IGNORECASE)
LABEL_PATTERNS = [
    re.compile(r"\bTOC\s+\d{2}-\d+\b", re.IGNORECASE),
    re.compile(r"\b[A-Z]{2,5}-\d+\b", re.IGNORECASE),
    re.compile(r"\b\d{2}-\d+\b"),
]


@dataclass
class ExistingTocRow:
    title: str
    page_index: int
    rect: fitz.Rect
    toc_type: str = "global"
    level: int = 1
    reference_text: str = ""
    chapter: str = ""
    target_label: str | None = None
    target_page_index: int | None = None
    target_y: float = 0.0
    unresolved_reason: str | None = None

    @property
    def page_number(self) -> int:
        return self.page_index + 1

    @property
    def target_page_number(self) -> int | None:
        if self.target_page_index is None:
            return None
        return self.target_page_index + 1


@dataclass
class ExistingTocLinkResult:
    output_path: Path
    toc_pages: list[int]
    reference_pages: list[int] = field(default_factory=list)
    linked_rows: list[ExistingTocRow] = field(default_factory=list)
    unresolved_rows: list[ExistingTocRow] = field(default_factory=list)
    revision_update: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        linked_by_type = _count_by_type(self.linked_rows)
        unresolved_by_type = _count_by_type(self.unresolved_rows)
        return {
            "output_path": str(self.output_path),
            "toc_pages": [page + 1 for page in self.toc_pages],
            "reference_pages": [page + 1 for page in self.reference_pages],
            "linked_count": len(self.linked_rows),
            "unresolved_count": len(self.unresolved_rows),
            "linked_by_type": linked_by_type,
            "unresolved_by_type": unresolved_by_type,
            "linked_rows": [_row_to_dict(row) for row in self.linked_rows],
            "unresolved_rows": [_row_to_dict(row) for row in self.unresolved_rows],
            "revision_update": self.revision_update,
        }


@dataclass(frozen=True)
class OutlineRoot:
    title: str
    page_number: int


def hyperlink_existing_toc(
    source_pdf: Path,
    output_pdf: Path,
    *,
    revision: str | None = None,
    revision_date: str | None = None,
    track_link_repair_revision: bool = False,
) -> ExistingTocLinkResult:
    """Overlay internal links on an existing visible table of contents.

    The function preserves all original pages. It removes link annotations from
    detected TOC pages, then writes internal PDF links over resolved global and
    chapter-level TOC rows.
    """

    output_pdf.parent.mkdir(parents=True, exist_ok=True)

    with fitz.open(source_pdf) as document:
        global_toc_pages = find_existing_toc_pages(document)
        local_toc_pages = find_local_toc_pages(document)
        reference_pages = find_eicas_reference_pages(document)
        toc_pages = sorted(set(global_toc_pages + local_toc_pages))
        if not toc_pages and not reference_pages:
            raise ValueError("No existing TOC or EICAS reference pages were detected.")

        rows = extract_existing_toc_rows(document, global_toc_pages)
        rows.extend(extract_local_toc_rows(document, local_toc_pages))
        rows.extend(extract_eicas_reference_rows(document, reference_pages))
        if not rows:
            raise ValueError("No linkable TOC rows or EICAS references were detected.")

        label_index = build_page_label_index(document, global_toc_pages)
        item_index = build_item_label_index(document, toc_pages)
        outline_index = build_outline_title_index(document)

        linked_rows: list[ExistingTocRow] = []
        unresolved_rows: list[ExistingTocRow] = []
        for row in rows:
            if row.toc_type == "local":
                location = item_index.get(normalize_label(row.target_label or ""))
                if location is not None:
                    row.target_page_index = location[0]
                    row.target_y = location[1]
                else:
                    row.unresolved_reason = "item_label_not_found_in_body"
            elif row.toc_type == "reference":
                location = item_index.get(normalize_label(row.target_label or ""))
                if location is not None:
                    row.target_page_index = location[0]
                    row.target_y = location[1]
                else:
                    row.unresolved_reason = "eicas_item_not_found_in_body"
            else:
                row.target_label = infer_target_label(row)
                row.target_page_index = resolve_target_page(row, label_index, outline_index)
                if row.target_page_index is None:
                    row.unresolved_reason = "page_label_or_outline_not_resolved"

            if row.target_page_index is None:
                unresolved_rows.append(row)
            else:
                linked_rows.append(row)

        if not linked_rows:
            raise ValueError("No existing TOC rows or EICAS references could be resolved to PDF pages.")

        for page_index in toc_pages:
            page = document[page_index]
            for link in list(page.get_links()):
                page.delete_link(link)

        for row in linked_rows:
            page = document[row.page_index]
            _delete_overlapping_links(page, row.rect)
            page.insert_link(
                {
                    "kind": fitz.LINK_GOTO,
                    "from": row.rect,
                    "page": row.target_page_index,
                    "to": fitz.Point(0, row.target_y),
                }
            )

        _repair_outline(document, linked_rows)
        revision_update = {}

        if output_pdf.exists():
            output_pdf.unlink()
        document.save(output_pdf, garbage=4, deflate=True)

    return ExistingTocLinkResult(
        output_path=output_pdf,
        toc_pages=toc_pages,
        reference_pages=reference_pages,
        linked_rows=linked_rows,
        unresolved_rows=unresolved_rows,
        revision_update=revision_update,
    )


def link_eicas_references(
    document: fitz.Document,
    excluded_pages: list[int] | None = None,
) -> tuple[list[ExistingTocRow], list[ExistingTocRow]]:
    """Add links on EICAS MEL item references in an already-open document."""

    excluded = sorted(set(excluded_pages or []))
    reference_pages = [
        page_index
        for page_index in find_eicas_reference_pages(document)
        if page_index not in excluded
    ]
    rows = extract_eicas_reference_rows(document, reference_pages)
    item_index = build_item_label_index(document, excluded)

    linked_rows: list[ExistingTocRow] = []
    unresolved_rows: list[ExistingTocRow] = []
    for row in rows:
        location = item_index.get(normalize_label(row.target_label or ""))
        if location is None:
            row.unresolved_reason = "eicas_item_not_found_in_body"
            unresolved_rows.append(row)
            continue

        row.target_page_index = location[0]
        row.target_y = location[1]
        page = document[row.page_index]
        _delete_overlapping_links(page, row.rect)
        page.insert_link(
            {
                "kind": fitz.LINK_GOTO,
                "from": row.rect,
                "page": row.target_page_index,
                "to": fitz.Point(0, row.target_y),
            }
        )
        linked_rows.append(row)

    return linked_rows, unresolved_rows


def find_existing_toc_pages(document: fitz.Document) -> list[int]:
    candidates: list[int] = []
    for page_index, page in enumerate(document):
        text = page.get_text("text")
        upper_text = text.upper()
        if "TABLE OF CONTENTS" not in upper_text:
            continue
        if "ATA -" in upper_text:
            continue
        if SECTION_ZERO_RE.search(text) or GLOBAL_TOC_RE.search(text) or _has_toc_table_header(text):
            candidates.append(page_index)
    return _longest_consecutive_run(candidates)


def find_local_toc_pages(document: fitz.Document) -> list[int]:
    """Find chapter-level TOC pages such as 'TOC 21-1'."""

    candidates: list[int] = []
    for page_index, page in enumerate(document):
        page_text = page.get_text("text")
        page_label_text = f"{_top_region_text(page)} {_bottom_region_text(page)}"
        if not LOCAL_TOC_RE.search(page_label_text):
            continue
        if _ata_chapter(page_text) is None:
            continue
        candidates.append(page_index)
    return candidates


def extract_existing_toc_rows(document: fitz.Document, toc_pages: list[int]) -> list[ExistingTocRow]:
    rows: list[ExistingTocRow] = []
    for page_index in toc_pages:
        rows.extend(_extract_rows_from_page(document[page_index], page_index))
    return rows


def extract_local_toc_rows(document: fitz.Document, toc_pages: list[int]) -> list[ExistingTocRow]:
    rows: list[ExistingTocRow] = []
    for page_index in toc_pages:
        rows.extend(_extract_local_rows_from_page(document[page_index], page_index))
    return rows


def find_eicas_reference_pages(document: fitz.Document) -> list[int]:
    """Find EICAS message pages that contain MEL item reference tables."""

    pages: list[int] = []
    for page_index, page in enumerate(document):
        text = page.get_text("text")
        upper_text = text.upper()
        if "EICAS MESSAGES" not in upper_text or "MEL ITEM" not in upper_text:
            continue

        reference_codes = [
            normalize_label(str(word[4]).strip())
            for word in page.get_text("words")
            if float(word[0]) >= 330
        ]
        if any(ITEM_LABEL_RE.fullmatch(code) for code in reference_codes):
            pages.append(page_index)

    pages.extend(_find_ocr_eicas_candidate_pages(document, pages))
    return sorted(set(pages))


def extract_eicas_reference_rows(document: fitz.Document, reference_pages: list[int]) -> list[ExistingTocRow]:
    rows: list[ExistingTocRow] = []
    for page_index in reference_pages:
        rows.extend(_extract_eicas_reference_rows_from_page(document[page_index], page_index))
    return rows


def build_page_label_index(document: fitz.Document, excluded_pages: list[int]) -> dict[str, int]:
    excluded = set(excluded_pages)
    label_index: dict[str, int] = {}

    for page_index, page in enumerate(document):
        if page_index in excluded:
            continue

        page_text_upper = page.get_text("text").upper()
        if "LIST OF EFFECTIVE PAGES" in page_text_upper:
            continue

        page_label_text = f"{_top_region_text(page)} {_bottom_region_text(page)}"
        for label in extract_reference_labels(page_label_text):
            normalized = normalize_label(label)
            label_index.setdefault(normalized, page_index)

    return label_index


def build_item_label_index(document: fitz.Document, excluded_pages: list[int]) -> dict[str, tuple[int, float]]:
    excluded = set(excluded_pages)
    item_index: dict[str, tuple[int, float]] = {}

    for page_index, page in enumerate(document):
        if page_index in excluded:
            continue

        page_text_upper = page.get_text("text").upper()
        ata_chapter = _ata_chapter(page_text_upper)
        if ata_chapter is None:
            continue
        if "LIST OF EFFECTIVE PAGES" in page_text_upper:
            continue

        lines = _word_lines(page)
        reference_table_y = _reference_table_start_y(lines)

        for index, line in enumerate(lines):
            y0 = float(line["y0"])
            words = line["words"]
            if y0 < 90 or y0 > page.rect.height - 45 or not words:
                continue
            if reference_table_y is not None and y0 > reference_table_y:
                continue

            label = _line_item_label(lines, index, ata_chapter)
            if label is None:
                continue

            item_index.setdefault(label, (page_index, max(0.0, y0 - 24.0)))

    return item_index


def build_outline_title_index(document: fitz.Document) -> dict[str, int]:
    title_index: dict[str, int] = {}
    for level, title, page_number in document.get_toc():
        if page_number < 1:
            continue
        title_index.setdefault(normalize_title(title), page_number - 1)
    return title_index


def infer_target_label(row: ExistingTocRow) -> str | None:
    labels = extract_reference_labels(row.reference_text)
    if labels:
        return labels[0]

    if row.chapter.isdigit():
        chapter_number = int(row.chapter)
        if chapter_number >= 20:
            return f"TOC {chapter_number}-1"

    return None


def resolve_target_page(
    row: ExistingTocRow,
    label_index: dict[str, int],
    outline_index: dict[str, int],
) -> int | None:
    if row.target_label:
        target = label_index.get(normalize_label(row.target_label))
        if target is not None:
            return target
        if re.match(r"^TOC\s+\d{2}-", normalize_label(row.target_label)):
            return None

    title_key = normalize_title(row.title)
    if title_key in outline_index:
        return outline_index[title_key]

    title_target = _resolve_common_front_matter(row.title, outline_index)
    if title_target is not None:
        return title_target

    return None


def extract_reference_labels(text: str) -> list[str]:
    labels: list[str] = []
    for pattern in LABEL_PATTERNS:
        for match in pattern.finditer(_normalize_reference_text(text)):
            label = match.group(0).upper()
            if label not in labels:
                labels.append(label)
    labels.sort(key=_label_priority)
    return labels


def normalize_label(label: str) -> str:
    normalized = (
        label.upper()
        .replace("\u00a0", " ")
        .replace("\u2013", "-")
        .replace("\u2014", "-")
        .replace("â€“", "-")
        .replace("â€”", "-")
    )
    return re.sub(r"\s+", " ", normalized).strip()


def normalize_title(title: str) -> str:
    normalized = re.sub(r"[^A-Z0-9]+", " ", title.upper()).strip()
    normalized = re.sub(r"\bMSGS\b", "MESSAGES", normalized)
    normalized = re.sub(r"\b(?:B787|787|PDF|MEL)\b", " ", normalized)
    normalized = re.sub(r"^\d+\s+", "", normalized)
    return re.sub(r"\s+", " ", normalized).strip()


def _extract_rows_from_page(page: fitz.Page, page_index: int) -> list[ExistingTocRow]:
    lines = _line_columns(page)
    rows: list[ExistingTocRow] = []
    current: ExistingTocRow | None = None

    for line in lines:
        y0, y1 = line["y0"], line["y1"]
        if y0 < 80 or y0 > page.rect.height - 45:
            continue

        chapter = line["chapter"]
        description = line["description"]
        reference = line["reference"]
        full_text = " ".join(part for part in (chapter, description, reference) if part).strip()
        if not full_text or _is_toc_header_or_footer(full_text):
            continue

        starts_row = bool(description) or bool(chapter and reference)
        if starts_row:
            if current and _row_is_linkable(current):
                rows.append(current)

            title = " ".join(part for part in (chapter, description) if part).strip()
            current = ExistingTocRow(
                title=title or full_text,
                chapter=chapter,
                reference_text=reference,
                page_index=page_index,
                rect=fitz.Rect(96, max(0, y0 - 2), page.rect.width - 54, y1 + 3),
            )
            continue

        if current and reference:
            current.reference_text = f"{current.reference_text} {reference}".strip()
            current.rect |= fitz.Rect(96, max(0, y0 - 2), page.rect.width - 54, y1 + 3)
        elif current and not reference and not chapter and description:
            current.title = f"{current.title} {description}".strip()
            current.rect |= fitz.Rect(96, max(0, y0 - 2), page.rect.width - 54, y1 + 3)

    if current and _row_is_linkable(current):
        rows.append(current)

    return rows


def _extract_local_rows_from_page(page: fitz.Page, page_index: int) -> list[ExistingTocRow]:
    rows: list[ExistingTocRow] = []
    current: ExistingTocRow | None = None

    for line in _word_lines(page):
        y0 = float(line["y0"])
        y1 = float(line["y1"])
        words = line["words"]
        if y0 < 90 or y0 > page.rect.height - 45 or not words:
            continue

        first_word = str(words[0][4]).strip()
        normalized_first_word = normalize_label(first_word)
        line_text = _words_to_text(words)
        starts_row = (
            float(words[0][0]) <= 170
            and ITEM_LABEL_RE.fullmatch(normalized_first_word) is not None
        )

        if starts_row:
            if current and _row_is_linkable(current):
                rows.append(current)

            title = " ".join(str(word[4]) for word in words[1:]).strip()
            current = ExistingTocRow(
                title=f"{normalized_first_word} {title}".strip(),
                toc_type="local",
                level=_local_row_level(normalized_first_word),
                target_label=normalized_first_word,
                reference_text=normalized_first_word,
                page_index=page_index,
                rect=fitz.Rect(68, max(0, y0 - 2), page.rect.width - 54, y1 + 3),
            )
            continue

        if current and float(words[0][0]) > 110 and line_text and not _is_local_header_or_footer(line_text):
            current.title = f"{current.title} {line_text}".strip()
            current.rect |= fitz.Rect(68, max(0, y0 - 2), page.rect.width - 54, y1 + 3)

    if current and _row_is_linkable(current):
        rows.append(current)

    return rows


def _extract_eicas_reference_rows_from_page(page: fitz.Page, page_index: int) -> list[ExistingTocRow]:
    rows = _extract_eicas_reference_rows_from_words(
        page=page,
        page_index=page_index,
        page_words=page.get_text("words"),
        source="text",
    )
    if rows or not _page_needs_ocr(page):
        return rows

    return _extract_eicas_reference_rows_from_words(
        page=page,
        page_index=page_index,
        page_words=_ocr_page_words(page),
        source="ocr",
    )


def _extract_eicas_reference_rows_from_words(
    *,
    page: fitz.Page,
    page_index: int,
    page_words: list[tuple],
    source: str,
) -> list[ExistingTocRow]:
    rows: list[ExistingTocRow] = []
    seen_rects: set[tuple[str, int, int, int, int]] = set()

    for line in _visual_word_lines(page_words):
        words = line["words"]
        if not words:
            continue
        y0 = float(line["y0"])
        if y0 < 90 or y0 > page.rect.height - 45:
            continue

        label, anchor_word = _eicas_reference_label_from_line(words)
        if label is None or anchor_word is None:
            continue

        rect = _eicas_row_link_rect(page, page_words, anchor_word)
        key = (label, round(rect.x0), round(rect.y0), round(rect.x1), round(rect.y1))
        if key in seen_rects:
            continue
        seen_rects.add(key)

        rows.append(
            ExistingTocRow(
                title=f"EICAS {source.upper()} reference {label}" if source == "ocr" else f"EICAS reference {label}",
                toc_type="reference",
                level=1,
                target_label=label,
                reference_text=label,
                page_index=page_index,
                rect=rect,
            )
        )

    return rows


def _eicas_row_link_rect(page: fitz.Page, words: list[tuple], target_word: tuple) -> fitz.Rect:
    target_y0 = float(target_word[1])
    target_y1 = float(target_word[3])
    same_row_words = [
        word
        for word in words
        if (
            abs(float(word[1]) - target_y0) <= 3.0
            or abs(float(word[3]) - target_y1) <= 3.0
            or (float(word[1]) <= target_y1 and float(word[3]) >= target_y0)
        )
    ]
    if not same_row_words:
        same_row_words = [target_word]

    left_x = min((float(word[0]) for word in same_row_words if float(word[0]) >= 45.0), default=float(target_word[0]))
    right_x = max(float(word[2]) for word in same_row_words)
    row_y0 = min(float(word[1]) for word in same_row_words)
    row_y1 = max(float(word[3]) for word in same_row_words)

    return fitz.Rect(
        max(0.0, left_x - 4.0),
        max(0.0, row_y0 - 2.0),
        min(float(page.rect.width), right_x + 6.0),
        min(float(page.rect.height), row_y1 + 3.0),
    )


def _eicas_reference_label_from_line(words: list[tuple]) -> tuple[str | None, tuple | None]:
    right_words = [word for word in words if float(word[0]) >= 300.0]
    if not right_words:
        return None, None

    for word in right_words:
        label = normalize_label(str(word[4]).strip())
        if ITEM_LABEL_RE.fullmatch(label):
            return label, word

    right_text = _words_to_text(right_words)
    match = ITEM_LABEL_SEARCH_RE.search(_normalize_reference_text(right_text))
    if match is None:
        return None, None

    label = normalize_label(match.group(0).replace(" ", ""))
    anchor_word = next((word for word in right_words if re.search(r"\d", str(word[4]))), right_words[0])
    return label, anchor_word


def _find_ocr_eicas_candidate_pages(document: fitz.Document, detected_pages: list[int]) -> list[int]:
    page_range = _eicas_page_range(document)
    if not page_range:
        return []

    detected = set(detected_pages)
    candidates: list[int] = []
    for page_index in page_range:
        if page_index in detected:
            continue
        if page_index < 0 or page_index >= len(document):
            continue
        if _page_needs_ocr(document[page_index]):
            candidates.append(page_index)
    return candidates


def _eicas_page_range(document: fitz.Document) -> range | None:
    em_pages: list[int] = []
    for page_index, page in enumerate(document):
        label_text = f"{_top_region_text(page)} {_bottom_region_text(page)}"
        if EM_PAGE_RE.search(label_text):
            em_pages.append(page_index)

    if em_pages:
        return range(min(em_pages), max(em_pages) + 1)

    eicas_pages: list[int] = []
    for page_index, page in enumerate(document):
        text = page.get_text("text").upper()
        if "SECTION" in text and "EICAS MESSAGES" in text:
            eicas_pages.append(page_index)

    if eicas_pages:
        return range(min(eicas_pages), max(eicas_pages) + 1)
    return None


def _page_needs_ocr(page: fitz.Page) -> bool:
    words = page.get_text("words")
    if len(words) > 8:
        return False
    return _large_image_coverage(page) >= 0.35


def _large_image_coverage(page: fitz.Page) -> float:
    page_area = max(float(page.rect.get_area()), 1.0)
    image_area = 0.0
    seen_rects: set[tuple[int, int, int, int]] = set()

    for image in page.get_images(full=True):
        xref = image[0]
        try:
            rects = page.get_image_rects(xref)
        except Exception:
            rects = []
        for rect in rects:
            key = (round(rect.x0), round(rect.y0), round(rect.x1), round(rect.y1))
            if key in seen_rects:
                continue
            seen_rects.add(key)
            image_area += float(rect.get_area())

    return min(image_area / page_area, 1.0)


def _ocr_page_words(page: fitz.Page) -> list[tuple]:
    settings = get_settings()
    if not settings.eicas_ocr_enabled:
        return []

    tessdata = str(settings.tessdata_dir) if settings.tessdata_dir else None
    try:
        textpage = page.get_textpage_ocr(
            language=settings.ocr_language,
            dpi=settings.ocr_dpi,
            full=True,
            tessdata=tessdata,
        )
    except RuntimeError as exc:
        raise RuntimeError(
            "OCR is required for an image-based EICAS page, but Tesseract OCR is not available. "
            "Install Tesseract OCR and ensure tesseract.exe is on PATH, or set TESSDATA_DIR to the tessdata folder."
        ) from exc

    return page.get_text("words", textpage=textpage)


def _visual_word_lines(words: list[tuple]) -> list[dict[str, object]]:
    visual_lines: list[dict[str, object]] = []
    for word in sorted(words, key=lambda item: (float(item[1]), float(item[0]))):
        text = str(word[4]).strip()
        if not text:
            continue
        center_y = (float(word[1]) + float(word[3])) / 2.0
        matched_line: dict[str, object] | None = None
        for line in visual_lines:
            if abs(float(line["center_y"]) - center_y) <= 4.0:
                matched_line = line
                break

        if matched_line is None:
            matched_line = {
                "center_y": center_y,
                "y0": float(word[1]),
                "y1": float(word[3]),
                "words": [],
            }
            visual_lines.append(matched_line)

        matched_line["words"].append(word)
        matched_line["center_y"] = (
            float(matched_line["center_y"]) * (len(matched_line["words"]) - 1) + center_y
        ) / len(matched_line["words"])
        matched_line["y0"] = min(float(matched_line["y0"]), float(word[1]))
        matched_line["y1"] = max(float(matched_line["y1"]), float(word[3]))

    for line in visual_lines:
        line["words"] = sorted(line["words"], key=lambda item: float(item[0]))

    return sorted(visual_lines, key=lambda line: (float(line["y0"]), float(line["words"][0][0])))


def _word_lines(page: fitz.Page) -> list[dict[str, object]]:
    grouped: dict[tuple[int, int], list[tuple]] = {}
    for word in page.get_text("words"):
        x0, y0, x1, y1, text, block_number, line_number, word_number = word
        if not str(text).strip():
            continue
        grouped.setdefault((block_number, line_number), []).append(word)

    lines: list[dict[str, object]] = []
    for words in grouped.values():
        ordered_words = sorted(words, key=lambda item: item[0])
        lines.append(
            {
                "y0": min(float(word[1]) for word in ordered_words),
                "y1": max(float(word[3]) for word in ordered_words),
                "words": ordered_words,
            }
        )

    return sorted(lines, key=lambda item: (float(item["y0"]), float(item["y1"])))


def _ata_chapter(text: str) -> str | None:
    match = ATA_RE.search(text)
    return match.group(1) if match else None


def _line_item_label(lines: list[dict[str, object]], index: int, ata_chapter: str) -> str | None:
    line = lines[index]
    words = line["words"]
    if not words:
        return None
    if float(words[0][0]) > 170:
        return None

    first_word = normalize_label(str(words[0][4]).strip())
    if ITEM_LABEL_RE.fullmatch(first_word) and first_word.startswith(f"{ata_chapter}-"):
        return first_word

    if index == 0:
        return None

    previous_text = _words_to_text(lines[index - 1]["words"])
    if not re.search(rf"\b{re.escape(ata_chapter)}\s*-\s*$", previous_text):
        return None

    combined = normalize_label(f"{ata_chapter}-{first_word}")
    if ITEM_LABEL_RE.fullmatch(combined):
        return combined
    return None


def _line_columns(page: fitz.Page) -> list[dict[str, str | float]]:
    grouped: dict[tuple[int, int], list[tuple]] = {}
    for word in page.get_text("words"):
        x0, y0, x1, y1, text, block_number, line_number, word_number = word
        if not text.strip():
            continue
        grouped.setdefault((block_number, line_number), []).append(word)

    lines: list[dict[str, str | float]] = []
    for words in grouped.values():
        words = sorted(words, key=lambda item: (item[1], item[0]))
        y0 = min(float(word[1]) for word in words)
        y1 = max(float(word[3]) for word in words)
        chapter_words: list[tuple] = []
        description_words: list[tuple] = []
        reference_words: list[tuple] = []

        for word in words:
            x0 = float(word[0])
            if x0 < 180:
                chapter_words.append(word)
            elif x0 < 390:
                description_words.append(word)
            else:
                reference_words.append(word)

        lines.append(
            {
                "y0": y0,
                "y1": y1,
                "chapter": _words_to_text(chapter_words),
                "description": _words_to_text(description_words),
                "reference": _words_to_text(reference_words),
            }
        )

    return sorted(lines, key=lambda item: (float(item["y0"]), float(item["y1"])))


def _words_to_text(words: list[tuple]) -> str:
    return " ".join(str(word[4]) for word in sorted(words, key=lambda item: item[0])).strip()


def _top_region_text(page: fitz.Page) -> str:
    words = [
        word
        for word in page.get_text("words")
        if float(word[1]) <= min(180.0, page.rect.height * 0.28)
    ]
    return " ".join(str(word[4]) for word in sorted(words, key=lambda item: (item[1], item[0])))


def _bottom_region_text(page: fitz.Page) -> str:
    words = [
        word
        for word in page.get_text("words")
        if float(word[1]) >= max(page.rect.height - 130.0, page.rect.height * 0.78)
    ]
    return " ".join(str(word[4]) for word in sorted(words, key=lambda item: (item[1], item[0])))


def _normalize_reference_text(text: str) -> str:
    normalized = (
        text.upper()
        .replace("\u2013", "-")
        .replace("\u2014", "-")
        .replace("â€“", "-")
        .replace("â€”", "-")
    )
    normalized = re.sub(r"(\d{2})\s*-\s*(\d+)", r"\1-\2", normalized)
    normalized = re.sub(r"([A-Z]{2,5})\s*-\s*(\d+)", r"\1-\2", normalized)
    normalized = re.sub(r"\bTOC\s+(\d{2})-(\d+)", r"TOC \1-\2", normalized)
    return re.sub(r"\s+", " ", normalized).strip()


def _label_priority(label: str) -> tuple[int, int]:
    if label.startswith("TOC "):
        return (0, 0)
    if re.match(r"^[A-Z]{2,5}-", label):
        return (1, 0)
    return (2, 0)


def _row_is_linkable(row: ExistingTocRow) -> bool:
    title = normalize_title(row.title)
    if not title:
        return False
    return title not in {"CHAPTER DESCRIPTION", "DESCRIPTION PAGE"}


def _local_row_level(label: str) -> int:
    parts = normalize_label(label).split("-")
    # 21-00-01 is the base item level under the chapter.
    return min(max(len(parts) - 1, 2), 6)


def _reference_table_start_y(lines: list[dict[str, object]]) -> float | None:
    starts: list[float] = []
    for line in lines:
        text = _words_to_text(line["words"])
        normalized = normalize_title(text)
        if normalized in {"MEL ITEM", "ASSOCIATED STATUS MESSAGE"}:
            starts.append(float(line["y0"]))
    return min(starts) if starts else None


def _is_toc_header_or_footer(text: str) -> bool:
    normalized = normalize_title(text)
    if normalized in {"CHAPTER", "DESCRIPTION", "PAGE", "CHAPTER DESCRIPTION PAGE", "DESCRIPTION PAGE", "TABLE OF CONTENTS"}:
        return True
    if re.fullmatch(r"TOC\s*\d+", normalized):
        return True
    return False


def _is_local_header_or_footer(text: str) -> bool:
    normalized = normalize_title(text)
    if normalized in {
        "BOEING",
        "MINIMUM EQUIPMENT LIST",
        "DISPATCH DEVIATION GUIDE",
        "TABLE OF CONTENTS",
        "AIR CONDITIONING",
    }:
        return True
    if normalized.startswith("AI ENGG"):
        return True
    if normalized.startswith("ATA"):
        return True
    if normalized.startswith("ISSUE") or normalized.startswith("REV"):
        return True
    if re.fullmatch(r"TOC\s+\d+\s+\d+", normalized):
        return True
    return False


def _has_toc_table_header(text: str) -> bool:
    upper = text.upper()
    return all(token in upper for token in ("CHAPTER", "DESCRIPTION", "PAGE"))


def _longest_consecutive_run(values: list[int]) -> list[int]:
    if not values:
        return []

    runs: list[list[int]] = []
    current = [values[0]]
    for value in values[1:]:
        if value == current[-1] + 1:
            current.append(value)
        else:
            runs.append(current)
            current = [value]
    runs.append(current)
    return max(runs, key=len)


def _resolve_common_front_matter(title: str, outline_index: dict[str, int]) -> int | None:
    normalized = normalize_title(title)
    if "COVER PAGE" in normalized:
        return 0

    fallbacks = {
        "DGCA APPROVAL LETTER": ("APPROVAL LETTER",),
        "LOG OF REVISIONS": ("LOG OF REV",),
        "LOG OF TEMPORARY REVISIONS": ("LOG OF TEMP REV",),
        "LIST OF EFFECTIVE PAGES": ("LEP",),
        "TABLE OF CONTENTS": ("TOC",),
    }
    for expected, outline_terms in fallbacks.items():
        if expected not in normalized:
            continue
        for outline_title, page_index in outline_index.items():
            if all(term in outline_title for term in outline_terms):
                return page_index
    return None


def _delete_overlapping_links(page: fitz.Page, rect: fitz.Rect) -> None:
    for link in list(page.get_links()):
        link_rect = fitz.Rect(link["from"])
        if link_rect.intersects(rect):
            page.delete_link(link)


def _count_by_type(rows: list[ExistingTocRow]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        counts[row.toc_type] = counts.get(row.toc_type, 0) + 1
    return counts


def _repair_outline(document: fitz.Document, rows: list[ExistingTocRow]) -> None:
    roots = _outline_roots(document, rows)
    if roots:
        repaired = _outline_from_roots_and_local_rows(roots, rows)
    else:
        repaired = _outline_from_resolved_rows(document, rows)

    if repaired:
        document.set_toc(repaired)


def _outline_roots(document: fitz.Document, rows: list[ExistingTocRow]) -> list[OutlineRoot]:
    bookmark_cache: dict[int, str] = {}
    roots_by_page: dict[int, OutlineRoot] = {}

    for level, title, page_number in document.get_toc():
        if level != 1 or page_number < 1:
            continue
        roots_by_page.setdefault(
            page_number,
            OutlineRoot(
                title=bookmark_title_for_page(document, page_number, title, bookmark_cache),
                page_number=page_number,
            ),
        )

    for row in rows:
        if row.toc_type != "global" or row.target_page_number is None:
            continue
        roots_by_page.setdefault(
            row.target_page_number,
            OutlineRoot(
                title=bookmark_title_for_page(
                    document,
                    row.target_page_number,
                    row.title,
                    bookmark_cache,
                ),
                page_number=row.target_page_number,
            ),
        )

    return sorted(roots_by_page.values(), key=lambda root: (root.page_number, root.title))


def _outline_from_roots_and_local_rows(
    roots: list[OutlineRoot],
    rows: list[ExistingTocRow],
) -> list[list[int | str]]:
    repaired: list[list[int | str]] = []
    local_rows = sorted(
        (
            row
            for row in rows
            if row.toc_type == "local" and row.target_page_number is not None
        ),
        key=lambda row: (row.target_page_number or 0, row.target_y, row.title),
    )
    local_index = 0

    for root_index, root in enumerate(roots):
        repaired.append([1, root.title, root.page_number])
        next_root_page = roots[root_index + 1].page_number if root_index + 1 < len(roots) else None
        previous_level = 1

        while local_index < len(local_rows):
            row = local_rows[local_index]
            row_page = row.target_page_number or 0
            if row_page < root.page_number:
                local_index += 1
                continue
            if next_root_page is not None and row_page >= next_root_page:
                break

            level = max(2, min(row.level, 6))
            if level > previous_level + 1:
                level = previous_level + 1
            repaired.append([level, row.title, row_page])
            previous_level = level
            local_index += 1

    return repaired


def _outline_from_resolved_rows(
    document: fitz.Document,
    rows: list[ExistingTocRow],
) -> list[list[int | str]]:
    repaired: list[list[int | str]] = []
    previous_level = 0
    bookmark_cache: dict[int, str] = {}
    resolved_rows = sorted(
        (
            row
            for row in rows
            if row.toc_type != "reference" and row.target_page_number is not None
        ),
        key=lambda row: (row.target_page_number or 0, row.target_y, row.level, row.title),
    )
    for row in resolved_rows:
        if row.toc_type == "reference":
            continue
        if row.target_page_number is None:
            continue
        level = row.level if previous_level else 1
        level = min(level, previous_level + 1) if previous_level else level
        if row.toc_type == "local":
            bookmark_title = row.title
        else:
            bookmark_title = bookmark_title_for_page(
                document,
                row.target_page_number,
                row.title,
                bookmark_cache,
            )
        repaired.append([level, bookmark_title, row.target_page_number])
        previous_level = level
    return repaired


def _row_to_dict(row: ExistingTocRow) -> dict:
    return {
        "title": row.title,
        "toc_page": row.page_number,
        "reference_text": row.reference_text,
        "toc_type": row.toc_type,
        "target_label": row.target_label,
        "target_page": row.target_page_number,
        "unresolved_reason": row.unresolved_reason,
    }

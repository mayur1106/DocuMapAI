from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import fitz

from app.models import Heading


ATA_RE = re.compile(r"\bATA\s*[-\u2013\u2014]?\s*(\d{2})\b", re.IGNORECASE)
ITEM_LABEL_RE = re.compile(r"^\d{2}(?:-\d{2}){1,6}[A-Z]?$")
FOOTNOTE_LEGEND_RE = re.compile(r"^(?P<marker>[#*]+)\s+(?P<text>.+)$")
STOP_SECTION_RE = re.compile(
    r"^(MAINTENANCE|OPERATIONS?|OPERATIONAL|DISPATCH|CREW|NOTE\b|DEACTIVATION|RESTORATION)\b",
    re.IGNORECASE,
)
HEADER_WORDS = {
    "CATEGORY",
    "DESCRIPTION",
    "DISPATCH",
    "EXCEPTIONS",
    "FOR",
    "INSTALLED",
    "ITEM",
    "NUMBER",
    "OR",
    "REMARKS",
    "REQUIRED",
}


@dataclass(frozen=True)
class TableGeometry:
    item_left: float
    item_right: float
    description_left: float
    description_right: float
    top_y: float


@dataclass
class MelTableEntry:
    label: str
    description_parts: list[str] = field(default_factory=list)
    page_number: int = 1
    x0: float = 0.0
    y0: float = 0.0

    @property
    def title(self) -> str:
        description = normalize_text(" ".join(self.description_parts))
        return f"{self.label} {description}".strip()


def extract_mel_table_headings(pdf_path: Path | str) -> list[Heading]:
    """Extract TOC entries from MEL item tables using ITEM + DESCRIPTION columns."""

    headings: list[Heading] = []
    seen_labels: set[str] = set()

    with fitz.open(pdf_path) as document:
        for page_index, page in enumerate(document, start=1):
            for entry in _extract_page_entries(page, page_index):
                if entry.label in seen_labels or not entry.title:
                    continue
                seen_labels.add(entry.label)
                headings.append(
                    Heading(
                        title=entry.title,
                        level=_level_from_item_label(entry.label),
                        page=entry.page_number,
                        confidence=0.96,
                        source="mel_table",
                        x0=entry.x0,
                        y0=max(0.0, entry.y0 - 24.0),
                    )
                )

    return headings


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _extract_page_entries(page: fitz.Page, page_number: int) -> list[MelTableEntry]:
    lines = _word_lines(page)
    page_text = page.get_text("text")
    ata_chapter = _ata_chapter(page_text)
    geometry = _find_table_geometry(page, lines, page.rect.width, ata_chapter)
    if ata_chapter is None or geometry is None:
        return []

    entries: list[MelTableEntry] = []
    current: MelTableEntry | None = None

    for line in lines:
        y0 = float(line["y0"])
        y1 = float(line["y1"])
        words = line["words"]
        if y0 < geometry.top_y or y0 > page.rect.height - 45 or not words:
            continue

        line_text = _words_to_text(words)
        if _is_footnote_legend_line(line_text):
            if current and current.title:
                entries.append(current)
            current = None
            continue
        if _is_table_header(line_text):
            continue

        if _looks_like_table_stop(words, line_text, geometry):
            if current and current.title:
                entries.append(current)
            current = None
            break

        label = _item_label_from_line(words, geometry, ata_chapter)
        description = _description_text_from_line(words, geometry)
        if label:
            if current and current.title:
                entries.append(current)
            current = MelTableEntry(
                label=label,
                description_parts=[description] if description else [],
                page_number=page_number,
                x0=float(words[0][0]),
                y0=y0,
            )
            continue

        if current and description:
            current.description_parts.append(description)
            current.y0 = min(current.y0, y0)
            continue

        if current and y1 - current.y0 > 260:
            entries.append(current)
            current = None

    if current and current.title:
        entries.append(current)

    _apply_footnote_legend(entries, lines, geometry)
    return entries


def _apply_footnote_legend(entries: list[MelTableEntry], lines: list[dict[str, object]], geometry: TableGeometry) -> None:
    if not entries:
        return
    legend_map = _footnote_legend_map(lines, geometry)
    if not legend_map:
        return

    for entry in entries:
        if not entry.description_parts:
            continue
        description = normalize_text(" ".join(entry.description_parts))
        replacement = _replace_description_marker(description, legend_map)
        if replacement:
            entry.description_parts = [replacement]


def _footnote_legend_map(lines: list[dict[str, object]], geometry: TableGeometry) -> dict[str, str]:
    legends: dict[str, str] = {}
    for line in lines:
        y0 = float(line["y0"])
        if y0 < geometry.top_y:
            continue
        words = line["words"]
        if not words:
            continue
        line_text = _words_to_text(words)
        match = _parse_footnote_legend(line_text)
        if not match:
            continue
        marker, text = match
        if marker and text:
            legends[marker] = text
    return legends


def _normalize_marker_only(text: str) -> str | None:
    normalized = normalize_text(text)
    return normalized if re.fullmatch(r"[#*]+", normalized) else None


def _parse_footnote_legend(line_text: str) -> tuple[str, str] | None:
    match = FOOTNOTE_LEGEND_RE.match(normalize_text(line_text))
    if not match:
        return None
    marker = _normalize_marker_only(match.group("marker"))
    text = normalize_text(match.group("text"))
    if not marker or not text:
        return None
    return marker, text


def _is_footnote_legend_line(line_text: str) -> bool:
    return _parse_footnote_legend(line_text) is not None


def _replace_description_marker(description: str, legend_map: dict[str, str]) -> str | None:
    normalized = normalize_text(description)
    if not normalized:
        return None
    parts = normalized.split(" ", 1)
    marker = _normalize_marker_only(parts[0])
    if not marker:
        return None
    legend = legend_map.get(marker)
    if not legend:
        return None
    # If row text is exactly marker, replace fully.
    if len(parts) == 1:
        return legend
    # If marker leaked with partial words, replace marker prefix with full legend.
    trailing = normalize_text(parts[1])
    return f"{legend} {trailing}".strip()


def _find_table_geometry(
    page: fitz.Page,
    lines: list[dict[str, object]],
    page_width: float,
    ata_chapter: str | None,
) -> TableGeometry | None:
    all_words = [word for line in lines for word in line["words"]]
    item_words = [word for word in all_words if str(word[4]).strip().upper() == "ITEM"]
    description_words = [word for word in all_words if str(word[4]).strip().upper() == "DESCRIPTION"]

    for item_word in item_words:
        item_x = float(item_word[0])
        item_y = float(item_word[1])
        candidates = [
            word
            for word in description_words
            if float(word[0]) > item_x and abs(float(word[1]) - item_y) <= 8.0
        ]
        if not candidates:
            continue
        description_word = min(candidates, key=lambda word: float(word[0]))
        description_x = float(description_word[0])

        remarks_x_values = [
            float(word[0])
            for word in all_words
            if (
                str(word[4]).strip().upper() == "REMARKS"
                and float(word[0]) > description_x
                and abs(float(word[1]) - item_y) <= 8.0
            )
        ]
        remarks_x = min(remarks_x_values) if remarks_x_values else None
        description_right = description_x + 125.0
        if remarks_x is not None:
            description_right = min(description_right, remarks_x - 64.0)
        description_right = max(description_x + 70.0, min(description_right, page_width * 0.52))

        return TableGeometry(
            item_left=max(0.0, item_x - 24.0),
            item_right=max(item_x + 38.0, description_x - 8.0),
            description_left=max(0.0, description_x - 8.0),
            description_right=description_right,
            top_y=max(float(item_word[3]), float(description_word[3])) + 4.0,
        )

    return _infer_table_geometry_without_headers(page, lines, page_width, ata_chapter)


def _infer_table_geometry_without_headers(
    page: fitz.Page,
    lines: list[dict[str, object]],
    page_width: float,
    ata_chapter: str | None,
) -> TableGeometry | None:
    if ata_chapter is None:
        return None

    # Find candidate item labels on the left half of the page.
    label_candidates: list[tuple[tuple, list[tuple]]] = []
    for line in lines:
        words = line["words"]
        for word in words:
            label = normalize_label(str(word[4]).strip())
            x0 = float(word[0])
            if x0 > page_width * 0.45:
                continue
            if ITEM_LABEL_RE.fullmatch(label) and label.startswith(f"{ata_chapter}-"):
                label_candidates.append((word, words))
                break

    if not label_candidates:
        return None

    item_x = min(float(word[0]) for word, _ in label_candidates)
    item_top = min(float(word[1]) for word, _ in label_candidates)

    description_x_candidates: list[float] = []
    for label_word, line_words in label_candidates:
        label_right = float(label_word[2])
        for word in sorted(line_words, key=lambda item: float(item[0])):
            x0 = float(word[0])
            if x0 <= label_right + 2.0 or x0 > page_width * 0.62:
                continue
            token = normalize_label(str(word[4]).strip())
            if re.fullmatch(r"[A-D]|\d+|-|\*+", token):
                continue
            description_x_candidates.append(x0)
            break

    if not description_x_candidates:
        return None
    description_x = min(description_x_candidates)

    # Prefer table column borders from vector drawings to place description right edge.
    verticals: list[float] = []
    for drawing in page.get_drawings():
        for item in drawing.get("items", []):
            if item[0] != "l":
                continue
            p1, p2 = item[1], item[2]
            x1, y1 = float(p1.x), float(p1.y)
            x2, y2 = float(p2.x), float(p2.y)
            if abs(x1 - x2) > 0.8:
                continue
            if max(y1, y2) < item_top - 2.0 or min(y1, y2) > page.rect.height - 25.0:
                continue
            verticals.append((x1 + x2) / 2.0)

    description_right = page_width * 0.52
    if verticals:
        candidates = sorted(set(round(x, 1) for x in verticals if x > description_x + 40.0))
        if candidates:
            description_right = min(candidates)
    description_right = max(description_x + 70.0, min(description_right, page_width * 0.7))

    return TableGeometry(
        item_left=max(0.0, item_x - 24.0),
        item_right=max(item_x + 38.0, description_x - 8.0),
        description_left=max(0.0, description_x - 8.0),
        description_right=description_right,
        top_y=max(0.0, item_top - 2.0),
    )


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

    return sorted(lines, key=lambda item: (float(item["y0"]), float(item["y1"]), float(item["words"][0][0])))


def _ata_chapter(text: str) -> str | None:
    match = ATA_RE.search(text)
    return match.group(1) if match else None


def _item_label_from_line(words: list[tuple], geometry: TableGeometry, ata_chapter: str) -> str | None:
    for word in words:
        x0 = float(word[0])
        if x0 < geometry.item_left or x0 > geometry.item_right:
            continue
        label = normalize_label(str(word[4]).strip())
        if ITEM_LABEL_RE.fullmatch(label) and label.startswith(f"{ata_chapter}-"):
            return label
    return None


def _description_text_from_line(words: list[tuple], geometry: TableGeometry) -> str:
    description_words: list[tuple] = []
    for word in words:
        x0 = float(word[0])
        text = str(word[4]).strip()
        if x0 < geometry.description_left or x0 >= geometry.description_right:
            continue
        if _is_noise_description_word(text, x0, geometry):
            continue
        description_words.append(word)
    return _words_to_text(description_words)


def _is_noise_description_word(text: str, x0: float, geometry: TableGeometry) -> bool:
    normalized = normalize_label(text)
    if normalized in HEADER_WORDS:
        return True
    if x0 > geometry.description_left + 82.0 and re.fullmatch(r"[A-D]|\d+|\*+", normalized):
        return True
    if ITEM_LABEL_RE.fullmatch(normalized):
        return True
    return False


def _looks_like_table_stop(words: list[tuple], line_text: str, geometry: TableGeometry) -> bool:
    if not words:
        return False
    first_x = min(float(word[0]) for word in words)
    if first_x > geometry.item_left + 70.0:
        return False
    return STOP_SECTION_RE.match(normalize_label(line_text)) is not None


def _is_table_header(text: str) -> bool:
    normalized = normalize_label(text)
    return normalized in {
        "ITEM",
        "DESCRIPTION",
        "ITEM DESCRIPTION",
        "REMARKS OR EXCEPTIONS",
        "CATEGORY",
        "NUMBER INSTALLED",
        "NUMBER REQUIRED FOR DISPATCH",
    }


def _words_to_text(words: list[tuple]) -> str:
    return normalize_text(" ".join(str(word[4]) for word in sorted(words, key=lambda item: item[0])))


def normalize_label(text: str) -> str:
    normalized = text.upper().replace("\u00a0", " ").replace("\u2013", "-").replace("\u2014", "-")
    return re.sub(r"\s+", " ", normalized).strip()


def _level_from_item_label(label: str) -> int:
    return min(max(len(normalize_label(label).split("-")) - 2, 1), 6)

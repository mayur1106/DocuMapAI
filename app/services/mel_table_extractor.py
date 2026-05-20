from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import fitz

from app.models import Heading


ATA_RE = re.compile(r"\bATA\s*[-\u2013\u2014]?\s*(\d{2})\b", re.IGNORECASE)
ITEM_LABEL_RE = re.compile(r"^\d{2}(?:-\d{2}){1,6}[A-Z]?$")
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
    geometry = _find_table_geometry(lines, page.rect.width, ata_chapter)
    if geometry is None:
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

    return entries


def _find_table_geometry(
    lines: list[dict[str, object]],
    page_width: float,
    ata_chapter: str | None = None,
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

    return _fallback_table_geometry(lines, page_width, ata_chapter)


def _fallback_table_geometry(
    lines: list[dict[str, object]],
    page_width: float,
    ata_chapter: str | None,
) -> TableGeometry | None:
    label_words: list[tuple] = []
    description_x_values: list[float] = []

    for line in lines:
        words = line["words"]
        label = _first_item_label_word(words, ata_chapter)
        if label is None:
            continue
        label_words.append(label)
        label_x = float(label[0])
        for word in words:
            x0 = float(word[0])
            token = normalize_label(str(word[4]).strip())
            if x0 <= label_x + 36.0:
                continue
            if ITEM_LABEL_RE.fullmatch(token):
                continue
            description_x_values.append(x0)

    if len(label_words) < 2:
        return None

    item_x = sorted(float(word[0]) for word in label_words)[len(label_words) // 2]
    top_y = min(float(word[3]) for word in label_words) + 4.0
    if description_x_values:
        description_x = sorted(description_x_values)[len(description_x_values) // 2]
    else:
        description_x = item_x + 96.0

    description_right = max(description_x + 70.0, min(description_x + 180.0, page_width * 0.52))
    return TableGeometry(
        item_left=max(0.0, item_x - 24.0),
        item_right=max(item_x + 38.0, description_x - 8.0),
        description_left=max(0.0, description_x - 8.0),
        description_right=description_right,
        top_y=max(80.0, top_y),
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


def _item_label_from_line(
    words: list[tuple],
    geometry: TableGeometry,
    ata_chapter: str | None,
) -> str | None:
    for word in words:
        x0 = float(word[0])
        if x0 < geometry.item_left or x0 > geometry.item_right:
            continue
        label = normalize_label(str(word[4]).strip())
        if not ITEM_LABEL_RE.fullmatch(label):
            continue
        if ata_chapter and not label.startswith(f"{ata_chapter}-"):
            continue
            return label
    return None


def _first_item_label_word(words: list[tuple], ata_chapter: str | None) -> tuple | None:
    for word in words:
        label = normalize_label(str(word[4]).strip())
        if not ITEM_LABEL_RE.fullmatch(label):
            continue
        if ata_chapter and not label.startswith(f"{ata_chapter}-"):
            continue
        return word
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

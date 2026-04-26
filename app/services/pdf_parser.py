from __future__ import annotations

import re
from collections import Counter, defaultdict
from statistics import median
from typing import Iterable

import fitz

from app.models import LineMetadata
from app.utils.regex_patterns import COPYRIGHT_RE, PAGE_NUMBER_RE


WHITESPACE_RE = re.compile(r"\s+")


def normalize_text(text: str) -> str:
    return WHITESPACE_RE.sub(" ", text).strip()


def extract_lines(pdf_path: str) -> list[LineMetadata]:
    """Extract line-level text and layout metadata from every page."""

    extracted: list[LineMetadata] = []
    with fitz.open(pdf_path) as document:
        for page_index, page in enumerate(document, start=1):
            page_rect = page.rect
            page_dict = page.get_text("dict", sort=True)
            order = 0
            for block_number, block in enumerate(page_dict.get("blocks", [])):
                if block.get("type") != 0:
                    continue
                for line_number, line in enumerate(block.get("lines", [])):
                    spans = [span for span in line.get("spans", []) if normalize_text(span.get("text", ""))]
                    if not spans:
                        continue
                    metadata = _line_from_spans(
                        spans=spans,
                        page_number=page_index,
                        page_width=page_rect.width,
                        page_height=page_rect.height,
                        block_number=block_number,
                        line_number=line_number,
                        line_order=order,
                    )
                    extracted.append(metadata)
                    order += 1

    return _with_spacing(_filter_boilerplate(extracted))


def estimate_body_font_size(lines: Iterable[LineMetadata]) -> float:
    sizes = [
        round(line.font_size, 1)
        for line in lines
        if 20 <= len(line.text) <= 180 and not line.is_bold
    ]
    if not sizes:
        sizes = [round(line.font_size, 1) for line in lines]
    return float(median(sizes)) if sizes else 10.0


def _line_from_spans(
    spans: list[dict],
    page_number: int,
    page_width: float,
    page_height: float,
    block_number: int,
    line_number: int,
    line_order: int,
) -> LineMetadata:
    spans = sorted(spans, key=lambda span: span["bbox"][0])
    text = normalize_text(" ".join(span.get("text", "") for span in spans))
    total_chars = max(sum(len(span.get("text", "")) for span in spans), 1)
    font_size = sum(float(span.get("size", 0.0)) * len(span.get("text", "")) for span in spans) / total_chars
    dominant_span = max(spans, key=lambda span: len(span.get("text", "")))
    x0 = min(float(span["bbox"][0]) for span in spans)
    y0 = min(float(span["bbox"][1]) for span in spans)
    x1 = max(float(span["bbox"][2]) for span in spans)
    y1 = max(float(span["bbox"][3]) for span in spans)
    is_bold = any(_span_is_bold(span) for span in spans)

    return LineMetadata(
        text=text,
        page_number=page_number,
        font_size=round(font_size, 2),
        font_name=str(dominant_span.get("font", "")),
        is_bold=is_bold,
        x0=x0,
        y0=y0,
        x1=x1,
        y1=y1,
        page_width=page_width,
        page_height=page_height,
        block_number=block_number,
        line_number=line_number,
        line_order=line_order,
        normalized_text=_normalize_for_repetition(text),
    )


def _span_is_bold(span: dict) -> bool:
    font_name = str(span.get("font", "")).lower()
    flags = int(span.get("flags", 0))
    return bool(flags & 16) or any(token in font_name for token in ("bold", "black", "heavy", "demi"))


def _filter_boilerplate(lines: list[LineMetadata]) -> list[LineMetadata]:
    if not lines:
        return []

    page_count = max(line.page_number for line in lines)
    repeated_candidates: dict[str, set[int]] = defaultdict(set)
    for line in lines:
        if line.y0 <= max(90, line.page_height * 0.10) or _is_likely_footer(line):
            repeated_candidates[line.normalized_text].add(line.page_number)

    repeated_text = {
        text
        for text, pages in repeated_candidates.items()
        if text and len(pages) >= max(3, int(page_count * 0.3))
    }

    filtered: list[LineMetadata] = []
    for line in lines:
        text = line.text.strip()
        normalized = line.normalized_text
        if not text:
            continue
        if PAGE_NUMBER_RE.fullmatch(text):
            continue
        if COPYRIGHT_RE.search(text):
            continue
        if normalized in repeated_text and (line.y0 <= max(120, line.page_height * 0.12) or _is_likely_footer(line)):
            continue
        filtered.append(line)
    return filtered


def _is_likely_footer(line: LineMetadata) -> bool:
    return line.y0 >= line.page_height - max(72, line.page_height * 0.08)


def _with_spacing(lines: list[LineMetadata]) -> list[LineMetadata]:
    lines_by_page: dict[int, list[LineMetadata]] = defaultdict(list)
    for line in lines:
        lines_by_page[line.page_number].append(line)

    ordered: list[LineMetadata] = []
    for page_number in sorted(lines_by_page):
        page_lines = sorted(lines_by_page[page_number], key=lambda item: (item.y0, item.x0, item.line_order))
        for index, line in enumerate(page_lines):
            previous_line = page_lines[index - 1] if index > 0 else None
            next_line = page_lines[index + 1] if index < len(page_lines) - 1 else None
            line.spacing_before = max(0.0, line.y0 - previous_line.y1) if previous_line else 99.0
            line.spacing_after = max(0.0, next_line.y0 - line.y1) if next_line else 99.0
            ordered.append(line)
    return ordered


def common_left_margin(lines: Iterable[LineMetadata]) -> float:
    left_edges = [round(line.x0 / 5.0) * 5.0 for line in lines if len(line.text) > 20]
    if not left_edges:
        return 0.0
    return Counter(left_edges).most_common(1)[0][0]


def _normalize_for_repetition(text: str) -> str:
    normalized = normalize_text(text).lower()
    return re.sub(r"\d+", "#", normalized)

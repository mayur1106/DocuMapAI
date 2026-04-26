from __future__ import annotations

import math
import re
from collections import Counter

from app.config import Settings, get_settings
from app.models import Heading, LineMetadata
from app.services.pdf_parser import common_left_margin, estimate_body_font_size
from app.utils.regex_patterns import (
    FIGURE_TABLE_RE,
    KEYWORD_PREFIX_RE,
    NUMBERING_PREFIX_RE,
    UPPERCASE_HEADING_RE,
    URL_OR_EMAIL_RE,
    matches_required_heading_pattern,
)


MAX_HEADING_LEVEL = 6


def detect_headings(lines: list[LineMetadata], settings: Settings | None = None) -> list[Heading]:
    """Detect headings using layout-aware scoring and infer hierarchy."""

    settings = settings or get_settings()
    if not lines:
        return []

    body_font_size = estimate_body_font_size(lines)
    left_margin = common_left_margin(lines)
    prelim: list[tuple[LineMetadata, float, str | None]] = []

    for line in lines:
        score, reason = score_heading_candidate(line, body_font_size, left_margin)
        if score >= 0.35:
            prelim.append((line, score, reason))

    style_counts = Counter(_style_signature(line) for line, _, _ in prelim)
    headings: list[Heading] = []
    seen: set[tuple[str, int]] = set()

    for line, base_score, reason in prelim:
        score = base_score
        if style_counts[_style_signature(line)] >= 2:
            score += 0.08
        score = min(score, 0.99)

        numbered = NUMBERING_PREFIX_RE.match(line.text.strip()) is not None
        threshold = settings.numbered_heading_threshold if numbered else settings.heading_confidence_threshold
        if score < threshold:
            continue

        normalized_key = (line.normalized_text, line.page_number)
        if normalized_key in seen:
            continue
        seen.add(normalized_key)

        headings.append(
            Heading(
                title=_clean_title(line.text),
                level=1,
                page=line.page_number,
                confidence=round(score, 2),
                source=reason or "detected",
                font_size=line.font_size,
                font_name=line.font_name,
                is_bold=line.is_bold,
                x0=line.x0,
                y0=line.y0,
            )
        )

    return _assign_levels(headings)


def score_heading_candidate(
    line: LineMetadata,
    body_font_size: float,
    left_margin: float,
) -> tuple[float, str | None]:
    text = line.text.strip()
    words = text.split()
    word_count = len(words)
    char_count = len(text)
    score = 0.0
    reasons: list[str] = []

    if char_count < 3 or word_count == 0:
        return 0.0, None
    if URL_OR_EMAIL_RE.search(text):
        return 0.0, None
    if FIGURE_TABLE_RE.search(text):
        return 0.0, None

    if matches_required_heading_pattern(text):
        score += 0.22
        reasons.append("pattern")

    numbering_match = NUMBERING_PREFIX_RE.match(text)
    if numbering_match:
        score += 0.18
        reasons.append("numbered")

    if KEYWORD_PREFIX_RE.match(text):
        score += 0.18
        reasons.append("keyword")

    if line.font_size >= body_font_size + 1.5:
        score += 0.18
        reasons.append("larger_font")
    elif line.font_size >= body_font_size + 0.6:
        score += 0.08

    if line.is_bold:
        score += 0.12
        reasons.append("bold")

    if UPPERCASE_HEADING_RE.match(text) and len(text) >= 6:
        score += 0.12
        reasons.append("uppercase")
    elif _is_title_case(text):
        score += 0.07
        reasons.append("title_case")

    if word_count <= 12 and char_count <= 100:
        score += 0.12
        reasons.append("short")
    elif word_count <= 18 and char_count <= 140:
        score += 0.05
    else:
        score -= 0.12

    if abs(line.x0 - left_margin) <= 12 or line.x0 <= left_margin + 24:
        score += 0.08
        reasons.append("left_aligned")

    line_height = max(line.y1 - line.y0, 1.0)
    if line.spacing_before >= line_height * 0.85:
        score += 0.06
    if line.spacing_after >= line_height * 0.35:
        score += 0.04

    if 35 <= line.y0 <= line.page_height - 72:
        score += 0.03

    score += _style_strength(line.font_size, body_font_size, line.is_bold)

    if text.endswith(".") and word_count > 8 and not numbering_match:
        score -= 0.08
    if text[:1] in {"-", "•", "*"}:
        score -= 0.10
    if _looks_like_sentence(text) and not numbering_match:
        score -= 0.08

    return max(0.0, min(score, 1.0)), "+".join(reasons) if reasons else None


def _assign_levels(headings: list[Heading]) -> list[Heading]:
    if not headings:
        return []

    style_to_level = _style_level_map(headings)
    previous_level = 1
    assigned: list[Heading] = []

    for heading in headings:
        heading.level = _infer_level(heading, style_to_level)
        if not assigned:
            heading.level = 1
        elif heading.level > previous_level + 1:
            heading.level = previous_level + 1
        previous_level = heading.level
        assigned.append(heading)

    return assigned


def _infer_level(heading: Heading, style_to_level: dict[tuple[float, bool], int]) -> int:
    numbering_match = NUMBERING_PREFIX_RE.match(heading.title)
    if numbering_match:
        return min(numbering_match.group("number").count(".") + 1, MAX_HEADING_LEVEL)

    if KEYWORD_PREFIX_RE.match(heading.title):
        return 1

    style_key = (round(heading.font_size or 0.0, 1), bool(heading.is_bold))
    return style_to_level.get(style_key, 1)


def _style_level_map(headings: list[Heading]) -> dict[tuple[float, bool], int]:
    styles = {
        (round(heading.font_size or 0.0, 1), bool(heading.is_bold))
        for heading in headings
        if not NUMBERING_PREFIX_RE.match(heading.title)
    }
    ranked = sorted(styles, key=lambda item: (item[0], item[1]), reverse=True)
    return {style: min(index + 1, MAX_HEADING_LEVEL) for index, style in enumerate(ranked)}


def _style_signature(line: LineMetadata) -> tuple[float, bool, int]:
    return (round(line.font_size * 2) / 2, line.is_bold, int(math.floor(line.x0 / 10) * 10))


def _style_strength(font_size: float, body_font_size: float, is_bold: bool) -> float:
    if font_size >= body_font_size + 2.5 and is_bold:
        return 0.07
    if font_size >= body_font_size + 1.2 or is_bold:
        return 0.03
    return 0.0


def _clean_title(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip(" \t\r\n.")


def _is_title_case(text: str) -> bool:
    words = [word for word in re.split(r"\s+", text) if word and not word[0].isdigit()]
    if not words:
        return False
    titleish = sum(1 for word in words if word[:1].isupper())
    return titleish / len(words) >= 0.6


def _looks_like_sentence(text: str) -> bool:
    if len(text.split()) < 8:
        return False
    alpha = [char for char in text if char.isalpha()]
    if not alpha:
        return False
    lower_ratio = sum(1 for char in alpha if char.islower()) / len(alpha)
    return lower_ratio > 0.65

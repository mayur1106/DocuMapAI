from __future__ import annotations

import re
from collections.abc import MutableMapping

import fitz


ATA_RE = re.compile(r"\bATA\s*[-\u2013\u2014]?\s*(\d{2})\b", re.IGNORECASE)
OUTLINE_TITLE_RE = re.compile(
    r"\b(?P<fleet>787|777|737|A\s*320|A320|A\s*321|A321|A\s*330|A330|A\s*350|A350|A\s*380|A380)"
    r"[\s_-]+(?P<ata>\d{2})[\s_-]+(?P<section>.+)",
    re.IGNORECASE,
)

FLEET_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b(?:BOEING\s*)?B?\s*787(?:-\d+)?\b", re.IGNORECASE), "787"),
    (re.compile(r"\b(?:BOEING\s*)?B?\s*777(?:-\d+)?\b", re.IGNORECASE), "777"),
    (re.compile(r"\b(?:BOEING\s*)?B?\s*737(?:-\d+)?\b", re.IGNORECASE), "737"),
    (re.compile(r"\b(?:AIRBUS\s*)?A\s*320(?:-\d+)?\b", re.IGNORECASE), "a320"),
    (re.compile(r"\b(?:AIRBUS\s*)?A\s*321(?:-\d+)?\b", re.IGNORECASE), "a321"),
    (re.compile(r"\b(?:AIRBUS\s*)?A\s*330(?:-\d+)?\b", re.IGNORECASE), "a330"),
    (re.compile(r"\b(?:AIRBUS\s*)?A\s*350(?:-\d+)?\b", re.IGNORECASE), "a350"),
    (re.compile(r"\b(?:AIRBUS\s*)?A\s*380(?:-\d+)?\b", re.IGNORECASE), "a380"),
]

HEADER_NOISE_REPLACEMENTS = [
    re.compile(r"\bMINIMUM\s+EQUIPMENT\s+LIST\s*&?\b", re.IGNORECASE),
    re.compile(r"\bDISPATCH\s+DEVIATION\s+GUIDE\b", re.IGNORECASE),
    re.compile(r"\bDEVIATION\s+GUIDE\b", re.IGNORECASE),
    re.compile(r"\bDISPATCH\b", re.IGNORECASE),
    re.compile(r"\bAI\s*/\s*ENGG\s*/\s*MEL\s*/\s*[A-Z0-9-]+\b", re.IGNORECASE),
    re.compile(r"\bBOEING\b", re.IGNORECASE),
    re.compile(r"\bAIRBUS\b", re.IGNORECASE),
]

SECTION_STOP_RE = re.compile(
    r"\b(?:ISSUE|REV(?:ISION)?|JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|SEPT|OCT|NOV|DEC|"
    r"TABLE\s+OF\s+CONTENTS|NUMBER\s+INSTALLED|NUMBER\s+REQUIRED|CATEGORY|"
    r"ITEM\s+DESCRIPTION|REMARKS\s+OR\s+EXCEPTIONS|REMARKS|PAGE)\b",
    re.IGNORECASE,
)


def bookmark_title_for_page(
    document: fitz.Document,
    page_number: int,
    fallback_title: str,
    cache: MutableMapping[int, str] | None = None,
) -> str:
    """Return a header-derived sidebar bookmark title for a 1-based PDF page."""

    page_index = page_number - 1
    if page_index < 0 or page_index >= len(document):
        return fallback_title

    if cache is not None and page_index in cache:
        return cache[page_index]

    title = (
        derive_bookmark_title(document[page_index])
        or derive_bookmark_title_from_text(fallback_title)
        or fallback_title
    )
    if cache is not None:
        cache[page_index] = title
    return title


def derive_bookmark_title(page: fitz.Page) -> str | None:
    """Build fleet_ata_section bookmark names from a page header."""

    header_text = _normalize_header_text(_top_region_text(page))
    if not header_text:
        return None

    fleet = _fleet_token(header_text)
    ata = _ata_token(header_text)
    section = _section_token(header_text)
    if not fleet or not ata or not section:
        return None

    return f"{fleet}_{ata}_{section}"


def derive_bookmark_title_from_text(text: str) -> str | None:
    """Normalize existing bookmark text that already contains fleet and ATA."""

    normalized = _normalize_header_text(text)
    match = OUTLINE_TITLE_RE.search(normalized)
    if match is None:
        return None

    fleet = _normalize_fleet_token(match.group("fleet"))
    ata = match.group("ata")
    section = _slugify_section(match.group("section"))
    if not fleet or not section:
        return None
    return f"{fleet}_{ata}_{section}"


def _fleet_token(text: str) -> str | None:
    for pattern, token in FLEET_PATTERNS:
        if pattern.search(text):
            return token
    return None


def _ata_token(text: str) -> str | None:
    match = ATA_RE.search(text)
    return match.group(1) if match else None


def _section_token(text: str) -> str | None:
    match = ATA_RE.search(text)
    if match is None:
        return None

    section_text = text[match.end() :]
    for pattern in HEADER_NOISE_REPLACEMENTS:
        section_text = pattern.sub(" ", section_text)

    section_text = SECTION_STOP_RE.split(section_text, maxsplit=1)[0]
    section_text = re.sub(r"\b(?:B|A)?\d{3}(?:-\d+)?\b", " ", section_text, flags=re.IGNORECASE)
    section_text = re.sub(r"[^A-Z0-9\s/&-]+", " ", section_text, flags=re.IGNORECASE)
    section_text = re.sub(r"\s+", " ", section_text).strip(" -/&")

    if not section_text:
        return None

    words = re.findall(r"[A-Za-z0-9]+", section_text)
    if not words:
        return None

    return "_".join(word.lower() for word in words[:8])


def _normalize_fleet_token(text: str) -> str | None:
    compact = re.sub(r"[^A-Z0-9]", "", text.upper())
    if compact in {"787", "777", "737"}:
        return compact
    if compact in {"A320", "A321", "A330", "A350", "A380"}:
        return compact.lower()
    return None


def _slugify_section(text: str) -> str | None:
    words = re.findall(r"[A-Za-z0-9]+", text)
    if not words:
        return None
    return "_".join(word.lower() for word in words[:8])


def _top_region_text(page: fitz.Page) -> str:
    words = [
        word
        for word in page.get_text("words")
        if float(word[1]) <= min(180.0, page.rect.height * 0.28)
    ]
    return " ".join(str(word[4]) for word in sorted(words, key=lambda item: (item[1], item[0])))


def _normalize_header_text(text: str) -> str:
    normalized = (
        text.replace("\u00a0", " ")
        .replace("\u2013", "-")
        .replace("\u2014", "-")
        .replace("Ã¢â‚¬â€œ", "-")
        .replace("Ã¢â‚¬â€\u009d", "-")
    )
    return re.sub(r"\s+", " ", normalized).strip()

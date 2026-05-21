from __future__ import annotations

import re
from pathlib import Path

import fitz

from app.config import Settings, get_settings
from app.models import Heading
from app.services.bookmark_namer import bookmark_title_for_page
from app.services.existing_toc_linker import link_eicas_references
from app.services.revision_manager import RevisionPageChange, apply_revision_updates
from app.services.toc_builder import paginate_toc_entries


ITEM_HEADING_RE = re.compile(r"^\d{2}-\d{2}-\d{2}(?:-\d{2}){0,4}[A-Z]?\b")


def write_pdf_with_toc(
    source_pdf: Path,
    output_pdf: Path,
    headings: list[Heading],
    settings: Settings | None = None,
    *,
    revision: str | None = None,
    revision_date: str | None = None,
) -> Path:
    """Insert clickable TOC pages and sidebar bookmarks into a copy of the PDF."""

    settings = settings or get_settings()
    output_pdf.parent.mkdir(parents=True, exist_ok=True)
    normalized_headings = normalize_heading_levels(headings)

    with fitz.open(source_pdf) as document:
        first_page_rect = document[0].rect
        width = first_page_rect.width
        height = first_page_rect.height
        toc_pages = paginate_toc_entries(normalized_headings, height, settings)
        toc_page_count = len(toc_pages)

        for index in range(toc_page_count):
            document.new_page(pno=index, width=width, height=height)

        for page_index, page_headings in enumerate(toc_pages):
            _draw_toc_page(
                document[page_index],
                page_headings,
                toc_page_count,
                page_index,
                width,
                settings,
            )

        revision_changes = [
            RevisionPageChange(page_index=page_index, page_label=f"TOC-{page_index + 1}", change_type="insert_front_toc_page")
            for page_index in range(toc_page_count)
        ]

        bookmark_cache: dict[int, str] = {}
        outline = [
            [
                heading.level,
                _bookmark_title_for_heading(
                    document,
                    heading.page + toc_page_count,
                    heading,
                    bookmark_cache,
                ),
                heading.page + toc_page_count,
            ]
            for heading in normalized_headings
        ]
        document.set_toc(outline)
        link_eicas_references(document, excluded_pages=list(range(toc_page_count)))
        apply_revision_updates(
            document,
            revision_changes,
            revision=revision,
            revision_date=revision_date,
        )

        if output_pdf.exists():
            output_pdf.unlink()
        document.save(output_pdf, garbage=4, deflate=True)

    return output_pdf


def normalize_heading_levels(headings: list[Heading]) -> list[Heading]:
    """Return headings with PDF-outline-safe hierarchy levels."""

    normalized: list[Heading] = []
    previous_level = 0

    for heading in headings:
        level = max(1, min(int(heading.level), 6))
        if previous_level == 0:
            level = 1
        elif level > previous_level + 1:
            level = previous_level + 1

        normalized.append(heading.model_copy(update={"level": level}))
        previous_level = level

    return normalized


def _bookmark_title_for_heading(
    document: fitz.Document,
    page_number: int,
    heading: Heading,
    cache: dict[int, str],
) -> str:
    if heading.source == "mel_table" or ITEM_HEADING_RE.match(heading.title):
        return heading.title
    return bookmark_title_for_page(document, page_number, heading.title, cache)


def _draw_toc_page(
    page: fitz.Page,
    headings: list[Heading],
    toc_page_count: int,
    toc_page_index: int,
    width: float,
    settings: Settings,
) -> None:
    title = settings.toc_title if toc_page_index == 0 else f"{settings.toc_title} (continued)"
    page.insert_text(
        (settings.toc_margin_x, settings.toc_margin_top),
        title,
        fontsize=settings.toc_title_font_size if toc_page_index == 0 else 14,
        fontname=settings.toc_font,
        color=(0, 0, 0),
    )

    y = settings.toc_margin_top + settings.toc_title_font_size + 34 if toc_page_index == 0 else settings.toc_margin_top + 36
    right_x = width - settings.toc_margin_x
    for heading in headings:
        final_page_number = heading.page + toc_page_count
        indent = max(0, heading.level - 1) * settings.toc_indent_per_level
        x = settings.toc_margin_x + indent
        font_size = max(settings.toc_entry_font_size - (heading.level - 1) * 0.25, 8.5)
        max_title_width = max(60.0, right_x - x - 8)
        display_title = _truncate_to_width(heading.title, max_title_width, settings.toc_font, font_size)
        title_width = fitz.get_text_length(display_title, fontname=settings.toc_font, fontsize=font_size)
        dots = _leader_dots(right_x - 8 - (x + title_width), settings.toc_font, font_size)

        page.insert_text((x, y), display_title, fontsize=font_size, fontname=settings.toc_font, color=(0, 0, 0))
        if dots:
            page.insert_text((x + title_width + 4, y), dots, fontsize=font_size, fontname=settings.toc_font, color=(0.45, 0.45, 0.45))

        target_page_index = final_page_number - 1
        target_y = max(0.0, float(heading.y0 or 0.0))
        page.insert_link(
            {
                "kind": fitz.LINK_GOTO,
                "from": fitz.Rect(settings.toc_margin_x, y - font_size, right_x, y + 3),
                "page": target_page_index,
                "to": fitz.Point(0, target_y),
            }
        )
        y += settings.toc_line_height


def _leader_dots(available_width: float, fontname: str, fontsize: float) -> str:
    if available_width <= 8:
        return ""
    dot_width = max(fitz.get_text_length(".", fontname=fontname, fontsize=fontsize), 1.0)
    count = int(available_width / dot_width)
    return "." * max(0, count)


def _truncate_to_width(text: str, max_width: float, fontname: str, fontsize: float) -> str:
    if fitz.get_text_length(text, fontname=fontname, fontsize=fontsize) <= max_width:
        return text
    ellipsis = "..."
    remaining = text
    while remaining and fitz.get_text_length(remaining + ellipsis, fontname=fontname, fontsize=fontsize) > max_width:
        remaining = remaining[:-1].rstrip()
    return remaining + ellipsis if remaining else ellipsis

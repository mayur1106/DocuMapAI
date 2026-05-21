from __future__ import annotations

from app.config import Settings, get_settings
from app.models import Heading


def flatten_toc(headings: list[Heading]) -> list[dict]:
    return [
        {
            "title": heading.title,
            "level": heading.level,
            "page": heading.page,
            "confidence": heading.confidence,
        }
        for heading in headings
    ]


def paginate_toc_entries(headings: list[Heading], page_height: float, settings: Settings | None = None) -> list[list[Heading]]:
    settings = settings or get_settings()
    if not headings:
        return []

    pages: list[list[Heading]] = [[]]
    y = _first_entry_y(0, settings)

    for heading in headings:
        if y + settings.toc_line_height > page_height - settings.toc_margin_bottom:
            pages.append([])
            y = _first_entry_y(len(pages) - 1, settings)
        pages[-1].append(heading)
        y += settings.toc_line_height

    return [page for page in pages if page]


def _first_entry_y(page_index: int, settings: Settings) -> float:
    if page_index == 0:
        return settings.toc_margin_top + settings.toc_title_font_size + 34
    return settings.toc_margin_top + 36

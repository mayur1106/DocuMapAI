from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import fitz

from app.config import Settings, get_settings
from app.models import Heading
from app.services.bookmark_namer import derive_bookmark_title, derive_bookmark_title_from_text
from app.services.existing_toc_linker import (
    ExistingTocRow,
    build_outline_title_index,
    build_page_label_index,
    extract_existing_toc_rows,
    find_existing_toc_pages,
    infer_target_label,
    link_eicas_references,
    resolve_target_page,
)


ATA_RE = re.compile(r"\bATA\s*[-\u2013\u2014]?\s*(\d{2})\b", re.IGNORECASE)
SECTION_TITLE_CHAPTER_RE = re.compile(
    r"\b(?:787|777|737|A\s*320|A320|A\s*321|A321|A\s*330|A330|A\s*350|A350|A\s*380|A380)"
    r"[\s_-]+(?P<chapter>\d{2})\b",
    re.IGNORECASE,
)
ITEM_CHAPTER_RE = re.compile(r"^(?P<chapter>\d{2})-")


@dataclass
class SectionTocPlan:
    chapter: str
    title: str
    source_start_page: int
    source_end_page: int
    headings: list[Heading]
    toc_pages: list[list[Heading]]
    append_blank_page: bool = False
    body_start_y: float = 90.0
    footer_top_y: float = 742.0
    footer_baseline_y: float = 753.0

    @property
    def inserted_page_count(self) -> int:
        return len(self.toc_pages) + (1 if self.append_blank_page else 0)


@dataclass
class SectionTocResult:
    output_path: Path
    sections: list[dict] = field(default_factory=list)
    heading_count: int = 0
    inserted_page_count: int = 0
    linked_global_rows: int = 0
    linked_eicas_rows: int = 0
    unresolved_eicas_rows: int = 0
    revision_update: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "output_path": str(self.output_path),
            "section_count": len(self.sections),
            "sections": self.sections,
            "heading_count": self.heading_count,
            "inserted_page_count": self.inserted_page_count,
            "linked_global_rows": self.linked_global_rows,
            "linked_eicas_rows": self.linked_eicas_rows,
            "unresolved_eicas_rows": self.unresolved_eicas_rows,
            "revision_update": self.revision_update,
        }


def has_missing_section_toc_pattern(
    source_pdf: Path,
    headings: list[Heading],
    settings: Settings | None = None,
) -> bool:
    settings = settings or get_settings()
    with fitz.open(source_pdf) as document:
        return bool(_build_section_toc_plans(document, headings, settings))


def write_pdf_with_section_tocs(
    source_pdf: Path,
    output_pdf: Path,
    headings: list[Heading],
    settings: Settings | None = None,
    *,
    revision: str | None = None,
    revision_date: str | None = None,
) -> SectionTocResult:
    """Insert missing chapter TOC pages before each ATA section."""

    settings = settings or get_settings()
    output_pdf.parent.mkdir(parents=True, exist_ok=True)

    with fitz.open(source_pdf) as document:
        source_toc = document.get_toc()
        blank_template_page_index = _find_intentionally_blank_template_page(document)
        plans = _build_section_toc_plans(document, headings, settings)
        if not plans:
            raise ValueError("No ATA sections with MEL table rows were found for chapter TOC insertion.")

        global_rows = _resolve_global_toc_rows(document)
        insertions = [(plan.source_start_page, plan.inserted_page_count) for plan in plans]

        for plan in sorted(plans, key=lambda item: item.source_start_page, reverse=True):
            insert_at = plan.source_start_page - 1
            for offset in range(plan.inserted_page_count):
                if plan.append_blank_page and offset == plan.inserted_page_count - 1 and blank_template_page_index is not None:
                    document.fullcopy_page(blank_template_page_index, to=insert_at + offset)
                else:
                    document.fullcopy_page(insert_at + offset, to=insert_at + offset)

        inserted_page_indices: list[int] = []
        sections: list[dict] = []
        chapter_toc_counts: dict[str, int] = {}
        for plan in plans:
            first_toc_page = _first_toc_final_page(plan, insertions)
            section_pages = list(range(first_toc_page, first_toc_page + plan.inserted_page_count))
            inserted_page_indices.extend(page - 1 for page in section_pages)
            first_chapter_toc_number = chapter_toc_counts.get(plan.chapter, 0) + 1
            toc_labels = [
                f"TOC {plan.chapter}-{first_chapter_toc_number + page_offset}"
                for page_offset in range(len(plan.toc_pages))
            ]
            chapter_toc_counts[plan.chapter] = first_chapter_toc_number + len(plan.toc_pages) - 1
            sections.append(
                {
                    "title": plan.title,
                    "chapter": plan.chapter,
                    "source_start_page": plan.source_start_page,
                    "source_end_page": plan.source_end_page,
                    "toc_pages": section_pages,
                    "toc_labels": toc_labels,
                    "entry_count": len(plan.headings),
                    "appended_blank_page": plan.append_blank_page,
                }
            )

            for page_offset, page_headings in enumerate(plan.toc_pages):
                page_label = toc_labels[page_offset]
                _draw_section_toc_page(
                    page=document[first_toc_page - 1 + page_offset],
                    plan=plan,
                    page_headings=page_headings,
                    page_offset=page_offset,
                    page_label=page_label,
                    insertions=insertions,
                    settings=settings,
                )
        _link_global_toc_rows(document, global_rows, plans, insertions)
        linked_eicas_rows, unresolved_eicas_rows = link_eicas_references(
            document,
            excluded_pages=inserted_page_indices,
        )
        document.set_toc(_build_outline(document, source_toc, plans, insertions))

        if output_pdf.exists():
            output_pdf.unlink()
        document.save(output_pdf, garbage=4, deflate=True)

    return SectionTocResult(
        output_path=output_pdf,
        sections=sections,
        heading_count=sum(len(plan.headings) for plan in plans),
        inserted_page_count=sum(plan.inserted_page_count for plan in plans),
        linked_global_rows=len(global_rows),
        linked_eicas_rows=len(linked_eicas_rows),
        unresolved_eicas_rows=len(unresolved_eicas_rows),
    )


def _build_section_toc_plans(
    document: fitz.Document,
    headings: list[Heading],
    settings: Settings,
) -> list[SectionTocPlan]:
    roots = _section_roots(document)
    if not roots:
        return []

    plans: list[SectionTocPlan] = []
    for index, root in enumerate(roots):
        next_start = roots[index + 1]["page"] if index + 1 < len(roots) else len(document) + 1
        start_page = int(root["page"])
        end_page = max(start_page, next_start - 1)
        chapter = str(root["chapter"])
        source_page = document[start_page - 1]
        body_start_y = _body_start_y(source_page)
        footer_top_y, footer_baseline_y = _footer_positions(source_page)
        root_headings = [
            heading
            for heading in headings
            if (
                start_page <= heading.page <= end_page
                and _heading_chapter(heading) == chapter
            )
        ]
        if not root_headings:
            continue

        root_headings = sorted(root_headings, key=lambda item: (item.page, item.y0 or 0.0, item.title))
        plans.append(
            SectionTocPlan(
                chapter=chapter,
                title=str(root["title"]),
                source_start_page=start_page,
                source_end_page=end_page,
                headings=root_headings,
                toc_pages=_paginate_section_toc_entries(
                    root_headings,
                    body_start_y,
                    source_page.rect.height,
                    footer_top_y,
                    settings,
                ),
                append_blank_page=False,
                body_start_y=body_start_y,
                footer_top_y=footer_top_y,
                footer_baseline_y=footer_baseline_y,
            )
        )

    for plan in plans:
        if plan.inserted_page_count % 2 == 1:
            plan.append_blank_page = True

    return plans


def _find_intentionally_blank_template_page(document: fitz.Document) -> int | None:
    best_page_index: int | None = None
    best_score = -1
    for page_index in range(len(document)):
        page = document[page_index]
        text = " ".join(page.get_text("text").upper().split())
        if "INTENTIONALLY LEFT BLANK" not in text:
            continue

        # Prefer blank templates that already include normal top header content.
        top_words = [
            word
            for word in page.get_text("words")
            if float(word[1]) <= min(110.0, page.rect.height * 0.2)
        ]
        score = 3 if top_words else 1
        if score > best_score:
            best_page_index = page_index
            best_score = score

    return best_page_index


def _section_roots(document: fitz.Document) -> list[dict[str, int | str]]:
    roots: list[dict[str, int | str]] = []
    seen_pages: set[int] = set()

    for level, title, page_number in document.get_toc():
        if level != 1 or page_number < 1 or page_number > len(document):
            continue

        chapter = _section_chapter(document, page_number, title)
        if chapter is None or page_number in seen_pages:
            continue

        seen_pages.add(page_number)
        roots.append(
            {
                "chapter": chapter,
                "page": page_number,
                "title": _section_title(document, page_number, title),
            }
        )

    return sorted(roots, key=lambda item: int(item["page"]))


def _section_chapter(document: fitz.Document, page_number: int, title: str) -> str | None:
    title_match = SECTION_TITLE_CHAPTER_RE.search(title)
    if title_match:
        return title_match.group("chapter")

    page_text = _top_region_text(document[page_number - 1])
    page_match = ATA_RE.search(page_text)
    return page_match.group(1) if page_match else None


def _section_title(document: fitz.Document, page_number: int, fallback_title: str) -> str:
    page_title = derive_bookmark_title(document[page_number - 1])
    fallback_bookmark_title = derive_bookmark_title_from_text(fallback_title)
    if (
        page_title
        and fallback_bookmark_title
        and fallback_bookmark_title.startswith(f"{page_title}_")
    ):
        return fallback_bookmark_title
    return page_title or fallback_bookmark_title or fallback_title


def _heading_chapter(heading: Heading) -> str | None:
    match = ITEM_CHAPTER_RE.match(heading.title)
    return match.group("chapter") if match else None


def _resolve_global_toc_rows(document: fitz.Document) -> list[ExistingTocRow]:
    global_pages = find_existing_toc_pages(document)
    rows = extract_existing_toc_rows(document, global_pages)
    if not rows:
        return []

    label_index = build_page_label_index(document, global_pages)
    outline_index = build_outline_title_index(document)
    resolved: list[ExistingTocRow] = []
    for row in rows:
        row.target_label = infer_target_label(row)
        row.target_page_index = resolve_target_page(row, label_index, outline_index)
        if row.target_page_index is not None:
            resolved.append(row)
    return resolved


def _draw_section_toc_page(
    *,
    page: fitz.Page,
    plan: SectionTocPlan,
    page_headings: list[Heading],
    page_offset: int,
    page_label: str,
    insertions: list[tuple[int, int]],
    settings: Settings,
) -> None:
    _prepare_section_toc_template(page, plan, page_label, settings)

    width = page.rect.width
    right_x = width - settings.toc_margin_x
    _draw_toc_side_bar(
        page=page,
        page_number_in_section_toc=page_offset + 1,
        content_left_x=settings.toc_margin_x,
        content_right_x=right_x,
        top_y=plan.body_start_y + 3.0,
        bottom_y=max(plan.body_start_y + 24.0, plan.footer_top_y - 6.0),
    )
    if page_offset == 0:
        title = "Table of Contents"
        title_font_size = 11.0
        title_width = fitz.get_text_length(title, fontname=settings.toc_font, fontsize=title_font_size)
        page.insert_text(
            ((width - title_width) / 2, plan.body_start_y + 18.0),
            title,
            fontsize=title_font_size,
            fontname=settings.toc_font,
            color=(0, 0, 0),
        )

    y = _first_section_entry_y(plan.body_start_y, page_offset)
    for heading in page_headings:
        target_page_number = _final_original_page(heading.page, insertions)
        indent = max(0, heading.level - 1) * settings.toc_indent_per_level
        x = settings.toc_margin_x + indent
        font_size = max(settings.toc_entry_font_size - (heading.level - 1) * 0.25, 8.5)
        font_name = "Helvetica-Bold" if heading.level == 1 else settings.toc_font
        page_number_text = str(target_page_number)
        page_number_width = fitz.get_text_length(page_number_text, fontname=font_name, fontsize=font_size)
        max_title_width = max(60.0, right_x - x - page_number_width - 16)
        display_title = _truncate_to_width(heading.title, max_title_width, font_name, font_size)
        title_width = fitz.get_text_length(display_title, fontname=font_name, fontsize=font_size)
        dots = _leader_dots(right_x - page_number_width - 8 - (x + title_width), font_name, font_size)

        page.insert_text((x, y), display_title, fontsize=font_size, fontname=font_name, color=(0, 0, 0))
        
        page.insert_link(
            {
                "kind": fitz.LINK_GOTO,
                "from": fitz.Rect(settings.toc_margin_x, y - font_size, right_x, y + 3),
                "page": target_page_number - 1,
                "to": fitz.Point(0, max(0.0, float(heading.y0 or 0.0))),
            }
        )
        y += settings.toc_line_height


def _prepare_section_toc_template(
    page: fitz.Page,
    plan: SectionTocPlan,
    page_label: str,
    settings: Settings,
) -> None:
    for link in list(page.get_links()):
        page.delete_link(link)

    body_rect = fitz.Rect(
        36.0,
        max(0.0, plan.body_start_y - 2.0),
        page.rect.width - 36.0,
        max(plan.body_start_y + 20.0, plan.footer_top_y - 4.0),
    )
    footer_rect = fitz.Rect(
        36.0,
        max(0.0, plan.footer_top_y - 3.0),
        page.rect.width - 36.0,
        page.rect.height - 20.0,
    )
    page.add_redact_annot(body_rect, fill=(1, 1, 1))
    page.add_redact_annot(footer_rect, fill=(1, 1, 1))
    page.apply_redactions(
        images=fitz.PDF_REDACT_IMAGE_NONE,
        graphics=fitz.PDF_REDACT_LINE_ART_REMOVE_IF_COVERED,
        text=fitz.PDF_REDACT_TEXT_REMOVE,
    )

    footer_text = page_label
    footer_font_size = 10.0
    footer_width = fitz.get_text_length(footer_text, fontname=settings.toc_font, fontsize=footer_font_size)
    page.insert_text(
        ((page.rect.width - footer_width) / 2, plan.footer_baseline_y),
        footer_text,
        fontsize=footer_font_size,
        fontname=settings.toc_font,
        color=(0, 0, 0),
    )


def _draw_toc_side_bar(
    *,
    page: fitz.Page,
    page_number_in_section_toc: int,
    content_left_x: float,
    content_right_x: float,
    top_y: float,
    bottom_y: float,
) -> None:
    if bottom_y <= top_y:
        return

    if page_number_in_section_toc % 2 == 1:
        x = min(page.rect.width - 8.0, content_right_x + 4.0)
    else:
        # Keep more breathing room from left-side TOC content on even pages.
        x = max(8.0, content_left_x - 10.0)

    page.draw_line(
        fitz.Point(x, top_y),
        fitz.Point(x, bottom_y),
        color=(0, 0, 0),
        width=0.6,
        overlay=True,
    )


def _link_global_toc_rows(
    document: fitz.Document,
    rows: list[ExistingTocRow],
    plans: list[SectionTocPlan],
    insertions: list[tuple[int, int]],
) -> None:
    for row in rows:
        if row.target_page_number is None:
            continue

        source_page_number = row.page_number
        page = document[_final_original_page(source_page_number, insertions) - 1]
        target_page_number = _section_toc_page_for_target(row.target_page_number, plans, insertions)
        _delete_overlapping_links(page, row.rect)
        page.insert_link(
            {
                "kind": fitz.LINK_GOTO,
                "from": row.rect,
                "page": target_page_number - 1,
                "to": fitz.Point(0, 0),
            }
        )


def _build_outline(
    document: fitz.Document,
    source_toc: list[list],
    plans: list[SectionTocPlan],
    insertions: list[tuple[int, int]],
) -> list[list[int | str]]:
    plan_by_start_page = {plan.source_start_page: plan for plan in plans}
    outline: list[list[int | str]] = []
    skip_nested_section_items = False

    for level, title, page_number in source_toc:
        if page_number < 1:
            continue

        if level == 1:
            plan = plan_by_start_page.get(page_number)
            skip_nested_section_items = plan is not None
            if plan is None:
                outline.append([1, _source_outline_title(document, page_number, title), _final_original_page(page_number, insertions)])
                continue

            first_toc_page = _first_toc_final_page(plan, insertions)
            outline.append([1, plan.title, first_toc_page])
            outline.extend(_outline_entries_for_plan(plan, insertions))
            continue

        if skip_nested_section_items:
            continue

        outline.append([level, title, _final_original_page(page_number, insertions)])

    if not outline:
        for plan in plans:
            outline.append([1, plan.title, _first_toc_final_page(plan, insertions)])
            outline.extend(_outline_entries_for_plan(plan, insertions))

    return outline


def _outline_entries_for_plan(
    plan: SectionTocPlan,
    insertions: list[tuple[int, int]],
) -> list[list[int | str]]:
    entries: list[list[int | str]] = []
    previous_level = 1
    for heading in plan.headings:
        level = max(2, min(heading.level + 1, 6))
        if level > previous_level + 1:
            level = previous_level + 1
        entries.append([level, heading.title, _final_original_page(heading.page, insertions)])
        previous_level = level
    return entries


def _source_outline_title(document: fitz.Document, page_number: int, title: str) -> str:
    if page_number < 1 or page_number > len(document):
        return title
    return (
        derive_bookmark_title(document[page_number - 1])
        or derive_bookmark_title_from_text(title)
        or title
    )


def _section_toc_page_for_target(
    target_page_number: int,
    plans: list[SectionTocPlan],
    insertions: list[tuple[int, int]],
) -> int:
    for plan in plans:
        if plan.source_start_page <= target_page_number <= plan.source_end_page:
            return _first_toc_final_page(plan, insertions)
    return _final_original_page(target_page_number, insertions)


def _paginate_section_toc_entries(
    headings: list[Heading],
    body_start_y: float,
    page_height: float,
    footer_top_y: float,
    settings: Settings,
) -> list[list[Heading]]:
    pages: list[list[Heading]] = [[]]
    y = _first_section_entry_y(body_start_y, 0)
    bottom_y = min(page_height - settings.toc_margin_bottom, footer_top_y - 12.0)

    for heading in headings:
        if pages[-1] and y + settings.toc_line_height > bottom_y:
            pages.append([])
            y = _first_section_entry_y(body_start_y, len(pages) - 1)
        pages[-1].append(heading)
        y += settings.toc_line_height

    return pages


def _first_section_entry_y(body_start_y: float, page_offset: int) -> float:
    if page_offset == 0:
        return body_start_y + 43.0
    return body_start_y + 15.0


def _first_toc_final_page(plan: SectionTocPlan, insertions: list[tuple[int, int]]) -> int:
    return plan.source_start_page + sum(count for start, count in insertions if start < plan.source_start_page)


def _final_original_page(page_number: int, insertions: list[tuple[int, int]]) -> int:
    return page_number + sum(count for start, count in insertions if start <= page_number)


def _top_region_text(page: fitz.Page) -> str:
    words = [
        word
        for word in page.get_text("words")
        if float(word[1]) <= min(180.0, page.rect.height * 0.28)
    ]
    return " ".join(str(word[4]) for word in sorted(words, key=lambda item: (item[1], item[0])))


def _body_start_y(page: fitz.Page) -> float:
    words = page.get_text("words")
    starts: list[float] = []
    for word in words:
        text = str(word[4]).strip().upper()
        if text in {"CATEGORY", "ITEM", "DESCRIPTION", "REMARKS"}:
            y0 = float(word[1])
            if 80.0 <= y0 <= page.rect.height * 0.35:
                starts.append(y0)
    if starts:
        return max(86.0, min(starts) - 4.0)
    return min(112.0, page.rect.height * 0.18)


def _footer_positions(page: fitz.Page) -> tuple[float, float]:
    footer_words = [
        word
        for word in page.get_text("words")
        if float(word[1]) >= page.rect.height - 80.0
    ]
    if not footer_words:
        return page.rect.height - 50.0, page.rect.height - 39.0

    footer_top = min(float(word[1]) for word in footer_words)
    footer_bottom = max(float(word[3]) for word in footer_words)
    return footer_top, footer_bottom - 1.0


def _delete_overlapping_links(page: fitz.Page, rect: fitz.Rect) -> None:
    for link in list(page.get_links()):
        link_rect = fitz.Rect(link["from"])
        if link_rect.intersects(rect):
            page.delete_link(link)


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

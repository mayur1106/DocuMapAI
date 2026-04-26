from __future__ import annotations

from dataclasses import replace
from datetime import datetime

import fitz

from app.config import Settings, get_settings
from app.models import (
    DocumentRecord,
    Heading,
    TocDryRunChangedPage,
    TocDryRunLepAction,
    TocDryRunLepReport,
    TocRevisionDryRunResponse,
)
from app.services.existing_toc_linker import (
    ExistingTocRow,
    build_item_label_index,
    build_outline_title_index,
    build_page_label_index,
    extract_eicas_reference_rows,
    extract_existing_toc_rows,
    extract_local_toc_rows,
    extract_reference_labels,
    find_eicas_reference_pages,
    find_existing_toc_pages,
    find_local_toc_pages,
    infer_target_label,
    normalize_label,
    resolve_target_page,
)
from app.services.heading_detector import detect_headings
from app.services.mel_table_extractor import extract_mel_table_headings
from app.services.pdf_parser import extract_lines
from app.services.pdf_writer import normalize_heading_levels
from app.services.section_toc_writer import _build_section_toc_plans
from app.services.toc_builder import paginate_toc_entries


def build_toc_revision_dry_run(
    record: DocumentRecord,
    *,
    revision: str | None = None,
    revision_date: str | None = None,
    track_link_repair_revision: bool = False,
    settings: Settings | None = None,
) -> TocRevisionDryRunResponse:
    settings = settings or get_settings()
    warnings: list[str] = []
    changed_pages: list[TocDryRunChangedPage] = []
    annotation_only_pages: set[int] = set()
    linkable_rows = 0
    unresolved_rows = 0
    heading_source = "none"
    heading_count = 0
    inserted_page_count = 0
    mode = "unknown"
    safe_to_apply = False

    with fitz.open(record.original_path) as document:
        page_count = len(document)
        global_toc_pages = find_existing_toc_pages(document)
        local_toc_pages = find_local_toc_pages(document)
        eicas_reference_pages = find_eicas_reference_pages(document)
        headings: list[Heading] = []

        if global_toc_pages and not local_toc_pages:
            headings = extract_mel_table_headings(record.original_path)
            heading_source = "mel_table"
            heading_count = len(headings)
            plans = _build_section_toc_plans(document, headings, settings) if headings else []
            if plans:
                mode = "insert_missing_section_tocs"
                safe_to_apply = True
                insertions = [(plan.source_start_page, plan.inserted_page_count) for plan in plans]
                inserted_page_count = sum(plan.inserted_page_count for plan in plans)
                for plan in plans:
                    first_final_page = _first_toc_final_page(plan.source_start_page, insertions)
                    for page_offset, page_entries in enumerate(plan.toc_pages):
                        changed_pages.append(
                            _changed_page(
                                change_type="insert_page",
                                page_label=f"TOC {plan.chapter}-{page_offset + 1}",
                                reason="missing_chapter_toc",
                                revision=revision,
                                revision_date=revision_date,
                                final_page_number=first_final_page + page_offset,
                                insert_before_page=plan.source_start_page,
                            )
                        )
                        if not page_entries:
                            warnings.append(f"Planned TOC {plan.chapter}-{page_offset + 1} has no entries.")

                linkable_rows, unresolved_rows, annotation_pages, simulation_warnings = _simulate_section_toc_linking(
                    document,
                    global_toc_pages,
                    eicas_reference_pages,
                    plans,
                )
                warnings.extend(simulation_warnings)
                annotation_only_pages.update(annotation_pages)
                changed_pages.extend(
                    _annotation_changed_pages(document, annotation_pages, revision, revision_date, track_link_repair_revision)
                )
            else:
                warnings.append("Global TOC pages were found, but no missing chapter TOC plan could be built.")

        if mode == "unknown" and (global_toc_pages or local_toc_pages or eicas_reference_pages):
            mode = "repair_existing_toc_links"
            linked_rows, unresolved, annotation_pages, simulation_warnings = _simulate_existing_toc_linking(
                document,
                global_toc_pages,
                local_toc_pages,
                eicas_reference_pages,
            )
            warnings.extend(simulation_warnings)
            linkable_rows = len(linked_rows)
            unresolved_rows = len(unresolved)
            annotation_only_pages.update(annotation_pages)
            safe_to_apply = bool(linked_rows)
            changed_pages.extend(
                _annotation_changed_pages(document, annotation_pages, revision, revision_date, track_link_repair_revision)
            )
            if not track_link_repair_revision:
                warnings.append(
                    "Existing TOC mode changes link annotations only. Revision/LEP updates are not planned unless track_link_repair_revision=true."
                )
            if unresolved:
                warnings.append(f"{len(unresolved)} TOC/reference rows could not be resolved during dry run.")

        if mode == "unknown":
            headings = extract_mel_table_headings(record.original_path)
            heading_source = "mel_table"
            if not headings:
                lines = extract_lines(str(record.original_path))
                headings = detect_headings(lines, settings)
                heading_source = "layout_detection"
            heading_count = len(headings)
            if headings:
                mode = f"generate_{heading_source}_toc"
                safe_to_apply = True
                toc_pages = paginate_toc_entries(normalize_heading_levels(headings), document[0].rect.height, settings)
                inserted_page_count = len(toc_pages)
                for page_offset in range(inserted_page_count):
                    changed_pages.append(
                        _changed_page(
                            change_type="insert_page",
                            page_label=f"TOC-{page_offset + 1}",
                            reason="new_front_toc",
                            revision=revision,
                            revision_date=revision_date,
                            final_page_number=page_offset + 1,
                            insert_before_page=1,
                        )
                    )
            else:
                mode = "no_reliable_toc_source"
                warnings.append("No existing TOC pages and no reliable MEL table rows or headings were detected.")

        lep_report = _build_lep_report(document, changed_pages)
        changed_pages = [
            page.model_copy(update={"lep_action": _lep_action_for_label(page.page_label, lep_report)})
            for page in changed_pages
        ]

    return TocRevisionDryRunResponse(
        document_id=record.id,
        filename=record.original_filename,
        page_count=page_count,
        mode=mode,
        safe_to_apply=safe_to_apply,
        mutates_pdf=False,
        revision=revision or "auto_next_chapter_revision",
        revision_date=revision_date or datetime.now().strftime("%b %Y").upper(),
        track_link_repair_revision=track_link_repair_revision,
        existing_global_toc_pages=[page + 1 for page in global_toc_pages],
        existing_local_toc_pages=[page + 1 for page in local_toc_pages],
        eicas_reference_pages=[page + 1 for page in eicas_reference_pages],
        heading_source=heading_source,
        heading_count=heading_count,
        inserted_page_count=inserted_page_count,
        changed_pages=changed_pages,
        annotation_only_pages=sorted(annotation_only_pages),
        linkable_rows=linkable_rows,
        unresolved_rows=unresolved_rows,
        lep=lep_report,
        warnings=warnings,
    )


def _simulate_existing_toc_linking(
    document: fitz.Document,
    global_toc_pages: list[int],
    local_toc_pages: list[int],
    reference_pages: list[int],
) -> tuple[list[ExistingTocRow], list[ExistingTocRow], set[int], list[str]]:
    toc_pages = sorted(set(global_toc_pages + local_toc_pages))
    rows = extract_existing_toc_rows(document, global_toc_pages)
    rows.extend(extract_local_toc_rows(document, local_toc_pages))
    eicas_rows, warnings = _extract_eicas_rows_safely(document, reference_pages)
    rows.extend(eicas_rows)

    label_index = build_page_label_index(document, global_toc_pages)
    item_index = build_item_label_index(document, toc_pages)
    outline_index = build_outline_title_index(document)
    linked_rows: list[ExistingTocRow] = []
    unresolved_rows: list[ExistingTocRow] = []

    for row in rows:
        row = replace(row)
        if row.toc_type in {"local", "reference"}:
            location = item_index.get(normalize_label(row.target_label or ""))
            if location is not None:
                row.target_page_index = location[0]
                row.target_y = location[1]
        else:
            row.target_label = infer_target_label(row)
            row.target_page_index = resolve_target_page(row, label_index, outline_index)

        if row.target_page_index is None:
            unresolved_rows.append(row)
        else:
            linked_rows.append(row)

    return linked_rows, unresolved_rows, {row.page_number for row in linked_rows}, warnings


def _simulate_section_toc_linking(
    document: fitz.Document,
    global_toc_pages: list[int],
    reference_pages: list[int],
    plans,
) -> tuple[int, int, set[int], list[str]]:
    rows = extract_existing_toc_rows(document, global_toc_pages)
    if not rows:
        rows = []
    label_index = build_page_label_index(document, global_toc_pages)
    outline_index = build_outline_title_index(document)
    linked = 0
    unresolved = 0
    annotation_pages: set[int] = set()
    plan_chapters = {str(plan.chapter) for plan in plans}
    for row in rows:
        row = replace(row)
        row.target_label = infer_target_label(row)
        if row.target_label and row.target_label.upper().startswith("TOC "):
            target_chapter = row.target_label.split(" ", 1)[1].split("-", 1)[0]
            if target_chapter in plan_chapters:
                linked += 1
                annotation_pages.add(row.page_number)
                continue
        row.target_page_index = resolve_target_page(row, label_index, outline_index)
        if row.target_page_index is None:
            unresolved += 1
        else:
            linked += 1
            annotation_pages.add(row.page_number)

    eicas_linked, eicas_unresolved, eicas_pages, warnings = _simulate_eicas_reference_linking(
        document,
        reference_pages,
        excluded_pages=[],
    )
    return linked + eicas_linked, unresolved + eicas_unresolved, annotation_pages | eicas_pages, warnings


def _simulate_eicas_reference_linking(
    document: fitz.Document,
    reference_pages: list[int],
    *,
    excluded_pages: list[int],
) -> tuple[int, int, set[int], list[str]]:
    rows, warnings = _extract_eicas_rows_safely(document, reference_pages)
    if not rows:
        return 0, 0, set(), warnings

    item_index = build_item_label_index(document, excluded_pages)
    linked = 0
    unresolved = 0
    annotation_pages: set[int] = set()
    for row in rows:
        location = item_index.get(normalize_label(row.target_label or ""))
        if location is None:
            unresolved += 1
            continue

        linked += 1
        annotation_pages.add(row.page_number)

    return linked, unresolved, annotation_pages, warnings


def _extract_eicas_rows_safely(
    document: fitz.Document,
    reference_pages: list[int],
) -> tuple[list[ExistingTocRow], list[str]]:
    if not reference_pages:
        return [], []
    try:
        return extract_eicas_reference_rows(document, reference_pages), []
    except RuntimeError as exc:
        return [], [f"EICAS reference dry run skipped because OCR is unavailable or failed: {exc}"]


def _annotation_changed_pages(
    document: fitz.Document,
    page_numbers: set[int],
    revision: str | None,
    revision_date: str | None,
    track_link_repair_revision: bool,
) -> list[TocDryRunChangedPage]:
    if not track_link_repair_revision:
        return []
    return [
        _changed_page(
            change_type="repair_links",
            page_label=_page_label(document[page_number - 1], page_number),
            reason="annotation_only_link_repair",
            revision=revision,
            revision_date=revision_date,
            page_number=page_number,
            final_page_number=page_number,
        )
        for page_number in sorted(page_numbers)
    ]


def _changed_page(
    *,
    change_type: str,
    page_label: str,
    reason: str,
    revision: str | None,
    revision_date: str | None,
    page_number: int | None = None,
    final_page_number: int | None = None,
    insert_before_page: int | None = None,
) -> TocDryRunChangedPage:
    revision_action = "set_revision_and_date" if revision and revision_date else "auto_next_chapter_revision_current_month"
    return TocDryRunChangedPage(
        change_type=change_type,
        page_label=page_label,
        reason=reason,
        page_number=page_number,
        final_page_number=final_page_number,
        insert_before_page=insert_before_page,
        revision_action=revision_action,
    )


def _build_lep_report(document: fitz.Document, changed_pages: list[TocDryRunChangedPage]) -> TocDryRunLepReport:
    lep_pages = _find_lep_pages(document)
    lep_labels: set[str] = set()
    notes: list[str] = []
    for page_index in lep_pages:
        lep_labels.update(_extract_lep_labels(document[page_index]))

    actions: list[TocDryRunLepAction] = []
    for page in changed_pages:
        normalized = normalize_label(page.page_label)
        matched = normalized in lep_labels
        if not lep_pages:
            action = "manual_review_no_lep"
        elif matched:
            action = "update_row"
        else:
            action = "insert_row"
        actions.append(TocDryRunLepAction(page_label=page.page_label, action=action, matched=matched))

    missing_count = sum(1 for action in actions if action.action == "insert_row")
    if not lep_pages and changed_pages:
        notes.append("No LEP pages were detected. LEP update cannot be planned automatically yet.")
        overflow_risk = "manual_review"
    elif missing_count > 12:
        overflow_risk = "high"
        notes.append("Many new LEP rows are needed. A continuation LEP page may be required.")
    elif missing_count:
        overflow_risk = "medium"
    else:
        overflow_risk = "low"

    return TocDryRunLepReport(
        detected=bool(lep_pages),
        pages=[page + 1 for page in lep_pages],
        detected_row_count=len(lep_labels),
        matched_change_count=sum(1 for action in actions if action.matched),
        missing_change_count=missing_count,
        overflow_risk=overflow_risk,
        actions=actions,
        notes=notes,
    )


def _find_lep_pages(document: fitz.Document) -> list[int]:
    pages: list[int] = []
    for page_index, page in enumerate(document):
        upper_text = page.get_text("text").upper()
        if "LIST OF EFFECTIVE PAGES" in upper_text:
            pages.append(page_index)
            continue
        if "EFFECTIVE PAGES" in upper_text and ("REV" in upper_text or "DATE" in upper_text):
            pages.append(page_index)
    return pages


def _extract_lep_labels(page: fitz.Page) -> set[str]:
    labels: set[str] = set()
    for line in _word_lines(page):
        text = " ".join(str(word[4]) for word in line)
        for label in extract_reference_labels(text):
            labels.add(normalize_label(label))
    return labels


def _page_label(page: fitz.Page, page_number: int) -> str:
    words = [
        word
        for word in page.get_text("words")
        if float(word[1]) <= min(180.0, page.rect.height * 0.28)
        or float(word[1]) >= max(page.rect.height - 130.0, page.rect.height * 0.78)
    ]
    text = " ".join(str(word[4]) for word in sorted(words, key=lambda item: (item[1], item[0])))
    labels = extract_reference_labels(text)
    return labels[0] if labels else f"PDF page {page_number}"


def _word_lines(page: fitz.Page) -> list[list[tuple]]:
    grouped: dict[tuple[int, int], list[tuple]] = {}
    for word in page.get_text("words"):
        text = str(word[4]).strip()
        if not text:
            continue
        grouped.setdefault((int(word[5]), int(word[6])), []).append(word)
    return [
        sorted(words, key=lambda item: float(item[0]))
        for words in sorted(grouped.values(), key=lambda words: (min(float(word[1]) for word in words), min(float(word[0]) for word in words)))
    ]


def _first_toc_final_page(source_start_page: int, insertions: list[tuple[int, int]]) -> int:
    return source_start_page + sum(count for start, count in insertions if start < source_start_page)


def _lep_action_for_label(page_label: str, lep_report: TocDryRunLepReport) -> str:
    for action in lep_report.actions:
        if action.page_label == page_label:
            return action.action
    return "not_evaluated"

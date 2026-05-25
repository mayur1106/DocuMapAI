from __future__ import annotations

import fitz

from app.config import get_settings
from app.models import DocumentStatus, Heading
from app.services.existing_toc_linker import (
    ExistingTocLinkResult,
    find_existing_toc_pages,
    find_local_toc_pages,
    hyperlink_existing_toc,
)
from app.services.heading_detector import detect_headings
from app.services.mel_table_extractor import extract_mel_table_headings
from app.services.pdf_parser import extract_lines
from app.services.pdf_writer import write_pdf_with_toc
from app.services.process_logs import append_stream, unresolved_report_path, write_unresolved_report
from app.services.section_toc_writer import (
    has_missing_section_toc_pattern,
    write_pdf_with_section_tocs,
)
from app.services.storage import get_document, log_activity, save_toc, update_document_status
from app.services.toc_builder import flatten_toc
from app.utils.logger import get_logger


logger = get_logger(__name__)


def generate_toc_task(document_id: str) -> dict:
    settings = get_settings()
    record = _get_document_for_worker(document_id, settings)
    update_document_status(document_id, DocumentStatus.PROCESSING, error=None, settings=settings)
    log_activity(
        action="toc_processing_started",
        status="info",
        message=f"Started TOC processing for {record.original_filename}.",
        document=record,
        settings=settings,
    )
    logger.info("Processing TOC for document %s", document_id)
    append_stream(document_id, "processing_started")
    report_path = unresolved_report_path(document_id, settings)
    if report_path.exists():
        report_path.unlink()

    try:
        output_path = settings.output_dir / f"{document_id}_with_toc.pdf"
        global_toc_pages, local_toc_pages = _existing_toc_pages(record.original_path)
        headings: list[Heading] | None = None

        if global_toc_pages and not local_toc_pages:
            append_stream(document_id, "mode_check: global_toc_present local_toc_missing")
            headings = extract_mel_table_headings(record.original_path)
            if headings and has_missing_section_toc_pattern(record.original_path, headings, settings):
                result = write_pdf_with_section_tocs(
                    record.original_path,
                    output_path,
                    headings,
                    settings,
                )
                toc_path = save_toc(document_id, headings, settings)
                update_document_status(
                    document_id,
                    DocumentStatus.READY,
                    output_path=output_path,
                    toc_path=toc_path,
                    error=None,
                    settings=settings,
                )
                logger.info(
                    "Inserted missing section TOCs for document %s with %d sections and %d entries",
                    document_id,
                    len(result.sections),
                    result.heading_count,
                )
                append_stream(
                    document_id,
                    f"completed_inserted_section_tocs sections={len(result.sections)} heading_count={result.heading_count}",
                )
                log_activity(
                    action="toc_processing_completed",
                    status="success",
                    message=f"Completed section TOC insertion for {record.original_filename}.",
                    document=record,
                    metadata={
                        "mode": "inserted_section_tocs",
                        "heading_count": result.heading_count,
                        "revision_update": result.revision_update,
                    },
                    settings=settings,
                )
                return {
                    "document_id": document_id,
                    "mode": "inserted_section_tocs",
                    **result.to_dict(),
                }

        if global_toc_pages or local_toc_pages:
            append_stream(document_id, "mode_check: existing_toc_detected linking")
            result = hyperlink_existing_toc(
                record.original_path,
                output_path,
            )
            headings = _headings_from_existing_toc(result)
            unresolved_report = None
            if result.unresolved_rows:
                report_path = write_unresolved_report(document_id, result.unresolved_rows, settings)
                unresolved_report = str(report_path)
                append_stream(
                    document_id,
                    f"unresolved_links={len(result.unresolved_rows)} report={report_path.name}",
                )
            toc_path = save_toc(document_id, headings, settings)
            update_document_status(
                document_id,
                DocumentStatus.READY,
                output_path=output_path,
                toc_path=toc_path,
                error=None,
                settings=settings,
            )
            logger.info(
                "Linked existing TOC for document %s with %d links and %d unresolved rows",
                document_id,
                len(result.linked_rows),
                len(result.unresolved_rows),
            )
            log_activity(
                action="toc_processing_completed",
                status="success",
                message=f"Completed existing TOC linking for {record.original_filename}.",
                document=record,
                metadata={
                    "mode": "linked_existing_toc",
                    "heading_count": len(headings),
                    "linked_rows": len(result.linked_rows),
                    "unresolved_rows": len(result.unresolved_rows),
                    "revision_update": result.revision_update,
                },
                settings=settings,
            )
            return {
                "document_id": document_id,
                "mode": "linked_existing_toc",
                "heading_count": len(headings),
                "unresolved_report": unresolved_report,
                **result.to_dict(),
            }

        headings = headings if headings is not None else extract_mel_table_headings(record.original_path)
        mode = "generated_mel_table_toc"
        if not headings:
            lines = extract_lines(str(record.original_path))
            headings = detect_headings(lines, settings)
            mode = "generated_layout_toc"
        if not headings:
            raise ValueError("No reliable MEL table rows or headings were detected in this PDF.")

        write_pdf_with_toc(
            record.original_path,
            output_path,
            headings,
            settings,
        )
        toc_path = save_toc(document_id, headings, settings)
        update_document_status(
            document_id,
            DocumentStatus.READY,
            output_path=output_path,
            toc_path=toc_path,
            error=None,
            settings=settings,
        )
        logger.info("Generated %s for document %s with %d entries", mode, document_id, len(headings))
        append_stream(document_id, f"completed_{mode} heading_count={len(headings)}")
        log_activity(
            action="toc_processing_completed",
            status="success",
            message=f"Completed {mode} for {record.original_filename}.",
            document=record,
            metadata={"mode": mode, "heading_count": len(headings), "output_path": str(output_path)},
            settings=settings,
        )
        return {
            "document_id": document_id,
            "mode": mode,
            "heading_count": len(headings),
            "output_path": str(output_path),
            "toc": flatten_toc(headings),
        }
    except Exception as exc:
        append_stream(document_id, f"processing_failed error={exc}")
        _mark_document_failed(document_id, exc, settings)
        log_activity(
            action="toc_processing_failed",
            status="error",
            message=f"TOC processing failed for document {document_id}.",
            document_id=document_id,
            metadata={"error": str(exc)},
            settings=settings,
        )
        logger.exception("Failed to generate TOC for document %s", document_id)
        raise


def _existing_toc_pages(pdf_path) -> tuple[list[int], list[int]]:
    with fitz.open(pdf_path) as document:
        return find_existing_toc_pages(document), find_local_toc_pages(document)


def _headings_from_existing_toc(result: ExistingTocLinkResult) -> list[Heading]:
    headings: list[Heading] = []
    rows = sorted(result.linked_rows, key=lambda row: (row.page_index, row.rect.y0, row.rect.x0))
    for row in rows:
        if row.toc_type == "reference" or row.target_page_number is None:
            continue
        headings.append(
            Heading(
                title=row.title,
                level=row.level,
                page=row.target_page_number,
                confidence=1.0,
                source=f"existing_{row.toc_type}_toc",
                x0=row.rect.x0,
                y0=row.target_y,
            )
        )
    return headings


def hyperlink_existing_toc_task(document_id: str) -> dict:
    settings = get_settings()
    record = _get_document_for_worker(document_id, settings)
    update_document_status(document_id, DocumentStatus.PROCESSING, error=None, settings=settings)
    log_activity(
        action="hyperlink_processing_started",
        status="info",
        message=f"Started existing TOC hyperlinking for {record.original_filename}.",
        document=record,
        settings=settings,
    )
    logger.info("Hyperlinking existing TOC for document %s", document_id)
    append_stream(document_id, "hyperlink_processing_started")
    report_path = unresolved_report_path(document_id, settings)
    if report_path.exists():
        report_path.unlink()

    try:
        output_path = settings.output_dir / f"{document_id}_linked_toc.pdf"
        result = hyperlink_existing_toc(
            record.original_path,
            output_path,
        )
        unresolved_report = None
        if result.unresolved_rows:
            report_path = write_unresolved_report(document_id, result.unresolved_rows, settings)
            unresolved_report = str(report_path)
            append_stream(
                document_id,
                f"unresolved_links={len(result.unresolved_rows)} report={report_path.name}",
            )
        update_document_status(
            document_id,
            DocumentStatus.READY,
            output_path=output_path,
            error=None,
            settings=settings,
        )
        logger.info(
            "Hyperlinked existing TOC for document %s with %d links and %d unresolved rows",
            document_id,
            len(result.linked_rows),
            len(result.unresolved_rows),
        )
        log_activity(
            action="hyperlink_processing_completed",
            status="success",
            message=f"Completed existing TOC hyperlinking for {record.original_filename}.",
            document=record,
            metadata={
                "linked_rows": len(result.linked_rows),
                "unresolved_rows": len(result.unresolved_rows),
                "revision_update": result.revision_update,
            },
            settings=settings,
        )
        return {
            "document_id": document_id,
            "unresolved_report": unresolved_report,
            **result.to_dict(),
        }
    except Exception as exc:
        append_stream(document_id, f"hyperlink_processing_failed error={exc}")
        _mark_document_failed(document_id, exc, settings)
        log_activity(
            action="hyperlink_processing_failed",
            status="error",
            message=f"Existing TOC hyperlinking failed for document {document_id}.",
            document_id=document_id,
            metadata={"error": str(exc)},
            settings=settings,
        )
        logger.exception("Failed to hyperlink existing TOC for document %s", document_id)
        raise


def _get_document_for_worker(document_id: str, settings):
    try:
        return get_document(document_id, settings)
    except KeyError as exc:
        message = (
            f"Document {document_id} was not found in worker storage. "
            "Ensure the API container mounts the expected /app/data volume."
        )
        logger.error(message)
        raise FileNotFoundError(message) from exc


def _mark_document_failed(document_id: str, exc: Exception, settings) -> None:
    try:
        update_document_status(document_id, DocumentStatus.FAILED, error=str(exc), settings=settings)
    except KeyError:
        logger.error("Unable to mark missing document %s as failed", document_id)

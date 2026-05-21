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
from app.services.process_diagnostics import (
    append_process_stream_line,
    initialize_process_stream,
    summarize_heading_stats,
    summarize_link_stats,
    write_process_report,
)
from app.services.section_toc_writer import (
    has_missing_section_toc_pattern,
    write_pdf_with_section_tocs,
)
from app.services.storage import get_document, log_activity, save_process_log, save_toc, update_document_status
from app.services.toc_builder import flatten_toc
from app.utils.logger import get_logger


logger = get_logger(__name__)


def generate_toc_task(
    document_id: str,
    *,
    track_link_repair_revision: bool | None = None,
    revision: str | None = None,
    revision_date: str | None = None,
    **_: object,
) -> dict:
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
    preview_stream_path = initialize_process_stream(
        document_id=document_id,
        output_dir=settings.output_dir,
        mode="generate_toc",
        source_filename=record.original_filename,
    )
    start_report_path = write_process_report(
        document_id=document_id,
        output_dir=settings.output_dir,
        mode="toc_processing_started",
        source_filename=record.original_filename,
        summary={"status": "processing_started"},
    )
    save_process_log(document_id, start_report_path, settings)

    try:
        output_path = settings.output_dir / f"{document_id}_with_toc.pdf"
        global_toc_pages, local_toc_pages = _existing_toc_pages(record.original_path)
        append_process_stream_line(
            preview_stream_path,
            f"detected_toc_pages: global={len(global_toc_pages)}, local={len(local_toc_pages)}",
        )
        headings: list[Heading] | None = None

        if global_toc_pages and not local_toc_pages:
            headings = extract_mel_table_headings(record.original_path)
            append_process_stream_line(preview_stream_path, f"mel_table_headings: {len(headings)}")
            if headings and has_missing_section_toc_pattern(record.original_path, headings, settings):
                result = write_pdf_with_section_tocs(
                    record.original_path,
                    output_path,
                    headings,
                    settings,
                    revision=revision,
                    revision_date=revision_date,
                )
                toc_path = save_toc(document_id, headings, settings)
                process_log_path = write_process_report(
                    document_id=document_id,
                    output_dir=settings.output_dir,
                    mode="inserted_section_tocs",
                    source_filename=record.original_filename,
                    summary={
                        **summarize_heading_stats(headings),
                        "sections_inserted": len(result.sections),
                        "linked_eicas_rows": result.linked_eicas_rows,
                        "unresolved_eicas_rows": result.unresolved_eicas_rows,
                    },
                )
                save_process_log(document_id, process_log_path, settings)
                append_process_stream_line(preview_stream_path, "status: completed_section_toc_insertion")
                update_document_status(
                    document_id,
                    DocumentStatus.READY,
                    output_path=output_path,
                    toc_path=toc_path,
                    process_log_path=process_log_path,
                    error=None,
                    settings=settings,
                )
                logger.info(
                    "Inserted missing section TOCs for document %s with %d sections and %d entries",
                    document_id,
                    len(result.sections),
                    result.heading_count,
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
                    "process_log_path": str(process_log_path),
                    **result.to_dict(),
                }

        if global_toc_pages or local_toc_pages:
            try:
                result = hyperlink_existing_toc(
                    record.original_path,
                    output_path,
                    progress_callback=lambda message: append_process_stream_line(preview_stream_path, message),
                )
                headings = _headings_from_existing_toc(result)
                toc_path = save_toc(document_id, headings, settings)
                process_log_path = write_process_report(
                    document_id=document_id,
                    output_dir=settings.output_dir,
                    mode="linked_existing_toc",
                    source_filename=record.original_filename,
                    summary=summarize_link_stats(result),
                    unresolved_rows=result.unresolved_rows,
                )
                save_process_log(document_id, process_log_path, settings)
                append_process_stream_line(preview_stream_path, "status: completed_existing_toc_linking")
                update_document_status(
                    document_id,
                    DocumentStatus.READY,
                    output_path=output_path,
                    toc_path=toc_path,
                    process_log_path=process_log_path,
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
                    "process_log_path": str(process_log_path),
                    **result.to_dict(),
                }
            except Exception as exc:
                append_process_stream_line(
                    preview_stream_path,
                    f"existing_toc_linking_failed: {exc}; falling back to generated_toc",
                )
                logger.warning("Existing TOC linking failed for %s, falling back to generated TOC: %s", document_id, exc)

        headings = headings if headings is not None else extract_mel_table_headings(record.original_path)
        append_process_stream_line(preview_stream_path, f"mel_table_headings: {len(headings)}")
        mode = "generated_mel_table_toc"
        if not headings:
            lines = extract_lines(str(record.original_path))
            headings = detect_headings(lines, settings)
            mode = "generated_layout_toc"
            append_process_stream_line(preview_stream_path, f"layout_headings: {len(headings)}")
        if not headings:
            raise ValueError("No reliable MEL table rows or headings were detected in this PDF.")

        write_pdf_with_toc(
            record.original_path,
            output_path,
            headings,
            settings,
            revision=revision,
            revision_date=revision_date,
        )
        toc_path = save_toc(document_id, headings, settings)
        process_log_path = write_process_report(
            document_id=document_id,
            output_dir=settings.output_dir,
            mode=mode,
            source_filename=record.original_filename,
            summary=summarize_heading_stats(headings),
        )
        save_process_log(document_id, process_log_path, settings)
        append_process_stream_line(preview_stream_path, f"status: completed_{mode}")
        update_document_status(
            document_id,
            DocumentStatus.READY,
            output_path=output_path,
            toc_path=toc_path,
            process_log_path=process_log_path,
            error=None,
            settings=settings,
        )
        logger.info("Generated %s for document %s with %d entries", mode, document_id, len(headings))
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
            "process_log_path": str(process_log_path),
            "toc": flatten_toc(headings),
        }
    except Exception as exc:
        try:
            failure_log_path = write_process_report(
                document_id=document_id,
                output_dir=settings.output_dir,
                mode="toc_processing_failed",
                source_filename=record.original_filename,
                summary={"error": str(exc)},
            )
            save_process_log(document_id, failure_log_path, settings)
        except Exception:
            logger.exception("Failed to write process log for failed TOC job %s", document_id)
        _mark_document_failed(document_id, exc, settings)
        append_process_stream_line(preview_stream_path, f"status: failed, error: {exc}")
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


def hyperlink_existing_toc_task(
    document_id: str,
    *,
    track_link_repair_revision: bool | None = None,
    revision: str | None = None,
    revision_date: str | None = None,
    **_: object,
) -> dict:
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
    preview_stream_path = initialize_process_stream(
        document_id=document_id,
        output_dir=settings.output_dir,
        mode="hyperlink_existing_toc",
        source_filename=record.original_filename,
    )
    start_report_path = write_process_report(
        document_id=document_id,
        output_dir=settings.output_dir,
        mode="hyperlink_processing_started",
        source_filename=record.original_filename,
        summary={"status": "processing_started"},
    )
    save_process_log(document_id, start_report_path, settings)

    try:
        output_path = settings.output_dir / f"{document_id}_linked_toc.pdf"
        result = hyperlink_existing_toc(
            record.original_path,
            output_path,
            revision=revision,
            revision_date=revision_date,
            track_link_repair_revision=bool(track_link_repair_revision),
            progress_callback=lambda message: append_process_stream_line(preview_stream_path, message),
        )
        process_log_path = write_process_report(
            document_id=document_id,
            output_dir=settings.output_dir,
            mode="hyperlink_existing_toc",
            source_filename=record.original_filename,
            summary=summarize_link_stats(result),
            unresolved_rows=result.unresolved_rows,
        )
        save_process_log(document_id, process_log_path, settings)
        append_process_stream_line(preview_stream_path, "status: completed_hyperlink_existing_toc")
        update_document_status(
            document_id,
            DocumentStatus.READY,
            output_path=output_path,
            process_log_path=process_log_path,
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
            "process_log_path": str(process_log_path),
            **result.to_dict(),
        }
    except Exception as exc:
        try:
            failure_log_path = write_process_report(
                document_id=document_id,
                output_dir=settings.output_dir,
                mode="hyperlink_existing_toc_failed",
                source_filename=record.original_filename,
                summary={"error": str(exc)},
            )
            save_process_log(document_id, failure_log_path, settings)
        except Exception:
            logger.exception("Failed to write process log for failed hyperlink job %s", document_id)
        _mark_document_failed(document_id, exc, settings)
        append_process_stream_line(preview_stream_path, f"status: failed, error: {exc}")
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
            "Ensure the API and worker containers mount the same /app/data volume and use the same Redis queue."
        )
        logger.error(message)
        raise FileNotFoundError(message) from exc


def _mark_document_failed(document_id: str, exc: Exception, settings) -> None:
    try:
        update_document_status(document_id, DocumentStatus.FAILED, error=str(exc), settings=settings)
    except KeyError:
        logger.error("Unable to mark missing document %s as failed", document_id)

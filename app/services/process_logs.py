from __future__ import annotations

from pathlib import Path

from openpyxl import Workbook

from app.config import Settings, get_settings
from app.services.existing_toc_linker import ExistingTocRow


def process_stream_path(document_id: str, settings: Settings | None = None) -> Path:
    settings = settings or get_settings()
    return settings.output_dir / f"{document_id}_process_stream.log"


def unresolved_report_path(document_id: str, settings: Settings | None = None) -> Path:
    settings = settings or get_settings()
    return settings.output_dir / f"{document_id}_unresolved_links.xlsx"


def append_stream(document_id: str, line: str, settings: Settings | None = None) -> Path:
    path = process_stream_path(document_id, settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line.rstrip() + "\n")
    return path


def read_stream(document_id: str, settings: Settings | None = None, max_chars: int = 16000) -> str:
    path = process_stream_path(document_id, settings)
    if not path.exists():
        raise FileNotFoundError(path)
    text = path.read_text(encoding="utf-8")
    return text[-max_chars:]


def write_unresolved_report(
    document_id: str,
    unresolved_rows: list[ExistingTocRow],
    settings: Settings | None = None,
) -> Path:
    path = unresolved_report_path(document_id, settings)
    path.parent.mkdir(parents=True, exist_ok=True)

    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "Unresolved Links"
    worksheet.append(
        [
            "toc_type",
            "toc_page",
            "title",
            "reference_text",
            "target_label",
            "reason",
        ]
    )
    for row in unresolved_rows:
        worksheet.append(
            [
                row.toc_type,
                row.page_number,
                row.title,
                row.reference_text,
                row.target_label or "",
                row.unresolved_reason or "target_not_found",
            ]
        )
    workbook.save(path)
    return path

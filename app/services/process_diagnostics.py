from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from app.models import Heading
from app.services.existing_toc_linker import ExistingTocLinkResult, ExistingTocRow


def write_process_log(
    *,
    document_id: str,
    output_dir: Path,
    mode: str,
    source_filename: str,
    lines: Iterable[str],
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    log_path = output_dir / f"{document_id}_process.log"
    timestamp = datetime.now(timezone.utc).isoformat()
    with log_path.open("w", encoding="utf-8") as handle:
        handle.write(f"timestamp_utc: {timestamp}\n")
        handle.write(f"document_id: {document_id}\n")
        handle.write(f"source_filename: {source_filename}\n")
        handle.write(f"mode: {mode}\n")
        handle.write("-" * 80 + "\n")
        for line in lines:
            handle.write(f"{line}\n")
    return log_path


def summarize_heading_gaps(headings: list[Heading]) -> list[str]:
    if not headings:
        return ["headings_extracted: 0"]
    per_page: dict[int, int] = {}
    for heading in headings:
        per_page[heading.page] = per_page.get(heading.page, 0) + 1
    sparse_pages = sorted(page for page, count in per_page.items() if count == 1)
    lines = [f"headings_extracted: {len(headings)}", f"pages_with_headings: {len(per_page)}"]
    if sparse_pages:
        lines.append(f"sparse_heading_pages: {', '.join(str(page) for page in sparse_pages[:25])}")
    return lines


def summarize_link_result(result: ExistingTocLinkResult) -> list[str]:
    lines = [
        f"linked_rows: {len(result.linked_rows)}",
        f"unresolved_rows: {len(result.unresolved_rows)}",
    ]
    if result.unresolved_rows:
        lines.append("unresolved_details:")
        for row in result.unresolved_rows[:200]:
            lines.append(_format_unresolved_row(row))
    return lines


def _format_unresolved_row(row: ExistingTocRow) -> str:
    reason = row.unresolved_reason or "target_not_found"
    target = row.target_label or "-"
    return (
        f"  - toc_page={row.page_number} toc_type={row.toc_type} "
        f"title={row.title!r} target_label={target!r} reason={reason}"
    )

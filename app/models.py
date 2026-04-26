from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class DocumentStatus(str, Enum):
    UPLOADED = "uploaded"
    QUEUED = "queued"
    PROCESSING = "processing"
    READY = "ready"
    COMPLETED = "completed"
    FAILED = "failed"


class DocumentWorkflow(str, Enum):
    TOC = "toc"
    XML = "xml"


class JobState(str, Enum):
    QUEUED = "queued"
    STARTED = "started"
    FINISHED = "finished"
    FAILED = "failed"


class LineMetadata(BaseModel):
    text: str
    page_number: int
    font_size: float
    font_name: str
    is_bold: bool
    x0: float
    y0: float
    x1: float
    y1: float
    page_width: float
    page_height: float
    block_number: int
    line_number: int
    line_order: int
    spacing_before: float = 0.0
    spacing_after: float = 0.0
    normalized_text: str = ""


class Heading(BaseModel):
    title: str = Field(description="Detected heading text exactly as extracted from the PDF.")
    level: int = Field(ge=1, le=6, description="Hierarchical TOC level. Level 1 is top-level.")
    page: int = Field(ge=1, description="Original PDF page number before TOC pages are inserted.")
    confidence: float = Field(ge=0.0, le=1.0, description="Detection confidence score from 0.0 to 1.0.")
    source: str = "detected"
    font_size: float | None = None
    font_name: str | None = None
    is_bold: bool | None = None
    x0: float | None = None
    y0: float | None = None


class DocumentRecord(BaseModel):
    id: str
    original_filename: str
    original_path: Path
    workflow: DocumentWorkflow = DocumentWorkflow.TOC
    output_path: Path | None = None
    toc_path: Path | None = None
    xml_path: Path | None = None
    xml_stats_path: Path | None = None
    status: DocumentStatus = DocumentStatus.UPLOADED
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    error: str | None = None

    @model_validator(mode="before")
    @classmethod
    def infer_legacy_workflow(cls, data: Any) -> Any:
        if not isinstance(data, dict) or data.get("workflow"):
            return data

        has_xml = bool(data.get("xml_path"))
        has_toc_output = bool(data.get("output_path") or data.get("toc_path"))
        return {
            **data,
            "workflow": DocumentWorkflow.XML.value if has_xml and not has_toc_output else DocumentWorkflow.TOC.value,
        }


class JobRecord(BaseModel):
    id: str
    document_id: str
    backend: str
    status: JobState = JobState.QUEUED
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    result: dict[str, Any] | None = None
    error: str | None = None


class UploadResponse(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "document_id": "8f6b13f4f9f6439f88a91642f9d3fb1d",
                "filename": "manual.pdf",
                "status": "uploaded",
            }
        }
    )

    document_id: str = Field(description="Unique ID assigned to the uploaded document.")
    filename: str = Field(description="Original filename supplied during upload.")
    status: DocumentStatus = Field(description="Current document processing state.")
    workflow: DocumentWorkflow = Field(default=DocumentWorkflow.TOC, description="Dashboard workflow that owns this upload.")


class DocumentResponse(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "id": "8f6b13f4f9f6439f88a91642f9d3fb1d",
                "filename": "manual.pdf",
                "status": "completed",
                "workflow": "toc",
                "created_at": "2026-04-25T12:00:00Z",
                "updated_at": "2026-04-25T12:05:00Z",
                "page_count": 42,
                "created_by": "System",
                "updated_by": "System",
                "has_output": True,
                "has_toc": True,
                "has_xml": True,
                "error": None,
            }
        }
    )

    id: str = Field(description="Document ID.")
    filename: str = Field(description="Original uploaded filename.")
    status: DocumentStatus = Field(description="Current document processing state.")
    workflow: DocumentWorkflow = Field(default=DocumentWorkflow.TOC, description="Dashboard workflow that owns this upload.")
    created_at: datetime = Field(description="Upload timestamp.")
    updated_at: datetime = Field(description="Last status update timestamp.")
    page_count: int | None = Field(default=None, description="Original PDF page count when readable.")
    created_by: str = Field(default="System", description="Creator label for dashboard display.")
    updated_by: str = Field(default="System", description="Last updater label for dashboard display.")
    has_output: bool = Field(description="Whether a processed PDF is available for download.")
    has_toc: bool = Field(description="Whether TOC JSON is available.")
    has_xml: bool = Field(default=False, description="Whether PDF-to-XML output is available.")
    error: str | None = Field(default=None, description="Failure details when processing fails.")


class GenerateResponse(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "job_id": "f4f26f92e42b4237b47d4c723c8f2876",
                "document_id": "8f6b13f4f9f6439f88a91642f9d3fb1d",
                "status": "queued",
                "backend": "rq",
            }
        }
    )

    job_id: str = Field(description="Background job ID used by the status endpoint.")
    document_id: str = Field(description="Document ID being processed.")
    status: str = Field(description="Initial job status.")
    backend: str = Field(description="Job backend. Usually 'rq', or 'local' when Redis is unavailable.")


class StatusResponse(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "job_id": "f4f26f92e42b4237b47d4c723c8f2876",
                "document_id": "8f6b13f4f9f6439f88a91642f9d3fb1d",
                "status": "finished",
                "backend": "rq",
                "result": {
                    "document_id": "8f6b13f4f9f6439f88a91642f9d3fb1d",
                    "heading_count": 3,
                },
                "error": None,
            }
        }
    )

    job_id: str = Field(description="Background job ID.")
    document_id: str | None = Field(default=None, description="Document ID associated with the job.")
    status: str = Field(description="Current job status.")
    backend: str = Field(description="Job backend used for processing.")
    result: dict[str, Any] | None = Field(default=None, description="Job result when processing is finished.")
    error: str | None = Field(default=None, description="Failure details when processing fails.")


class ActivityLogResponse(BaseModel):
    id: str = Field(description="Unique activity log entry ID.")
    created_at: datetime = Field(description="Activity timestamp.")
    action: str = Field(description="Machine-readable activity name.")
    status: str = Field(description="Activity status such as success, info, warning, or error.")
    message: str = Field(description="Human-readable activity summary.")
    document_id: str | None = Field(default=None, description="Related document ID when available.")
    filename: str | None = Field(default=None, description="Related original file name when available.")
    workflow: DocumentWorkflow | None = Field(default=None, description="Related workflow when available.")
    metadata: dict[str, Any] = Field(default_factory=dict, description="Additional activity details.")


class TocEntryResponse(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "title": "5.13 MEL/CDL",
                "level": 2,
                "page": 42,
                "confidence": 0.91,
            }
        }
    )

    title: str = Field(description="Detected heading title.")
    level: int = Field(ge=1, le=6, description="Hierarchical TOC level.")
    page: int = Field(ge=1, description="Original PDF page number where the heading appears.")
    confidence: float = Field(ge=0.0, le=1.0, description="Heading detection confidence score.")


class TocDryRunChangedPage(BaseModel):
    change_type: str = Field(description="Type of change planned for the page.")
    page_label: str = Field(description="Visible or inferred page label affected by the change.")
    reason: str = Field(description="Why this page is included in the dry run.")
    page_number: int | None = Field(default=None, description="Existing one-based page number when applicable.")
    final_page_number: int | None = Field(default=None, description="Expected one-based page number after planned insertions.")
    insert_before_page: int | None = Field(default=None, description="Original one-based page before which a new page would be inserted.")
    revision_action: str = Field(description="Planned revision/date action for this page.")
    lep_action: str = Field(default="not_evaluated", description="Expected LEP action for this page.")


class TocDryRunLepAction(BaseModel):
    page_label: str = Field(description="Page label evaluated against LEP rows.")
    action: str = Field(description="Expected LEP action, such as update_row, insert_row, or manual_review.")
    matched: bool = Field(description="Whether an existing LEP row was detected.")


class TocDryRunLepReport(BaseModel):
    detected: bool = Field(description="Whether LEP pages were detected.")
    pages: list[int] = Field(description="One-based LEP page numbers.")
    detected_row_count: int = Field(description="Number of label-like LEP rows detected.")
    matched_change_count: int = Field(description="Changed pages with an existing LEP row.")
    missing_change_count: int = Field(description="Changed pages that would need a new LEP row.")
    overflow_risk: str = Field(description="Conservative overflow risk estimate for LEP insertions.")
    actions: list[TocDryRunLepAction] = Field(description="Per-page LEP actions.")
    notes: list[str] = Field(default_factory=list, description="LEP detection notes and caveats.")


class TocRevisionDryRunResponse(BaseModel):
    document_id: str = Field(description="Document ID.")
    filename: str = Field(description="Original uploaded filename.")
    page_count: int = Field(description="Original PDF page count.")
    mode: str = Field(description="Predicted processing mode.")
    safe_to_apply: bool = Field(description="Whether the dry run found enough structure to proceed.")
    mutates_pdf: bool = Field(default=False, description="Always false for dry-run responses.")
    revision: str | None = Field(default=None, description="Revision strategy that would be applied later.")
    revision_date: str | None = Field(default=None, description="Revision date value that would be applied later.")
    track_link_repair_revision: bool = Field(description="Whether annotation-only hyperlink repair should trigger revision/LEP entries.")
    existing_global_toc_pages: list[int] = Field(description="Detected global TOC page numbers.")
    existing_local_toc_pages: list[int] = Field(description="Detected chapter TOC page numbers.")
    eicas_reference_pages: list[int] = Field(description="Detected EICAS reference page numbers.")
    heading_source: str = Field(description="Heading source used for planning.")
    heading_count: int = Field(description="Number of headings or MEL rows available for planning.")
    inserted_page_count: int = Field(description="Number of TOC pages that would be inserted.")
    changed_pages: list[TocDryRunChangedPage] = Field(description="Pages that would require revision/date and LEP consideration.")
    annotation_only_pages: list[int] = Field(description="Pages where link annotations may be repaired without visible content changes.")
    linkable_rows: int = Field(description="Rows that appear resolvable to link targets.")
    unresolved_rows: int = Field(description="Rows that could not be resolved during dry run.")
    lep: TocDryRunLepReport = Field(description="LEP impact report.")
    warnings: list[str] = Field(default_factory=list, description="Dry-run warnings and manual review notes.")


class XmlPageStats(BaseModel):
    page: int = Field(ge=1, description="One-based page number.")
    width: float = Field(description="Page width in PDF points.")
    height: float = Field(description="Page height in PDF points.")
    text_blocks: int = Field(description="Number of text blocks extracted from the page.")
    image_blocks: int = Field(description="Number of image blocks detected on the page.")
    lines: int = Field(description="Number of text lines extracted from the page.")
    spans: int = Field(description="Number of text spans extracted from the page.")
    words: int = Field(description="Approximate number of words extracted from the page.")
    characters: int = Field(description="Number of text characters extracted from the page.")
    links: int = Field(description="Number of link annotations on the page.")
    annotations: int = Field(description="Number of non-link annotations on the page.")


class XmlStatsResponse(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "document_id": "8f6b13f4f9f6439f88a91642f9d3fb1d",
                "filename": "manual.pdf",
                "xml_filename": "8f6b13f4f9f6439f88a91642f9d3fb1d.xml",
                "page_count": 42,
                "text_blocks": 318,
                "image_blocks": 7,
                "lines": 1902,
                "spans": 2218,
                "words": 18322,
                "characters": 112409,
                "links": 0,
                "annotations": 0,
                "fonts": ["Arial", "Arial-BoldMT"],
                "file_size_bytes": 5242880,
                "xml_size_bytes": 2134420,
                "pages": [],
            }
        }
    )

    document_id: str = Field(description="Document ID.")
    filename: str = Field(description="Original uploaded filename.")
    xml_filename: str = Field(description="Generated XML filename.")
    page_count: int = Field(description="Total PDF pages converted.")
    text_blocks: int = Field(description="Total text blocks in the XML.")
    image_blocks: int = Field(description="Total image blocks in the XML.")
    lines: int = Field(description="Total text lines in the XML.")
    spans: int = Field(description="Total text spans in the XML.")
    words: int = Field(description="Approximate total words in the XML.")
    characters: int = Field(description="Total text characters in the XML.")
    links: int = Field(description="Total link annotations in the XML.")
    annotations: int = Field(description="Total non-link annotations in the XML.")
    fonts: list[str] = Field(description="Font names observed in extracted text spans.")
    file_size_bytes: int = Field(description="Original PDF file size.")
    xml_size_bytes: int = Field(description="Generated XML file size.")
    pages: list[XmlPageStats] = Field(description="Per-page extraction statistics.")


class XmlConversionResponse(XmlStatsResponse):
    xml_path: str = Field(description="Server-side path of the generated XML file.")


class ErrorResponse(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "detail": "Document not found.",
            }
        }
    )

    detail: str = Field(description="Human-readable error message.")

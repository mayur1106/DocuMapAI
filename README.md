# PDF Table of Contents Generator

A FastAPI backend service that accepts a text-based PDF, detects headings from layout and text patterns, inserts clickable table of contents pages, adds PDF sidebar bookmarks, and returns the modified PDF.

## Features

- Upload and validate PDFs.
- Extract line-level text metadata with PyMuPDF.
- Detect headings with scoring from numbering, font size, boldness, casing, line length, alignment, spacing, position, and repeated style consistency.
- Ignore common headers, footers, page numbers, copyright notices, and repeated boilerplate.
- Build hierarchical TOC JSON.
- If a visible TOC already exists, preserve it and repair its internal hyperlinks.
- If no visible TOC exists, insert generated TOC pages at the start of the PDF.
- Add clickable internal links from TOC entries to target pages.
- Add clickable links from EICAS MEL item references to matching MEL item pages.
- Use OCR only for image-based EICAS pages when normal PDF text extraction cannot read the EICAS table.
- Preview TOC, revision/date, and LEP impact with a read-only dry run before applying document changes.
- Add PDF outline/sidebar bookmarks with `fitz.Document.set_toc()`.
- Name sidebar bookmarks from destination page headers when available, for example `787_57_wings`.
- Convert uploaded PDFs to layout-preserving XML with page, block, line, span, image, link, annotation, and font metadata.
- Run generation through Redis Queue, with a local background fallback for development.

## Project Structure

```text
app/
  main.py
  config.py
  models.py
  services/
    pdf_parser.py
    heading_detector.py
    mel_table_extractor.py
    toc_builder.py
    pdf_writer.py
    existing_toc_linker.py
    section_toc_writer.py
    bookmark_namer.py
    pdf_xml_parser.py
    storage.py
    jobs.py
  workers/
    tasks.py
    run_worker.py
  utils/
    regex_patterns.py
    logger.py
data/
  uploads/
  output/
requirements.txt
README.md
```

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

## Docker Setup

The full application can run with Docker Compose:

```bash
docker compose up --build
```

This starts:

- FastAPI backend: `http://localhost:8000`
- Swagger UI: `http://localhost:8000/docs`
- React dashboard: `http://localhost:5173`
- Redis queue: `localhost:6379`
- RQ worker for PDF processing

Uploaded and generated files are persisted on your machine through the mounted `data/` folder:

```text
./data:/app/data
```

To stop the stack:

```bash
docker compose down
```

To rebuild after dependency changes:

```bash
docker compose build --no-cache
docker compose up
```

Optional port overrides:

```bash
API_PORT=8010 FRONTEND_PORT=5178 REDIS_PORT=6380 docker compose up --build
```

You can also copy `.env.docker.example` to `.env` and edit the ports there before starting Compose.

On PowerShell:

```powershell
Copy-Item .env.docker.example .env
```

Optional frontend API URL override at build time:

```bash
VITE_API_BASE_URL=http://localhost:8010 docker compose up --build
```

The Docker backend image includes Tesseract OCR with English language data for image-based EICAS pages.

Start Redis if you want RQ-backed jobs:

```bash
redis-server
```

Start the API:

```bash
uvicorn app.main:app --reload
```

Start an RQ worker in a second terminal on Linux, macOS, WSL, or Docker:

```bash
rq worker -u redis://localhost:6379/0 pdf-toc
```

On Windows PowerShell, RQ's default worker is not compatible because it uses Unix-only `os.fork()` and `SIGALRM`. Use the bundled Windows-safe worker for local development:

```powershell
python -m app.workers.run_worker
```

You can also override Redis or queue settings:

```powershell
python -m app.workers.run_worker --url redis://localhost:6379/0 --queue pdf-toc
```

If Redis is not available, the API falls back to a local FastAPI background task by default. Disable that with:

```bash
ALLOW_INLINE_FALLBACK=false
```

### OCR for Image-Based EICAS Pages

OCR is scoped to image-like pages inside the EICAS `EM-*` page range. The rest of the PDF still uses normal text extraction.

PyMuPDF OCR requires Tesseract OCR on the machine. On Windows, install Tesseract and make sure `tesseract.exe` is available on `PATH`. If needed, point the app to the tessdata folder:

```powershell
$env:TESSDATA_DIR="C:\Program Files\Tesseract-OCR\tessdata"
```

Optional OCR settings:

```powershell
$env:EICAS_OCR_ENABLED="true"
$env:OCR_LANGUAGE="eng"
$env:OCR_DPI="220"
```

If an image-based EICAS page is found and OCR is unavailable, the job fails with a setup message instead of silently skipping those references.

## Swagger Documentation

FastAPI serves interactive Swagger documentation automatically:

- Swagger UI: `http://localhost:8000/docs`
- ReDoc: `http://localhost:8000/redoc`
- Raw OpenAPI schema: `http://localhost:8000/openapi.json`

Use Swagger UI to upload a PDF, preview dry-run TOC revision/LEP impact, start TOC generation, poll job status, inspect TOC JSON, and download the generated PDF from the browser.

## React Dashboard

The repo includes a Vite + React admin dashboard in `frontend/`.

Install and run it in a second terminal:

```powershell
cd frontend
npm install
npm run dev
```

Open:

```text
http://localhost:5173
```

The dashboard integrates:

- `POST /upload`
- `GET /documents`
- `DELETE /documents/{document_id}`
- `POST /convert-xml/{document_id}`
- `GET /xml/{document_id}/stats`
- `GET /xml/{document_id}/preview`
- `GET /xml/{document_id}/download`
- `POST /generate-toc/{document_id}`
- `POST /hyperlink-existing-toc/{document_id}`
- `GET /status/{job_id}`
- `GET /toc/{document_id}`
- `GET /download/{document_id}`

If your API runs on a different host or port, create `frontend/.env.local`:

```text
VITE_API_BASE_URL=http://localhost:8000
```

## API Usage

Upload a PDF:

```bash
curl -F "file=@manual.pdf" http://localhost:8000/upload
```

Process the PDF:

```bash
curl -X POST http://localhost:8000/generate-toc/{document_id}
```

`/generate-toc/{document_id}` is the normal smart workflow:

- If the PDF already has visible TOC pages, the service preserves those pages and fixes their links.
- If the PDF does not have visible TOC pages, the service creates new clickable TOC pages.
- If generated TOC pages are added, the service automatically increments the current chapter revision and stamps the current month/year on those TOC page headers and LEP rows.

Process with automatic revision/date updates:

```bash
curl -X POST http://localhost:8000/generate-toc/{document_id}
```

Hyperlink an already existing TOC without inserting new TOC pages:

```bash
curl -X POST http://localhost:8000/hyperlink-existing-toc/{document_id}
```

Preview TOC, revision/date, and LEP impact without modifying the PDF:

```bash
curl http://localhost:8000/dry-run/toc/{document_id}
```

Check job status:

```bash
curl http://localhost:8000/status/{job_id}
```

Read detected TOC JSON:

```bash
curl http://localhost:8000/toc/{document_id}
```

Download the final PDF:

```bash
curl -L -o manual_with_toc.pdf http://localhost:8000/download/{document_id}
```

Convert an uploaded PDF to XML:

```bash
curl -X POST http://localhost:8000/convert-xml/{document_id}
```

Read XML conversion statistics:

```bash
curl http://localhost:8000/xml/{document_id}/stats
```

Preview the generated XML:

```bash
curl http://localhost:8000/xml/{document_id}/preview
```

Download the generated XML:

```bash
curl -L -o manual.xml http://localhost:8000/xml/{document_id}/download
```

When using `/hyperlink-existing-toc/{document_id}`, the generated file is named:

```text
data/output/{document_id}_linked_toc.pdf
```

The status result includes:

- `toc_pages`: existing TOC pages detected in the source PDF.
- `linked_count`: number of TOC rows converted to internal PDF links.
- `unresolved_count`: rows that could not be safely mapped to a destination page.
- `unresolved_rows`: row details for manual review.

## How TOC Detection Works

The service first checks for existing visible TOC pages, including global pages such as `TOC-1` and chapter pages such as `TOC 22-1`.

If an existing visible TOC is found, the service does not insert new TOC pages. It parses the visible TOC rows, maps each item to the matching PDF page, overlays internal links, and rebuilds sidebar bookmarks from the resolved TOC rows.

If the PDF has a global TOC but is missing chapter-level TOC pages, the service inserts new TOC pages at the start of each ATA section. Each section TOC is generated from MEL table rows using:

```text
ITEM + DESCRIPTION
```

Those generated section TOC rows link to the exact MEL item row, and the sidebar bookmarks are rebuilt so each section root contains its generated item bookmarks.

Changed TOC pages are stamped in the detected header revision/date cells. The service keeps the existing issue text, such as `Issue-V`, increments the current revision for that chapter, such as `Rev-3` to `Rev-4`, and uses the current month/year, such as `APR 2026`. Matching LEP rows are updated in place. If a new generated TOC page has no existing LEP row, the service adds it to available intentionally blank LEP pages, paginating across those pages when needed while keeping the signature/seal area untouched. If no safe LEP space is available, the job result reports the unplaced rows for manual review instead of rewriting dense LEP table content.

Sidebar bookmark names are derived from the destination page header when the service can find all required parts:

```text
{fleet}_{ata}_{page_header_name}
```

Examples:

```text
787_57_wings
777_28_fuel
a320_27_flight_controls
```

The visible TOC row text is preserved. If a page header does not expose fleet type, ATA code, and page header name, the bookmark falls back to the original TOC title.

If no visible TOC is found, the service extracts MEL item rows from the source tables. For MEL table pages, each generated TOC title is built from:

```text
ITEM + DESCRIPTION
```

For example, a table row like:

```text
21-52-05        Condenser Inlet
                Temperature Control
                systems
                (Cont'd)
21-52-05-02     Condenser Inlet
                Temperature Sensor
                Systems
```

becomes:

```text
21-52-05 Condenser Inlet Temperature Control systems (Cont'd)
21-52-05-02 Condenser Inlet Temperature Sensor Systems
```

The generated TOC links jump to the row position where the MEL item appears. If table extraction finds no reliable MEL item rows, the service falls back to layout-based heading detection.

## Heading Detection Fallback

The detector only promotes text that was extracted from the PDF. It does not invent headings.

Each line receives a confidence score from:

- Numbered section patterns such as `1`, `1.1`, `1.1.1`, and `5.13`.
- Required regex patterns:
  - `^\d+\s+.+$`
  - `^\d+\.\d+\s+.+$`
  - `^\d+\.\d+\.\d+\s+.+$`
  - `^\d+(\.\d+){1,5}\s+.+$`
  - `^(Chapter|Section|Part|Appendix)\s+[A-Z0-9]+`
  - `^[A-Z][A-Z\s\-\/]{5,}$`
- Larger-than-body font size.
- Bold font detection from span flags and font name.
- Uppercase or title-case text.
- Short line length.
- Left alignment.
- Spacing before and after the line.
- Page position.
- Repeated style consistency across candidates.

Numbered headings infer level from numbering depth:

- `5` -> level 1
- `5.13` -> level 2
- `5.13.1` -> level 3

For unnumbered headings, the service infers hierarchy from relative font size and bold style.

## Existing TOC Hyperlinking

Use `/hyperlink-existing-toc/{document_id}` when a PDF already has visible table of contents pages but the entries are missing links or point to external files.

This mode:

- Preserves all original PDF pages.
- Detects global TOC pages such as `TOC-1`, `TOC-2`.
- Detects chapter-level TOC pages such as `TOC 21-1`, `TOC 21-2`.
- Detects `EICAS MESSAGES` reference tables and links MEL item references such as `22-11-02` to the matching MEL item page when that item exists in the same PDF.
- For image-based EICAS pages, performs OCR on those EICAS pages only and then applies the same MEL item reference linking.
- Parses TOC rows by word coordinates instead of plain text, which works better for column-based TOC layouts.
- Builds a page-label index from printed labels such as `PMI-1`, `EM-1`, `TOC 21-1`, and `21-1`.
- Builds an item-label index from actual content pages, so chapter TOC rows such as `21-00-01 Air Synoptic Display` jump to the page where `21-00-01` appears.
- Deletes existing link annotations only on the detected TOC pages and replaces overlapping links on resolved EICAS item references.
- Adds internal clickable `GOTO` links over resolved TOC rows and full EICAS rows that contain MEL item references.
- Rebuilds sidebar bookmarks from resolved TOC rows. EICAS reference links are kept as page links only, not sidebar bookmark entries.
- Reports unresolved rows instead of hallucinating destinations.

For split PDFs, some TOC entries may point to chapters that are listed in the visible TOC but not present in that PDF part. Those rows are intentionally left unresolved.

EICAS references follow the same rule: item codes are linked only when the matching MEL item page exists in the same PDF.

## PDF to XML Conversion

Use `/convert-xml/{document_id}` to create a layout-preserving XML representation of any uploaded PDF.

The XML includes:

- Document metadata from the PDF.
- Page dimensions and rotation.
- Text blocks, lines, and spans.
- Span text, font name, font size, color, bold/italic flags, and bounding boxes.
- Image blocks with dimensions and bounding boxes.
- Link annotations and their destinations when available.
- Other page annotations.

The conversion response and `/xml/{document_id}/stats` include aggregate and per-page statistics:

- page count
- text blocks
- image blocks
- lines
- spans
- words
- characters
- links
- annotations
- fonts detected
- original PDF size
- generated XML size

Generated XML is stored at:

```text
data/output/{document_id}.xml
```

The dashboard includes a dedicated **PDF to XML** page with a source document selector, conversion action, statistics cards, detected fonts, per-page analytics, XML preview, and XML download.

## Storage

The initial implementation uses local filesystem storage:

- Original PDFs: `data/uploads/{document_id}.pdf`
- Generated PDFs: `data/output/{document_id}_with_toc.pdf`
- TOC JSON: `data/output/{document_id}_toc.json`
- XML output: `data/output/{document_id}.xml`
- XML statistics: `data/output/{document_id}_xml_stats.json`
- Document metadata: `data/documents.json`
- Local fallback job metadata: `data/jobs.json`

## Limitations

- OCR is intentionally limited to image-based EICAS pages. Scanned PDFs without embedded text elsewhere will fail heading extraction.
- The detector assumes a reasonably formatted PDF with consistent typography.
- Complex multi-column layouts may need tuning.
- Existing PDF page labels are not rewritten. Visible TOC page numbers use final physical PDF page numbers after inserted TOC pages.
- Encrypted PDFs are rejected.
- Local JSON metadata is suitable for initial deployment and development, not high-concurrency production workloads.

## Future Improvements

- Add database-backed document and job records.
- Add S3-compatible object storage.
- Support PDF page labels.
- Add per-document detection tuning options.
- Add OCR as an optional fallback behind an explicit flag.
- Add automated evaluation against labeled PDFs.
- Add auth, rate limits, and upload scanning.

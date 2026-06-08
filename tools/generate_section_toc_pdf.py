from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import get_settings
from app.services.mel_table_extractor import extract_mel_table_headings
from app.services.section_toc_writer import write_pdf_with_section_tocs


def main() -> int:
    if len(sys.argv) != 4:
        print("Usage: generate_section_toc_pdf.py SOURCE.pdf OUTPUT.pdf TOC.json")
        return 2

    source_pdf = Path(sys.argv[1])
    output_pdf = Path(sys.argv[2])
    toc_json = Path(sys.argv[3])

    settings = get_settings()
    headings = extract_mel_table_headings(source_pdf)
    result = write_pdf_with_section_tocs(source_pdf, output_pdf, headings, settings)
    toc_json.parent.mkdir(parents=True, exist_ok=True)
    toc_json.write_text(
        json.dumps([heading.model_dump() for heading in headings], indent=2),
        encoding="utf-8",
    )
    print(json.dumps(result.to_dict(), indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

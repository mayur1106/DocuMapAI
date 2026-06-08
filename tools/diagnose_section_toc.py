from __future__ import annotations

import json
import sys
from pathlib import Path

import fitz

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import get_settings
from app.services.mel_table_extractor import extract_mel_table_headings
from app.services.section_toc_writer import _build_section_toc_plans, has_missing_section_toc_pattern


def main() -> int:
    path = Path(sys.argv[1])
    settings = get_settings()
    headings = extract_mel_table_headings(path)
    with fitz.open(path) as document:
        plans = _build_section_toc_plans(document, headings, settings) if headings else []
        print(
            json.dumps(
                {
                    "heading_count": len(headings),
                    "heading_sample": [heading.model_dump() for heading in headings[:10]],
                    "has_missing_section_toc_pattern": has_missing_section_toc_pattern(path, headings, settings),
                    "plan_count": len(plans),
                    "plan_sample": [
                        {
                            "chapter": plan.chapter,
                            "title": plan.title,
                            "source_start_page": plan.source_start_page,
                            "source_end_page": plan.source_end_page,
                            "entry_count": len(plan.headings),
                            "toc_page_count": len(plan.toc_pages),
                        }
                        for plan in plans[:20]
                    ],
                },
                indent=2,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

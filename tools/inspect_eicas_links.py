from __future__ import annotations

import json
import sys
from pathlib import Path

import fitz

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.existing_toc_linker import (  # noqa: E402
    EM_PAGE_RE,
    ITEM_LABEL_RE,
    _bottom_region_text,
    _top_region_text,
    extract_eicas_reference_rows,
    find_eicas_reference_pages,
    normalize_label,
)


def page_labels(page: fitz.Page) -> list[str]:
    text = f"{_top_region_text(page)} {_bottom_region_text(page)}"
    return [match.group(0).upper().replace(" ", "") for match in EM_PAGE_RE.finditer(text)]


def page_item_labels(page: fitz.Page) -> list[str]:
    labels: list[str] = []
    for word in page.get_text("words"):
        label = normalize_label(str(word[4]).strip())
        if ITEM_LABEL_RE.fullmatch(label) and label not in labels:
            labels.append(label)
    return labels


def main() -> int:
    path = Path(sys.argv[1])
    with fitz.open(path) as document:
        eicas_pages = find_eicas_reference_pages(document)
        all_em_pages = [
            page_index
            for page_index, page in enumerate(document)
            if page_labels(page)
        ]
        rows = extract_eicas_reference_rows(document, eicas_pages)
        row_by_page: dict[int, list[str]] = {}
        for row in rows:
            row_by_page.setdefault(row.page_index, []).append(row.target_label or "")

        summary = []
        for page_index in eicas_pages:
            page = document[page_index]
            links = page.get_links()
            targets = []
            for link in links[:10]:
                target = link.get("page")
                targets.append(
                    {
                        "target_page": None if target is None else target + 1,
                        "target_labels": [] if target is None else page_item_labels(document[target])[:5],
                    }
                )
            summary.append(
                {
                    "page": page_index + 1,
                    "em_labels": page_labels(page),
                    "detected_rows": row_by_page.get(page_index, []),
                    "link_count": len(links),
                    "targets_sample": targets,
                }
            )

        print(
            json.dumps(
                {
                    "pdf": str(path),
                    "all_em_page_count": len(all_em_pages),
                    "all_em_pages": [
                        {
                            "page": page_index + 1,
                            "em_labels": page_labels(document[page_index]),
                            "word_count": len(document[page_index].get_text("words")),
                            "has_eicas": "EICAS MESSAGES" in document[page_index].get_text("text").upper(),
                            "has_mel_item": "MEL ITEM" in document[page_index].get_text("text").upper(),
                            "link_count": len(document[page_index].get_links()),
                        }
                        for page_index in all_em_pages
                    ],
                    "eicas_page_count": len(eicas_pages),
                    "eicas_pages": summary,
                },
                indent=2,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

import fitz

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.existing_toc_linker import (
    build_outline_title_index,
    build_page_label_index,
    extract_existing_toc_rows,
    extract_reference_labels,
    find_existing_toc_pages,
    find_local_toc_pages,
    infer_target_label,
    normalize_label,
    resolve_target_page,
)


def top_bottom_text(page: fitz.Page) -> str:
    rect = page.rect
    top = fitz.Rect(0, 0, rect.width, min(100, rect.height))
    bottom = fitz.Rect(0, max(0, rect.height - 90), rect.width, rect.height)
    return f"{page.get_textbox(top)} {page.get_textbox(bottom)}"


def main() -> int:
    path = Path(sys.argv[1])
    with fitz.open(path) as document:
        global_toc_pages = find_existing_toc_pages(document)
        local_toc_pages = find_local_toc_pages(document)
        rows = extract_existing_toc_rows(document, global_toc_pages)
        label_index = build_page_label_index(document, global_toc_pages)
        outline_index = build_outline_title_index(document)

        unresolved = []
        linked = 0
        for row in rows:
            row.target_label = infer_target_label(row)
            row.target_page_index = resolve_target_page(row, label_index, outline_index)
            if row.target_page_index is None:
                unresolved.append(
                    {
                        "title": row.title,
                        "reference_text": row.reference_text,
                        "target_label": row.target_label,
                    }
                )
            else:
                linked += 1

        label_samples = []
        for page_index, page in enumerate(document):
            if page_index in global_toc_pages:
                continue
            text = top_bottom_text(page)
            labels = extract_reference_labels(text)
            if labels:
                label_samples.append(
                    {
                        "page": page_index + 1,
                        "labels": labels[:5],
                        "text": " ".join(text.split())[:220],
                    }
                )
            if len(label_samples) >= 20:
                break

        target_counts = Counter(normalize_label(item["target_label"] or "") for item in unresolved)
        print(
            json.dumps(
                {
                    "pages": document.page_count,
                    "outline_count": len(document.get_toc()),
                    "global_toc_pages": [page + 1 for page in global_toc_pages],
                    "local_toc_pages": [page + 1 for page in local_toc_pages],
                    "global_rows": len(rows),
                    "global_row_sample": [
                        {
                            "chapter": row.chapter,
                            "title": row.title,
                            "reference_text": row.reference_text,
                        }
                        for row in rows[:20]
                    ],
                    "linked_global_rows": linked,
                    "unresolved_global_rows": len(unresolved),
                    "label_index_count": len(label_index),
                    "label_index_sample": list(label_index.items())[:20],
                    "unresolved_target_counts": target_counts.most_common(20),
                    "unresolved_sample": unresolved[:20],
                    "page_label_samples": label_samples,
                },
                indent=2,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

import fitz


def convert_pdf_to_xml(
    *,
    document_id: str,
    filename: str,
    pdf_path: Path,
    xml_path: Path,
) -> dict[str, Any]:
    xml_path.parent.mkdir(parents=True, exist_ok=True)
    stats = _empty_stats(document_id, filename, xml_path.name, pdf_path.stat().st_size)
    root = ET.Element(
        "pdf_document",
        {
            "document_id": document_id,
            "filename": filename,
            "source_path": str(pdf_path),
            "generated_at": datetime.now(timezone.utc).isoformat(),
        },
    )

    with fitz.open(pdf_path) as document:
        stats["page_count"] = document.page_count
        root.set("page_count", str(document.page_count))
        _append_metadata(root, document.metadata or {})
        pages_element = ET.SubElement(root, "pages")

        for page_index, page in enumerate(document, start=1):
            page_stats = _append_page(pages_element, page, page_index)
            stats["pages"].append(page_stats)
            for key in ("text_blocks", "image_blocks", "lines", "spans", "words", "characters", "links", "annotations"):
                stats[key] += page_stats[key]

            fonts = page_stats.pop("_fonts", set())
            stats["_fonts"].update(fonts)

    stats["fonts"] = sorted(stats.pop("_fonts"))
    _indent(root)
    tree = ET.ElementTree(root)
    tree.write(xml_path, encoding="utf-8", xml_declaration=True, short_empty_elements=True)
    stats["xml_size_bytes"] = xml_path.stat().st_size
    return stats


def read_xml_preview(xml_path: Path, max_characters: int = 16000) -> str:
    with xml_path.open("r", encoding="utf-8") as handle:
        return handle.read(max_characters)


def _append_metadata(root: ET.Element, metadata: dict[str, Any]) -> None:
    metadata_element = ET.SubElement(root, "metadata")
    for key in sorted(metadata):
        value = metadata.get(key)
        if value is None or value == "":
            continue
        child = ET.SubElement(metadata_element, _safe_tag(key))
        child.text = str(value)


def _append_page(parent: ET.Element, page: fitz.Page, page_number: int) -> dict[str, Any]:
    page_stats: dict[str, Any] = {
        "page": page_number,
        "width": round(float(page.rect.width), 2),
        "height": round(float(page.rect.height), 2),
        "text_blocks": 0,
        "image_blocks": 0,
        "lines": 0,
        "spans": 0,
        "words": 0,
        "characters": 0,
        "links": 0,
        "annotations": 0,
        "_fonts": set(),
    }
    page_element = ET.SubElement(
        parent,
        "page",
        {
            "number": str(page_number),
            "width": _number(page.rect.width),
            "height": _number(page.rect.height),
            "rotation": str(page.rotation),
        },
    )

    blocks_element = ET.SubElement(page_element, "blocks")
    text_dict = page.get_text("dict")
    for block_index, block in enumerate(text_dict.get("blocks", [])):
        block_type = block.get("type")
        bbox = _bbox(block.get("bbox"))
        if block_type == 0:
            page_stats["text_blocks"] += 1
            block_element = ET.SubElement(
                blocks_element,
                "block",
                {"index": str(block_index), "type": "text", **bbox},
            )
            _append_lines(block_element, block.get("lines", []), page_stats)
        elif block_type == 1:
            page_stats["image_blocks"] += 1
            ET.SubElement(
                blocks_element,
                "block",
                {
                    "index": str(block_index),
                    "type": "image",
                    "width": str(block.get("width", "")),
                    "height": str(block.get("height", "")),
                    "ext": str(block.get("ext", "")),
                    **bbox,
                },
            )

    _append_links(page_element, page, page_stats)
    _append_annotations(page_element, page, page_stats)
    return page_stats


def _append_lines(parent: ET.Element, lines: list[dict[str, Any]], page_stats: dict[str, Any]) -> None:
    for line_index, line in enumerate(lines):
        page_stats["lines"] += 1
        line_element = ET.SubElement(
            parent,
            "line",
            {
                "index": str(line_index),
                "writing_mode": str(line.get("wmode", "")),
                "direction": _point(line.get("dir")),
                **_bbox(line.get("bbox")),
            },
        )
        for span_index, span in enumerate(line.get("spans", [])):
            text = str(span.get("text", ""))
            font = str(span.get("font", ""))
            flags = int(span.get("flags", 0) or 0)
            page_stats["spans"] += 1
            page_stats["words"] += len(text.split())
            page_stats["characters"] += len(text)
            if font:
                page_stats["_fonts"].add(font)

            span_element = ET.SubElement(
                line_element,
                "span",
                {
                    "index": str(span_index),
                    "font": font,
                    "size": _number(span.get("size")),
                    "color": _color(span.get("color")),
                    "flags": str(flags),
                    "bold": str(_is_bold(font, flags)).lower(),
                    "italic": str(_is_italic(font, flags)).lower(),
                    **_bbox(span.get("bbox")),
                },
            )
            span_element.text = text


def _append_links(parent: ET.Element, page: fitz.Page, page_stats: dict[str, Any]) -> None:
    links = page.get_links()
    page_stats["links"] = len(links)
    if not links:
        return
    links_element = ET.SubElement(parent, "links")
    for index, link in enumerate(links):
        attrs = {"index": str(index), "kind": str(link.get("kind", "")), **_bbox(link.get("from"))}
        if "page" in link:
            attrs["target_page"] = str(int(link["page"]) + 1)
        if link.get("uri"):
            attrs["uri"] = str(link["uri"])
        ET.SubElement(links_element, "link", attrs)


def _append_annotations(parent: ET.Element, page: fitz.Page, page_stats: dict[str, Any]) -> None:
    annotations = list(page.annots() or [])
    page_stats["annotations"] = len(annotations)
    if not annotations:
        return
    annotations_element = ET.SubElement(parent, "annotations")
    for index, annot in enumerate(annotations):
        attrs = {
            "index": str(index),
            "type": str(annot.type[1] if annot.type else ""),
            "content": str(annot.info.get("content", "") if annot.info else ""),
            **_bbox(annot.rect),
        }
        ET.SubElement(annotations_element, "annotation", attrs)


def _empty_stats(document_id: str, filename: str, xml_filename: str, file_size_bytes: int) -> dict[str, Any]:
    return {
        "document_id": document_id,
        "filename": filename,
        "xml_filename": xml_filename,
        "page_count": 0,
        "text_blocks": 0,
        "image_blocks": 0,
        "lines": 0,
        "spans": 0,
        "words": 0,
        "characters": 0,
        "links": 0,
        "annotations": 0,
        "fonts": [],
        "_fonts": set(),
        "file_size_bytes": file_size_bytes,
        "xml_size_bytes": 0,
        "pages": [],
    }


def _bbox(value: Any) -> dict[str, str]:
    if value is None:
        return {}
    rect = fitz.Rect(value)
    return {
        "x0": _number(rect.x0),
        "y0": _number(rect.y0),
        "x1": _number(rect.x1),
        "y1": _number(rect.y1),
    }


def _point(value: Any) -> str:
    if not value or len(value) < 2:
        return ""
    return f"{_number(value[0])},{_number(value[1])}"


def _number(value: Any) -> str:
    try:
        return f"{float(value):.2f}".rstrip("0").rstrip(".")
    except (TypeError, ValueError):
        return ""


def _color(value: Any) -> str:
    try:
        return f"#{int(value):06x}"
    except (TypeError, ValueError):
        return ""


def _is_bold(font: str, flags: int) -> bool:
    lowered = font.lower()
    return "bold" in lowered or "black" in lowered or bool(flags & 16)


def _is_italic(font: str, flags: int) -> bool:
    lowered = font.lower()
    return "italic" in lowered or "oblique" in lowered or bool(flags & 2)


def _safe_tag(value: str) -> str:
    cleaned = "".join(character if character.isalnum() or character in {"_", "-"} else "_" for character in value.strip())
    if not cleaned or cleaned[0].isdigit():
        return f"field_{cleaned}"
    return cleaned


def _indent(root: ET.Element) -> None:
    try:
        ET.indent(root, space="  ")
    except AttributeError:
        pass

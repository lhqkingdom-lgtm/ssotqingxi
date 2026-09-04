from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from dataclasses import replace
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable

from core.ir import BBox, CellIR, DocumentIR, PageIR, TableIR
from core.text import compact


@dataclass(frozen=True)
class HtmlCell:
    text: str
    row_span: int
    column_span: int


class _TableHtmlParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.table_depth = 0
        self.in_row = False
        self.in_cell = False
        self.rows: list[list[HtmlCell]] = []
        self.current_row: list[HtmlCell] = []
        self.current_text: list[str] = []
        self.current_row_span = 1
        self.current_column_span = 1

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.casefold()
        if tag == "table":
            self.table_depth += 1
            return
        if self.table_depth != 1:
            return
        if tag == "tr":
            self.in_row = True
            self.current_row = []
        elif tag in {"td", "th"} and self.in_row:
            values = {key.casefold(): value for key, value in attrs}
            self.in_cell = True
            self.current_text = []
            self.current_row_span = _positive_span(values.get("rowspan"))
            self.current_column_span = _positive_span(values.get("colspan"))
        elif tag == "br" and self.in_cell:
            self.current_text.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if tag == "table":
            self.table_depth = max(0, self.table_depth - 1)
            return
        if self.table_depth != 1:
            return
        if tag in {"td", "th"} and self.in_cell:
            self.current_row.append(
                HtmlCell(_clean_cell_text("".join(self.current_text)), self.current_row_span, self.current_column_span)
            )
            self.in_cell = False
        elif tag == "tr" and self.in_row:
            self.rows.append(self.current_row)
            self.current_row = []
            self.in_row = False

    def handle_data(self, data: str) -> None:
        if self.table_depth == 1 and self.in_cell:
            self.current_text.append(data)


def _positive_span(value: str | None) -> int:
    try:
        return max(1, int(value or 1))
    except (TypeError, ValueError):
        return 1


def _clean_cell_text(value: str) -> str:
    lines = [compact(line.replace("\xa0", " ")) for line in value.splitlines()]
    return "\n".join(line for line in lines if line)


def html_to_cells(table_id: str, page: int, fragment: int, html: str) -> tuple[CellIR, ...]:
    parser = _TableHtmlParser()
    parser.feed(html)
    parser.close()
    if not parser.rows:
        raise ValueError("MinerU table HTML contains no rows")

    anchors: dict[tuple[int, int], tuple[HtmlCell, str]] = {}
    covered: dict[tuple[int, int], str] = {}
    maximum_column = 0
    maximum_row = len(parser.rows)
    for row_index, row in enumerate(parser.rows):
        column = 0
        for parsed_cell in row:
            while (row_index, column) in covered or (row_index, column) in anchors:
                column += 1
            cell_id = f"{table_id}-f{fragment}-p{page}-r{row_index}-c{column}"
            anchors[(row_index, column)] = (parsed_cell, cell_id)
            for target_row in range(row_index, row_index + parsed_cell.row_span):
                for target_column in range(column, column + parsed_cell.column_span):
                    position = (target_row, target_column)
                    if position in covered or (position in anchors and position != (row_index, column)):
                        raise ValueError(f"overlapping rowspan/colspan at row={target_row}, column={target_column}")
                    if position != (row_index, column):
                        covered[position] = cell_id
            maximum_column = max(maximum_column, column + parsed_cell.column_span)
            maximum_row = max(maximum_row, row_index + parsed_cell.row_span)
            column += parsed_cell.column_span

    cells: list[CellIR] = []
    for row in range(maximum_row):
        for column in range(maximum_column):
            anchor = anchors.get((row, column))
            if anchor is not None:
                parsed_cell, cell_id = anchor
                cells.append(
                    CellIR(
                        cell_id=cell_id,
                        page=page,
                        fragment=fragment,
                        row=row,
                        column=column,
                        raw_text=parsed_cell.text,
                        normalized_text=compact(parsed_cell.text).lower(),
                        bbox=None,
                        row_span=parsed_cell.row_span,
                        column_span=parsed_cell.column_span,
                    )
                )
                continue
            inherited_from = covered.get((row, column))
            cells.append(
                CellIR(
                    cell_id=f"{table_id}-f{fragment}-p{page}-r{row}-c{column}",
                    page=page,
                    fragment=fragment,
                    row=row,
                    column=column,
                    raw_text="",
                    normalized_text="",
                    bbox=None,
                    inherited_from=inherited_from,
                )
            )
    return tuple(cells)


def _walk(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _node_text(node: Any) -> str:
    parts: list[str] = []
    for item in _walk(node):
        if item.get("type") == "text" and isinstance(item.get("content"), str):
            parts.append(item["content"])
    return compact(" ".join(parts))


def _first_descendant_value(node: dict[str, Any], key: str) -> str:
    direct = node.get(key)
    if isinstance(direct, str) and direct:
        return direct
    for item in _walk(node):
        value = item.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _table_title(node: dict[str, Any]) -> str:
    candidates: list[str] = []
    for item in _walk(node):
        if item.get("type") == "table_caption":
            title = _node_text(item)
            if title:
                candidates.append(title)
    preferred = [title for title in candidates if _looks_like_caption(title)]
    return preferred[0] if preferred else ""


def _looks_like_caption(value: str) -> bool:
    return bool(re.search(r"\b(?:table|figure)\s*\d+|[表图]\s*\d+", value, re.IGNORECASE))


def _page_captions(page: dict[str, Any]) -> list[tuple[BBox, str]]:
    captions: list[tuple[BBox, str]] = []
    seen: set[tuple[BBox, str]] = set()
    for item in _walk(page.get("preproc_blocks", [])):
        if item.get("type") != "table_caption":
            continue
        bbox = _bbox(item)
        title = _node_text(item)
        if bbox is not None and title and _looks_like_caption(title) and (bbox, title) not in seen:
            seen.add((bbox, title))
            captions.append((bbox, title))
    return captions


def _nearest_preceding_caption(captions: list[tuple[BBox, str]], table_bbox: BBox | None) -> str:
    if table_bbox is None:
        return ""
    eligible = [item for item in captions if item[0][3] <= table_bbox[1] + 2]
    if not eligible:
        return ""
    return max(eligible, key=lambda item: item[0][3])[1]


def _orientation(node: dict[str, Any]) -> int:
    angles = [item.get("angle") for item in _walk(node) if item.get("angle") is not None]
    for angle in angles:
        try:
            numeric = int(float(angle)) % 360
            if numeric:
                return numeric
        except (TypeError, ValueError):
            continue
    return 0


def _bbox(node: dict[str, Any]) -> BBox | None:
    value = node.get("bbox")
    if not isinstance(value, list) or len(value) != 4:
        return None
    try:
        return tuple(float(item) for item in value)  # type: ignore[return-value]
    except (TypeError, ValueError):
        return None


def _resolve_image(base_dir: Path, image_path: str) -> Path | None:
    if not image_path:
        return None
    supplied = Path(image_path)
    candidates = [supplied] if supplied.is_absolute() else [base_dir / supplied, base_dir / "images" / supplied]
    return next((candidate.resolve() for candidate in candidates if candidate.exists()), candidates[-1].resolve())


def _page_text(page: dict[str, Any]) -> str:
    seen: set[tuple[tuple[Any, ...], str]] = set()
    parts: list[str] = []
    for item in _walk(page.get("preproc_blocks", [])):
        if item.get("type") != "text" or not isinstance(item.get("content"), str):
            continue
        signature = (tuple(item.get("bbox") or ()), item["content"])
        if signature not in seen:
            seen.add(signature)
            parts.append(item["content"])
    return "\n".join(parts)


def _source_file(base_dir: Path) -> str:
    folder_name = base_dir.name
    pdf_marker = folder_name.casefold().find(".pdf-")
    if pdf_marker >= 0:
        return folder_name[:pdf_marker + 4]
    origins = sorted(base_dir.glob("*_origin.pdf"))
    if origins:
        return origins[0].name
    pdfs = sorted(base_dir.glob("*.pdf"))
    return pdfs[0].name if pdfs else base_dir.name


def _source_pdf(base_dir: Path) -> Path | None:
    origins = sorted(base_dir.glob("*_origin.pdf"))
    if origins:
        return origins[0]
    pdfs = sorted(base_dir.glob("*.pdf"))
    return pdfs[0] if pdfs else None


def _pdf_page_rotations(base_dir: Path) -> list[int]:
    source_pdf = _source_pdf(base_dir)
    if source_pdf is None:
        return []
    try:
        from pypdf import PdfReader

        return [int(page.get("/Rotate", 0) or 0) % 360 for page in PdfReader(str(source_pdf)).pages]
    except Exception:
        return []


def _logical_columns(table: TableIR) -> int:
    return max((cell.column for cell in table.cells), default=-1) + 1


def _infer_missing_titles(tables: list[TableIR]) -> list[TableIR]:
    result = list(tables)
    for index, table in enumerate(result):
        if _looks_like_caption(table.title):
            continue
        page = table.pages[0]
        columns = _logical_columns(table)
        neighbors = [
            candidate
            for candidate in result
            if candidate is not table
            and abs(candidate.pages[0] - page) == 1
            and _logical_columns(candidate) == columns
            and _looks_like_caption(candidate.title)
        ]
        if len(neighbors) != 1:
            continue
        neighbor = neighbors[0]
        inferred = re.sub(r"\(?\s*continued\s*\)?|（?\s*续(?:表)?\s*）?", "", neighbor.title, flags=re.IGNORECASE)
        result[index] = replace(
            table,
            title=inferred,
            metadata={**table.metadata, "title_inferred_from_table_id": neighbor.table_id},
        )
    return result


def load_mineru_document(layout_path: Path) -> DocumentIR:
    payload = json.loads(layout_path.read_text(encoding="utf-8"))
    pages_payload = payload.get("pdf_info")
    if not isinstance(pages_payload, list):
        raise ValueError("MinerU layout.json does not contain a pdf_info list")
    base_dir = layout_path.resolve().parent
    page_rotations = _pdf_page_rotations(base_dir)
    tables: list[TableIR] = []
    pages: list[PageIR] = []

    for fallback_index, page_payload in enumerate(pages_payload):
        if not isinstance(page_payload, dict):
            continue
        page_index = int(page_payload.get("page_idx", fallback_index))
        page_number = page_index + 1
        source_rotation = page_rotations[page_index] if page_index < len(page_rotations) else 0
        size = page_payload.get("page_size") or [0, 0]
        width = float(size[0]) if len(size) > 0 else 0.0
        height = float(size[1]) if len(size) > 1 else 0.0
        page_captions = _page_captions(page_payload)
        candidates: dict[tuple[Any, ...], dict[str, Any]] = {}
        for node in _walk(page_payload.get("preproc_blocks", [])):
            if node.get("type") != "table":
                continue
            html = _first_descendant_value(node, "html")
            if not html:
                continue
            bbox = _bbox(node)
            image_path = _first_descendant_value(node, "image_path")
            signature = (
                page_number,
                hashlib.sha256(html.encode("utf-8")).hexdigest(),
                image_path,
            )
            title = _table_title(node) or _nearest_preceding_caption(page_captions, bbox)
            candidate = {
                "html": html,
                "bbox": bbox,
                "image_path": image_path,
                "title": title,
                "orientation": _orientation(node) or source_rotation,
                "source_index": node.get("index"),
            }
            previous = candidates.get(signature)
            if previous is None or (not previous["title"] and candidate["title"]):
                candidates[signature] = candidate

        page_table_ids: list[str] = []
        ordered = sorted(
            candidates.values(),
            key=lambda item: (
                item["bbox"][1] if item["bbox"] else float("inf"),
                item["bbox"][0] if item["bbox"] else float("inf"),
                hashlib.sha256(item["html"].encode("utf-8")).hexdigest(),
            ),
        )
        for item in ordered:
            html_digest = hashlib.sha256(item["html"].encode("utf-8")).hexdigest()
            table_id = f"mineru-p{page_number}-{html_digest[:12]}"
            cells = html_to_cells(table_id, page_number, 1, item["html"])
            resolved_image = _resolve_image(base_dir, item["image_path"])
            title = item["title"]
            table = TableIR(
                table_id=table_id,
                title=title,
                pages=(page_number,),
                continued=bool(re.search(r"\bcontinued\b|续(?:表)?", title, re.IGNORECASE)),
                orientations=(item["orientation"],),
                bboxes=(item["bbox"] or (0.0, 0.0, 0.0, 0.0),),
                cells=cells,
                metadata={
                    "source_backend": "mineru",
                    "layout_file": str(layout_path.resolve()),
                    "mineru_page_index": page_index,
                    "image_path": item["image_path"],
                    "resolved_image_path": str(resolved_image) if resolved_image else "",
                    "image_exists": bool(resolved_image and resolved_image.exists()),
                    "html_sha256": html_digest,
                    "source_node_index": item["source_index"],
                    "source_page_rotation": source_rotation,
                },
            )
            tables.append(table)
            page_table_ids.append(table_id)
        pages.append(
            PageIR(
                page=page_number,
                width=width,
                height=height,
                rotation=source_rotation,
                text=_page_text(page_payload),
                table_ids=tuple(page_table_ids),
            )
        )
    tables = _infer_missing_titles(tables)
    page_table_ids = {page.page: [] for page in pages}
    for table in tables:
        for page_number in table.pages:
            page_table_ids.setdefault(page_number, []).append(table.table_id)
    pages = [replace(page, table_ids=tuple(page_table_ids.get(page.page, []))) for page in pages]
    return DocumentIR(source_file=_source_file(base_dir), page_count=len(pages), pages=tuple(pages), tables=tuple(tables))

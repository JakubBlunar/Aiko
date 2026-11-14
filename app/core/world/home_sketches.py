"""Editable sketchbook pages as bounded drawing data, never executable markup."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import hashlib
import json
import math
from typing import Any
from uuid import uuid4
from xml.etree import ElementTree as ET

from app.core.world.home_drinks import HomeAction, _version, parse_at

SKETCH_ACTIONS = frozenset({"new_sketch", "revise_sketch"})
PALETTE = ("#292524", "#57534e", "#a8a29e", "#d6d3d1")
REFERENCE = (
    "A fictional view from the virtual apartment window: a low flat roof on the left, "
    "a taller narrow building in the middle, and three overlapping sloped roofs on the right. "
    "This is an authored drawing reference, not a view of real buildings or a visual observation."
)


def new_sketchbook():
    return {"schema": 1, "pages": [], "reference": REFERENCE}


def sketchbook_state(item: Any):
    state = item.get("state", {}) if isinstance(item, dict) else getattr(item, "state", {})
    book = state.get("home_sketchbook")
    if (not isinstance(book, dict) or book.get("schema") != 1
            or not isinstance(book.get("pages"), list)):
        return None
    if len(book["pages"]) > 16:
        return None
    for page in book["pages"]:
        if (not isinstance(page, dict) or not isinstance(page.get("id"), str)
                or not isinstance(page.get("revisions"), list)
                or not 1 <= len(page["revisions"]) <= 4):
            return None
        if any(not isinstance(row, dict) or not isinstance(row.get("title"), str)
               or not isinstance(row.get("strokes"), list) for row in page["revisions"]):
            return None
    return book


def validate_drawing(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict) or set(raw) - {"title", "strokes", "note"}:
        raise ValueError("Drawing needs a title and bounded strokes, not markup")
    title, strokes, note = raw.get("title"), raw.get("strokes"), raw.get("note", "")
    if (not isinstance(title, str) or not 2 <= len(title.strip()) <= 80
            or "[[" in title or "<|" in title or not isinstance(note, str) or len(note) > 240
            or not isinstance(strokes, list) or not 1 <= len(strokes) <= 24):
        raise ValueError("Invalid drawing title, note or stroke count")
    result = []
    total = 0
    for stroke in strokes:
        if not isinstance(stroke, dict) or set(stroke) - {"points", "width", "shade", "filled"}:
            raise ValueError("Only structured points, width, shade and filled are supported")
        points, width = stroke.get("points"), stroke.get("width", 2)
        shade, filled = stroke.get("shade", 0), stroke.get("filled", False)
        if (type(width) not in (int, float) or not math.isfinite(width) or not 0.5 <= width <= 8
                or type(shade) is not int or not 0 <= shade <= 3 or type(filled) is not bool
                or not isinstance(points, list) or not 2 <= len(points) <= 32):
            raise ValueError("Invalid stroke properties")
        clean = []
        for point in points:
            if (not isinstance(point, list) or len(point) != 2
                    or any(type(value) not in (int, float) or not math.isfinite(value)
                           for value in point) or not 0 <= point[0] <= 512
                    or not 0 <= point[1] <= 320):
                raise ValueError("Drawing coordinates must stay inside the 512 by 320 page")
            clean.append([float(point[0]), float(point[1])])
        total += len(clean)
        result.append({"points": clean, "width": float(width), "shade": shade, "filled": filled})
    if total > 256:
        raise ValueError("A drawing may have at most 256 points")
    return {"title": title.strip(), "strokes": result, "note": note}


def drawing_hash(drawing):
    return hashlib.sha256(json.dumps(drawing, sort_keys=True).encode()).hexdigest()


def sketch_actions(items, locations, room, now: datetime) -> list[HomeAction]:
    local = {loc["id"] for loc in locations if loc["scene_id"] == room.get("scene_id")}
    day = now.astimezone().date()
    writes = 0
    for item in items:
        book = sketchbook_state(item)
        for page in book["pages"] if book else []:
            for revision in page.get("revisions", []):
                at = parse_at(revision.get("at"))
                writes += int(at is not None and at.astimezone().date() == day)
    if writes >= 3:
        return []
    actions = []
    for item in items:
        from app.core.world.home_belongings import active_item

        book = sketchbook_state(item)
        if book is None or not active_item(item):
            continue
        spot = item["location_id"] or room.get("location_id")
        if spot not in local:
            continue
        tools = next((row for row in items if active_item(row)
                      and row["location_id"] in (None, spot)
                      and isinstance(row["state"].get("home_drawing_tools"), list)
                      and "pencil" in row["state"]["home_drawing_tools"]), None)
        if tools is None:
            continue
        targets = (_version(item), _version(tools))
        if len(book["pages"]) < 16:
            actions.append(HomeAction("new_sketch", item["id"], spot,
                                      "saved a new drawing in my sketchbook", targets,
                                      parameters={"drawing_required": True}))
        if "eraser" in tools["state"]["home_drawing_tools"]:
            for page in book["pages"][-3:]:
                revisions = page.get("revisions", [])
                if revisions and len(revisions) < 4:
                    actions.append(HomeAction(
                        "revise_sketch", item["id"], spot,
                        "saved another revision of a sketchbook drawing", targets,
                        parameters={"drawing_required": True, "page_id": page["id"],
                                    "base_revision": len(revisions)},
                    ))
    return actions


def apply_sketch_action(action: HomeAction, items, now: datetime, drawing):
    if drawing is None:
        raise ValueError("A sketch needs actual drawing data, not a narration or title alone")
    item = deepcopy(items[action.item_id])
    book = item["state"]["home_sketchbook"]
    if action.action == "new_sketch":
        page = {"id": uuid4().hex, "revisions": []}
        book["pages"].append(page)
    else:
        page = next(page for page in book["pages"] if page["id"] == action.parameters["page_id"])
    page["revisions"].append({**drawing, "revision": len(page["revisions"]) + 1,
                              "at": now.isoformat(), "sha256": drawing_hash(drawing)})
    return [item]


def sketch_context(item, page_id=None):
    book = sketchbook_state(item)
    if book is None:
        return None
    pages = book["pages"]
    page = next((page for page in pages if page["id"] == page_id), pages[-1] if pages else None)
    return {"reference": book.get("reference", REFERENCE), "page_count": len(pages),
            "latest_drawing": page["revisions"][-1] if page and page.get("revisions") else None,
            "mode": "original creative drawing, not autobiography or visual observation"}


def inspect_sketch(item, page=None, revision=None):
    book = sketchbook_state(item)
    pages = book["pages"] if book else []
    if not pages:
        return {"pages": [], "reference": book.get("reference", REFERENCE) if book else REFERENCE}
    number = len(pages) if page is None else page
    if type(number) is not int or not 1 <= number <= len(pages):
        raise ValueError("Choose an existing sketchbook page number")
    revisions = pages[number - 1]["revisions"]
    version = len(revisions) if revision is None else revision
    if type(version) is not int or not 1 <= version <= len(revisions):
        raise ValueError("Choose an existing saved sketch revision")
    return {"page": number, "page_count": len(pages), "revision_count": len(revisions),
            "drawing": revisions[version - 1], "reference": book.get("reference", REFERENCE),
            "render_url": f"/api/world/items/{item.id}/sketch.svg?page={number}&revision={version}"}


def render_sketch_svg(drawing) -> str:
    drawing = validate_drawing({key: drawing[key] for key in ("title", "strokes", "note")})
    root = ET.Element("svg", {"xmlns": "http://www.w3.org/2000/svg", "viewBox": "0 0 512 320",
                              "role": "img", "aria-label": drawing["title"]})
    ET.SubElement(root, "title").text = drawing["title"]
    ET.SubElement(root, "rect", {"width": "512", "height": "320", "fill": "#faf7ef"})
    for stroke in drawing["strokes"]:
        shade = PALETTE[stroke["shade"]]
        ET.SubElement(root, "polygon" if stroke["filled"] else "polyline", {
            "points": " ".join(f"{x},{y}" for x, y in stroke["points"]),
            "stroke": shade, "fill": shade if stroke["filled"] else "none",
            "stroke-width": str(stroke["width"]), "stroke-linejoin": "round",
            "stroke-linecap": "round",
        })
    return ET.tostring(root, encoding="unicode")

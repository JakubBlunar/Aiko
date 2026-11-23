"""Build a transparent, pivot-aligned rig folder from neutral artwork and masks."""

import argparse
import json
import math
import re
from pathlib import Path
from tempfile import TemporaryDirectory

from PIL import Image, ImageChops, ImageDraw


def point(value):
    if len(value) != 2 or not all(isinstance(n, (int, float)) and math.isfinite(n) for n in value):
        raise ValueError(f"Expected two finite coordinates, got {value!r}")
    return tuple(value)


def layer_image(sheet, part, root):
    with Image.open(root / part["image"]) if "image" in part else sheet.copy() as source:
        image = source.convert("RGBA")
    if image.size != sheet.size:
        raise ValueError(f"Full-canvas image required for {part['id']}")
    if "polygon" in part and "mask" in part:
        raise ValueError("Choose one mask or polygon per layer")
    if "mask" in part:
        with Image.open(root / part["mask"]) as file:
            mask = file.convert("L")
        if mask.size != sheet.size:
            raise ValueError(f"Full-canvas grayscale mask required for {part['id']}")
        image.putalpha(ImageChops.multiply(image.getchannel("A"), mask))
    elif "polygon" in part:
        points = [point(p) for p in part["polygon"]]
        if len(points) < 3:
            raise ValueError("A polygon needs three or more points")
        # Supersample only the cut region. Full character sheets can be several thousand pixels.
        left = max(0, math.floor(min(x for x, _ in points)) - 1)
        top = max(0, math.floor(min(y for _, y in points)) - 1)
        right = min(sheet.width, math.ceil(max(x for x, _ in points)) + 2)
        bottom = min(sheet.height, math.ceil(max(y for _, y in points)) + 2)
        if left >= right or top >= bottom:
            raise ValueError(f"Polygon outside sheet: {part['id']}")
        image = image.crop((left, top, right, bottom))
        mask = Image.new("L", (image.width * 4, image.height * 4))
        ImageDraw.Draw(mask).polygon([((x-left)*4, (y-top)*4) for x, y in points], fill=255)
        image.putalpha(ImageChops.multiply(image.getchannel("A"),
                                           mask.resize(image.size, Image.Resampling.BOX)))
        return trimmed(image, left, top, part["id"])
    return trimmed(image, 0, 0, part["id"])


def trimmed(image, x, y, name):
    box = image.getchannel("A").getbbox()
    if not box:
        raise ValueError(f"Empty layer: {name}")
    return image.crop(box), box[0] + x, box[1] + y


def build(recipe, output):
    spec = json.loads(recipe.read_text(encoding="utf-8"))
    if output.exists():
        raise FileExistsError(f"Choose a new output directory: {output}")
    root = recipe.parent
    output.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=output.parent, prefix=".cut-") as scratch:
        dest = Path(scratch) / "rig"
        dest.mkdir()
        pivots = {}
        bones = []
        layers = []
        with Image.open(root / spec["sheet"]) as sheet:
            width, height = sheet.size
            for part in spec["parts"]:
                name = part["id"]
                if not re.fullmatch(r"[a-z][a-z0-9_.-]*", name) or name in pivots:
                    raise ValueError(f"Invalid or duplicate part ID: {name}")
                parent = part.get("parent")
                if parent is not None and parent not in pivots:
                    raise ValueError(f"Declare parent {parent} before {name}")
                px, py = point(part["pivot"])
                bx, by = pivots[parent] if parent else (0, 0)
                pivots[name] = px, py
                bone = {"id": name, "x": px-bx, "y": py-by}
                if parent:
                    bone["parent"] = parent
                if "drives" in part:
                    bone["drives"] = part["drives"]
                bones.append(bone)
                image, x, y = layer_image(sheet, part, root)
                image.save(dest / (name + ".png"))
                layers.append({"id": name, "bone": name, "src": name + ".png",
                               "x": x-px, "y": y-py})
        order = spec.get("drawOrder", list(pivots))
        if len(order) != len(layers) or set(order) != set(pivots):
            raise ValueError("drawOrder must list each part once")
        by_id = {layer["id"]: layer for layer in layers}
        rig = {"version": 1, "name": spec["name"], "width": width, "height": height,
               "parameters": spec.get("parameters", []), "bones": bones,
               "layers": [by_id[name] for name in order], "clips": spec.get("clips", [])}
        (dest / "rig.json").write_text(json.dumps(rig, indent=2) + "\n", encoding="utf-8",
                                       newline="\n")
        dest.rename(output)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("recipe", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    build(args.recipe, args.output)

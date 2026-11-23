"""Turn a locally installed Alexia reference into editable Rig Studio pieces and shape keys."""
import argparse
import json
import math
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory

from PIL import Image, ImageChops, ImageDraw, ImageFilter

STUDIO = Path(__file__).resolve().parent
DEFAULT_MODEL = STUDIO.parents[1] / "data/personas/active/Alexia/Alexia.model3.json"


def cut_texture(atlas, surface, destination):
    uv = surface["uv"]
    points = [(u * atlas.width, v * atlas.height) for u, v in zip(uv[::2], uv[1::2], strict=True)]
    # Some authoring vertices sit just outside the source sheet. Preserve their coordinates.
    left = max(0, math.floor(min(x for x, _ in points)) - 3)
    top = max(0, math.floor(min(y for _, y in points)) - 3)
    right = min(atlas.width, math.ceil(max(x for x, _ in points)) + 3)
    bottom = min(atlas.height, math.ceil(max(y for _, y in points)) + 3)
    tile = atlas.crop((left, top, right, bottom))
    local = [(x-left, y-top) for x, y in points]
    mask = Image.new("L", tile.size)
    pen = ImageDraw.Draw(mask)
    indices = surface["indices"]
    for i in range(0, len(indices), 3):
        pen.polygon([local[k] for k in indices[i:i+3]], fill=255)
    tile.putalpha(ImageChops.multiply(tile.getchannel("A"), mask.filter(ImageFilter.MaxFilter(5))))
    tile.save(destination)
    surface["uv"] = [round(n, 7) for x, y in local for n in (x/tile.width, y/tile.height)]
    return [left, top, right, bottom]


def build(model, output, atlas_size):
    if output.exists():
        raise FileExistsError(f"Choose a new export folder to preserve edited layers: {output}")
    if atlas_size < 1:
        raise ValueError("Texture size must be positive")
    output.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["node", str(STUDIO / "build.mjs")], check=True)
    with TemporaryDirectory(dir=output.parent, prefix=".atlas-") as temp:
        stage = Path(temp) / "character"
        stage.mkdir()
        dump = Path(temp) / "sample.json"
        sampler = STUDIO / "sample-atlas.mjs"
        subprocess.run(["node", str(sampler), str(model), str(dump)], check=True)
        data = json.loads(dump.read_text(encoding="utf-8"))
        rig = data["rig"]
        for index, filename in enumerate(data["textures"]):
            with Image.open(model.parent / filename) as source:
                source.thumbnail((atlas_size, atlas_size), Image.Resampling.LANCZOS)
                atlas = source.convert("RGBA")
            for layer, record in zip(rig["layers"], data["inventory"], strict=True):
                if record["atlas"] == index:
                    record["crop"] = cut_texture(atlas, layer["surface"], stage / layer["src"])
                    record["texture"] = filename
                    record["atlasSize"] = list(atlas.size)
            atlas.close()
        for name, value in (("rig", rig), ("inventory", data["inventory"])):
            (stage / (name + ".json")).write_text(json.dumps(value, separators=(",", ":")),
                                                  encoding="utf-8")
        stage.rename(output)
    print(f"Built {len(rig['layers'])} editable pieces and {len(rig['parameters'])} controls")
    print(output)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", type=Path, default=DEFAULT_MODEL)
    parser.add_argument("--output", type=Path, default=STUDIO / "private/aiko")
    parser.add_argument("--texture-size", type=int, default=4096)
    args = parser.parse_args()
    build(args.model.resolve(), args.output.resolve(), args.texture_size)

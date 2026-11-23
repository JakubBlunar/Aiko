"""End-to-end checks for cutting full-canvas art and keeping joint registration."""
import json
import subprocess
import sys
from pathlib import Path

from PIL import Image

CUT = Path(__file__).resolve().parents[1] / "cut.py"


def test_cut_and_reassemble(tmp_path):
    sheet = Image.new("RGBA", (16, 12))
    sheet.paste((21, 83, 32, 255), (2, 2, 10, 11))
    sheet.save(tmp_path / "base.png")
    mask = Image.new("L", sheet.size)
    mask.paste(255, (2, 2, 10, 11))
    mask.save(tmp_path / "mask.png")
    arm = Image.new("RGBA", sheet.size)
    arm.paste((253, 198, 24, 255), (8, 5, 14, 8))
    arm.save(tmp_path / "arm.png")
    spec = {"name": "Fixture", "sheet": "base.png", "parts": [
        {"id": "body", "pivot": [6, 8], "mask": "mask.png"},
        {"id": "arm", "parent": "body", "pivot": [9, 6], "image": "arm.png",
         "polygon": [[7, 4], [15, 4], [15, 9], [7, 9]]}],
        "drawOrder": ["arm", "body"]}
    recipe = tmp_path / "cut.json"
    recipe.write_text(json.dumps(spec))
    dest = tmp_path / "rig"
    cmd = [sys.executable, str(CUT), str(recipe), str(dest)]
    subprocess.run(cmd, check=True)
    rig = json.loads((dest / "rig.json").read_text())
    assert rig["bones"][1] == {"id": "arm", "parent": "body", "x": 3, "y": -2}
    assert [layer["id"] for layer in rig["layers"]] == ["arm", "body"]
    reconstructed = Image.new("RGBA", sheet.size)
    for layer, part in zip(rig["layers"], reversed(spec["parts"]), strict=True):
        x, y = part["pivot"]
        with Image.open(dest / layer["src"]) as image:
            reconstructed.alpha_composite(image, (x + layer["x"], y + layer["y"]))
    assert reconstructed.getpixel((6, 6)) == (21, 83, 32, 255)
    assert reconstructed.getpixel((12, 6)) == (253, 198, 24, 255)
    assert reconstructed.getpixel((15, 11))[-1] == 0
    assert subprocess.run(cmd, capture_output=True).returncode != 0
    assert json.loads((dest / "rig.json").read_text()) == rig


def test_failed_cut_does_not_leave_a_partial_rig(tmp_path):
    Image.new("RGBA", (4, 4), "red").save(tmp_path / "base.png")
    recipe = tmp_path / "cut.json"
    recipe.write_text(json.dumps({"name": "X", "sheet": "base.png", "parts": [
        {"id": "body", "pivot": [0, 0]}, {"id": "arm", "pivot": [0, 0], "parent": "absent"}]}))
    dest = tmp_path / "new"
    run = subprocess.run([sys.executable, str(CUT), str(recipe), str(dest)], capture_output=True)
    assert run.returncode and b"Declare parent" in run.stderr
    assert not dest.exists()
    assert not list(tmp_path.glob(".cut-*"))

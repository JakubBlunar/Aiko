"""Inspect the body, replaceable clothing and spring in a locally served wardrobe."""
import argparse
import json
import shutil
import socket
import subprocess
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic, sleep
from urllib.error import URLError
from urllib.request import urlopen

from PIL import Image, ImageChops, ImageDraw, ImageStat
from playwright.sync_api import expect, sync_playwright

STUDIO = Path(__file__).resolve().parents[1]


def check_edit(workspace, browser_path):
    scratch = STUDIO.parents[1] / ".tmp-tests"
    scratch.mkdir(exist_ok=True)
    with TemporaryDirectory(dir=scratch, prefix="run-wardrobe-edit-") as tmp:
        root = Path(tmp) / "character"
        shutil.copytree(workspace, root)
        body = (root / "body.json").read_bytes()
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        server = subprocess.Popen(["node", "server.mjs", "--rig", str(root), "--port", str(port)],
                                  cwd=STUDIO)
        url = f"http://127.0.0.1:{port}"
        try:
            deadline = monotonic() + 20
            while True:
                try:
                    with urlopen(url + "/local/info", timeout=1):
                        break
                except URLError:
                    if server.poll() is not None or monotonic() >= deadline:
                        raise
                    sleep(.1)
            with sync_playwright() as pw:
                browser = pw.chromium.launch(executable_path=browser_path,
                                             args=["--enable-unsafe-swiftshader"])
                page = browser.new_page()
                page.goto(url)
                outfit = page.get_by_label("Outfit", exact=True)
                outfit.select_option("4")
                page.locator("#motion").select_option("wrist")
                page.locator("#seek").fill("0.75")
                atlas = page.locator("#atlas")
                before = Image.open(BytesIO(atlas.screenshot())).convert("RGB")
                file = root / "costumes/studio-dress.json"
                costume = json.loads(file.read_bytes())
                costume["label"] = "Repainted dress"
                costume["pieces"][0]["src"] = "dress/lining.svg"
                file.write_text(json.dumps(costume), encoding="utf-8")
                expect(outfit.locator("option:checked")).to_have_text("Repainted dress")
                expect(page.locator("#seek")).to_have_value("0.75")
                after = Image.open(BytesIO(atlas.screenshot())).convert("RGB")
                assert max(ImageStat.Stat(ImageChops.difference(before, after)).mean) > .05
                assert (root / "body.json").read_bytes() == body
                browser.close()
        finally:
            server.terminate()
            server.wait(timeout=10)


def run(url, browser_path, output, original_url):
    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=browser_path,
                                     args=["--enable-unsafe-swiftshader"])
        try:
            page = browser.new_page(viewport={"width": 1600, "height": 1050})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(url)
            outfit = page.get_by_label("Outfit", exact=True)
            outfit.wait_for(timeout=45000)
            style = ".views .surface{width:380px;height:700px;background:#505b66}"
            page.add_style_tag(content=style)
            page.locator("#motion").select_option("")
            atlas = page.locator("#atlas")

            def frame():
                page.evaluate("() => new Promise(r => requestAnimationFrame("
                              "() => requestAnimationFrame(r)))")
                return Image.open(BytesIO(atlas.screenshot())).convert("RGB")

            images = []
            for value in ("3", "4", "0", "1", "2"):
                outfit.select_option(value)
                image = frame()
                assert all(ImageChops.difference(image, im).getbbox() for _, im in images)
                images.append((outfit.locator("option:checked").inner_text(), image))
                if value == "3":
                    with page.expect_download() as exported:
                        page.get_by_role("button", name="Export pose PNG").click()
                    png = Image.open(exported.value.path()).convert("RGBA")
                    assert png.size == (1125, 2048) and png.getchannel("A").getextrema() == (0, 255)
                    if output:
                        png.save(output.with_name("body-fitting.png"))
            outfit.select_option("4")
            page.locator("#motion").select_option("foot-tap")
            assert outfit.input_value() == "4"
            page.locator("#seek").fill("0.3")
            hem = page.get_by_label("Skirt swing")
            swing = float(hem.input_value())
            assert abs(swing - .06) > .01
            page.locator("#seek").fill("0.9")
            page.locator("#seek").fill("0.3")
            assert float(hem.input_value()) == swing
            hem.fill("1")
            a = frame()
            hem.fill("-1")
            assert ImageChops.difference(a, frame()).getbbox()
            page.locator("#motion").select_option("")
            elbow = page.get_by_label("Left elbow bend", exact=True)
            before = frame()
            elbow.fill("18")
            assert ImageChops.difference(before, frame()).getbbox()
            if original_url:
                original = browser.new_page(viewport={"width": 1600, "height": 1050})
                original.goto(original_url)
                original.get_by_label("Outfit", exact=True).wait_for(timeout=45000)
                original.add_style_tag(content=style)
                for value in ("0", "1", "2"):
                    for view in (original, page):
                        view.locator("#motion").select_option("wrist")
                        view.get_by_label("Outfit", exact=True).select_option(value)
                        view.locator("#seek").fill("0.75")
                    captured = original.locator("#atlas").screenshot()
                    old = Image.open(BytesIO(captured)).convert("RGB")
                    delta = max(ImageStat.Stat(ImageChops.difference(old, frame())).mean)
                    assert delta < .1, (value, delta)
                    print(f"Stock outfit {value} mean channel difference {delta:.4f}")
            assert not errors, errors
            if output:
                w, h = images[0][1].size
                sheet = Image.new("RGB", (w * len(images), h + 28), "#fffdf9")
                draw = ImageDraw.Draw(sheet)
                for i, (label, image) in enumerate(images):
                    sheet.paste(image, (i*w, 0))
                    draw.text((i*w+10, h+6), label, fill="#322e29")
                sheet.save(output)
        finally:
            browser.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:5177")
    parser.add_argument("--browser")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--original-url")
    parser.add_argument("--check-edit", type=Path)
    args = parser.parse_args()
    run(args.url, args.browser, args.output, args.original_url)
    if args.check_edit:
        check_edit(args.check_edit, args.browser)

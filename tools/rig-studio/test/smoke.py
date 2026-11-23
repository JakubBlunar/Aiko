"""Exercise the built studio in local Chromium, optionally including the installed reference."""
import argparse
import socket
import subprocess
import time
from io import BytesIO
from pathlib import Path
from urllib.request import urlopen

from PIL import Image, ImageChops
from playwright.sync_api import sync_playwright

STUDIO = Path(__file__).resolve().parents[1]


def opaque_colors(png):
    image = Image.open(BytesIO(png)).convert("RGB")
    return len(image.resize((120, 160)).getcolors(20000))


def same_pixels(first, second, tolerance=0):
    with Image.open(BytesIO(first)) as a, Image.open(BytesIO(second)) as b:
        if a.size != b.size:
            return False
        diff = ImageChops.difference(a.convert("RGB"), b.convert("RGB"))
        return all(high <= tolerance for _, high in diff.getextrema())


def run(browser_path, reference):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    origin = f"http://127.0.0.1:{port}"
    server = subprocess.Popen(["node", "server.mjs", "--port", str(port)], cwd=STUDIO)
    try:
        for _ in range(100):
            if server.poll() is not None:
                raise RuntimeError("Studio server exited")
            try:
                with urlopen(origin, timeout=1):
                    break
            except OSError:
                time.sleep(.1)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=browser_path,
                                                  args=["--enable-unsafe-swiftshader"])
            page = browser.new_page(viewport={"width": 1200, "height": 800})
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto(origin + "/?rig=example")
            page.get_by_role("heading", name="Pip", exact=True).wait_for()
            page.locator("#motion").select_option("wave")
            page.locator("#seek").fill("0.5")
            frame = page.locator("#custom").screenshot()
            page.locator("#next").click()
            assert page.locator("#time").inner_text().startswith("0.533")
            page.locator("#prev").click()
            assert same_pixels(page.locator("#custom").screenshot(), frame)
            page.locator("#play").click()
            page.wait_for_timeout(140)
            page.locator("#play").click()
            assert page.locator("#time").inner_text() != "0.500 / 2.000 s"
            page.get_by_label("Head tilt", exact=True).fill("12")
            assert "manual" in page.locator("#parameters").inner_text()
            page.locator("#reset").click()
            assert "manual" not in page.locator("#parameters").inner_text()
            page.locator("#joints summary").click()
            page.locator("#joint").select_option("hand.left")
            page.locator("#jointX").fill("2")
            page.locator("#jointX").press("Tab")
            assert page.locator("#bones").is_checked()
            with page.expect_download() as download:
                page.locator("#save").click()
            assert download.value.suggested_filename == "rig.json"
            page.locator("#folder").set_input_files(STUDIO / "example")
            page.wait_for_function("document.querySelector('#joint').value === 'body'")
            if reference:
                page.locator("#loadRef").click()
                page.wait_for_function(
                    "document.querySelector('#refPrompt').hidden ||"
                    " !document.querySelector('#loadRef').disabled", timeout=100000)
                message = page.locator("#refStatus").inner_text()
                assert page.locator("#refPrompt").is_hidden(), message
                linked = page.locator("#parameters").inner_text()
                assert "reference linked" in linked and "custom only" in linked
                before = page.locator("#reference").screenshot()
                page.get_by_label("Head tilt", exact=True).fill("25")
                after = page.locator("#reference").screenshot()
                assert opaque_colors(after) > 120 and not same_pixels(before, after)
                assert same_pixels(page.locator("#reference").screenshot(), after)
                with page.expect_download() as neutral:
                    page.locator("#neutral").click()
                with Image.open(neutral.value.path()) as plate:
                    assert max(plate.size) == 2048
                    assert plate.getchannel("A").getextrema() == (0, 255)
                page.wait_for_function("!document.querySelector('#neutral').disabled")
                # Resizing the WebGL framebuffer can round an edge pixel by one RGB level.
                assert same_pixels(page.locator("#reference").screenshot(), after, tolerance=1)
            assert not errors, errors
            browser.close()
    finally:
        server.terminate()
        server.wait(timeout=10)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--browser", help="Path to a local Chromium executable")
    parser.add_argument("--reference", action="store_true")
    args = parser.parse_args()
    run(args.browser, args.reference)
    print("Studio browser checks passed")

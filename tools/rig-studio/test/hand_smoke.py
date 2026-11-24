"""Verify finger poses on both original outfits in the local wardrobe studio."""
import argparse
import base64
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw
from playwright.sync_api import sync_playwright

HANDS = {"Right": (150, 902, 318, 1066), "Left": (807, 902, 975, 1066)}

def changed_pixels(a, b):
    # Ignore subpixel raster variation when the export resizes the WebGL canvas.
    return ImageChops.difference(a, b).convert("L").point(lambda v: 255 if v > 10 else 0).getbbox()


def run(url, browser_path, output):
    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=browser_path,
                                     args=["--enable-unsafe-swiftshader"])
        try:
            page = browser.new_page(viewport={"width": 1400, "height": 900})
            page.goto(url)
            outfit = page.get_by_label("Outfit", exact=True)
            outfit.wait_for(timeout=45000)
            assert outfit.input_value() == "0"
            motion = page.locator("#motion")
            motion.select_option("")
            page.add_style_tag(content="#atlas {width:1125px;height:2048px}")

            def frame():
                # Read the public preview in the same paint as the WebGL draw.
                data = page.evaluate("""() => {
                    document.querySelector('#bones').dispatchEvent(new Event('change'))
                    return document.querySelector('#atlas').toDataURL().split(',')[1]
                }""")
                with Image.open(BytesIO(base64.b64decode(data))) as png:
                    assert png.size == (1125, 2048)
                    image = Image.new("RGB", png.size, "#455361")
                    image.paste(png, mask=png.getchannel("A"))
                return image

            for style in ("0", "1", "2"):
                outfit.select_option(style)
                neutral = frame()
                for side, bounds in HANDS.items():
                    for finger in ("index", "middle", "ring", "little"):
                        slider = page.get_by_label(f"{side} {finger} bend", exact=True)
                        slider.fill("1")
                        box = changed_pixels(frame(), neutral)
                        assert box and bounds[0] <= box[0] < box[2] <= bounds[2], (side, box)
                        assert bounds[1] + 45 < box[1] < box[3] <= bounds[3], (finger, box)
                        slider.fill("0")
                assert changed_pixels(frame(), neutral) is None

            outfit.select_option("0")
            arms = page.get_by_label("Casual arm pose", exact=True)
            arms.select_option("1")
            assert page.get_by_label("Left index bend", exact=True).is_disabled()
            arms.select_option("0")
            motion.select_option("beckon")
            page.locator("#seek").fill("0.72")
            assert page.get_by_label("Right index bend", exact=True).input_value() == "1"
            assert float(page.get_by_label("Right wrist bend").input_value()) > 0
            motion.select_option("fingers")
            seek = page.locator("#seek")
            seek.fill("1.25")
            held = frame()
            seek.fill("2.75")
            seek.fill("1.25")
            assert changed_pixels(frame(), held) is None
            if output:
                frames = []
                for i in range(36):
                    seek.fill(f"{i / 12:g}")
                    image = frame()
                    pair = Image.new("RGB", (336, 188), "#455361")
                    draw = ImageDraw.Draw(pair)
                    for j, (side, box) in enumerate(HANDS.items()):
                        pair.paste(image.crop(box), (168*j, 0))
                        draw.text((168*j+8, 168), side + " hand", fill="white")
                    frames.append(pair)
                frames[0].save(output, save_all=True, append_images=frames[1:],
                               duration=83, loop=0)
        finally:
            browser.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:5177")
    parser.add_argument("--browser")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    run(args.url, args.browser, args.output)

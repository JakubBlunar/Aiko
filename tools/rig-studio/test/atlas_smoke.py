"""Compare the private atlas rig against the installed reference in local Chromium."""
import argparse
import base64
import json
import socket
import subprocess
import sys
from dataclasses import asdict
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageStat
from playwright.sync_api import sync_playwright

STUDIO = Path(__file__).resolve().parents[1]
ROOT = STUDIO.parents[1]


def pixels(locator):
    locator.evaluate("() => new Promise(done => requestAnimationFrame("
                     "() => requestAnimationFrame(done)))")
    return Image.open(BytesIO(locator.screenshot())).convert("RGB")


def save_tail_loop(page, custom, reference, output):
    frames = []
    for index in range(40):
        page.locator("#seek").fill(f"{index / 20:g}")
        halves = [pixels(pane).crop((0, 210, 300, 430)) for pane in (custom, reference)]
        pair = Image.new("RGB", (600, 220))
        pair.paste(halves[0])
        pair.paste(halves[1], (300, 0))
        frames.append(pair)
    frames[0].save(output / "tail-swing.gif", save_all=True, append_images=frames[1:],
                   duration=50, loop=0)


def check_tail_gestures(page, custom, reference, output):
    motion, seek = page.locator("#motion"), page.locator("#seek")
    phase = page.get_by_label("Tail swing phase", exact=True)
    panes = (custom, reference)
    preview = []
    specs = [
        ("tail_wag_slow", "Slow wag", 4, [(1, .16), (3, -.16)]),
        ("tail_wag_fast", "Fast wag", 2, [(.5, -.46), (1.5, .46)]),
        ("tail_flick", "Teasing flick", 2, [(.7, -.42), (1.2, -.42)]),
    ]
    for name, label, duration, samples in specs:
        motion.select_option(name)
        page.get_by_label("Layer with idle").uncheck()
        seek.fill("0")
        neutral = [pixels(p) for p in panes]
        endpoints = []
        for seconds, expected in samples:
            seek.fill(str(seconds))
            assert abs(float(phase.input_value()) - expected) < .001, (name, seconds)
            frames = [pixels(p) for p in panes]
            for rest, frame in zip(neutral, frames, strict=True):
                delta = ImageChops.difference(rest, frame).crop((0, 235, 230, 430))
                assert max(ImageStat.Stat(delta).mean) > .4, (name, seconds)
            assert compare(*frames)["silhouette_iou"] > .96, (name, seconds)
            endpoints.append(frames)
        if name == "tail_flick":
            for first, last in zip(*endpoints, strict=True):
                assert max(ImageStat.Stat(ImageChops.difference(first, last)).mean) < .05
        seek.fill(str(duration))
        for rest, pane in zip(neutral, panes, strict=True):
            assert max(ImageStat.Stat(ImageChops.difference(rest, pixels(pane))).mean) < .05
        if output:
            frames = []
            for i in range(duration * 15 + 1):
                # Copy the original Cubism pane in the same draw before WebGL clears it.
                data = page.evaluate("""seconds => {
                    const seek = document.querySelector('#seek')
                    seek.value = seconds
                    seek.dispatchEvent(new Event('input'))
                    const src = document.querySelector('#reference')
                    const dst = document.createElement('canvas')
                    dst.width = 245; dst.height = 208
                    const ctx = dst.getContext('2d')
                    ctx.fillStyle = '#30444e'; ctx.fillRect(0,0,245,208)
                    ctx.drawImage(src,0,212,245,208,0,0,245,208)
                    return dst.toDataURL().split(',')[1]
                }""", i / 15)
                with Image.open(BytesIO(base64.b64decode(data))) as frame:
                    frames.append(frame.convert("RGB"))
            preview.append((label, frames))
    if preview:
        strip = []
        for i in range(61):
            frame = Image.new("RGB", (735, 236), "#30444e")
            draw = ImageDraw.Draw(frame)
            for col, (label, frames) in enumerate(preview):
                frame.paste(frames[min(i, len(frames)-1)], (245*col, 28))
                draw.text((245*col+10, 8), label, fill="white")
            strip.append(frame)
        strip[0].save(output / "tail-gestures.gif", save_all=True, append_images=strip[1:],
                      duration=([70, 60, 70] * 21)[:61], loop=0)
    motion.select_option("")


def check_tail_physics(page):
    sys.path.insert(0, str(ROOT))
    from app.core.persona.avatar_profile import from_disk

    folder = ROOT / "data/personas/active/Alexia"
    profile = from_disk(folder)
    # Keep the editor's normal reference deterministic. Only this test request
    # includes the original physics file alongside the Studio mesh and textures.
    manifest = page.request.get(page.url + "reference/model.model3.json").json()
    manifest["FileReferences"]["Physics"] = "tail-test.physics3.json"
    page.route("**/tail-test.model3.json", lambda route: route.fulfill(json=manifest))
    page.route("**/tail-test.physics3.json", lambda route: route.fulfill(
        path=folder / "Alexia.physics3.json", content_type="application/json"))
    bundle = subprocess.check_output(["node", "-e", """
        const {buildSync} = require('./web/node_modules/esbuild')
        const result = buildSync({entryPoints:['tools/rig-studio/test/tail-physics.mjs'],
          external:['pixi-live2d-display'], bundle:true, format:'cjs', write:false})
        process.stdout.write(result.outputFiles[0].text)
    """], cwd=ROOT).decode()
    page.evaluate("""async ([source, manifest]) => {
        const module = {exports:{}}
        Function('require','module',source)(() => window.PIXI.live2d,module)
        await module.exports.check(manifest)
    }""", [bundle, asdict(profile)])
    print("Live2D physics verified both wag speeds, the five-joint hold and release.")


def check_inspector_scroll(page):
    panel = page.get_by_role("complementary", name="Pose inspector")
    surfaces = page.locator(".views .surface")
    transport = page.locator(".transport")
    for width, height in [(1280, 800), (1920, 1080)]:
        page.set_viewport_size({"width": width, "height": height})
        panel.evaluate("p => p.scrollTop = 0")
        previews = [view.bounding_box() for view in surfaces.all()]
        timeline = transport.bounding_box()
        assert previews[0]["height"] == previews[1]["height"] > height * .6
        assert timeline["y"] + timeline["height"] <= height
        panel.hover(position={"x": 8, "y": 80})
        page.mouse.wheel(0, panel.evaluate("p => p.scrollHeight"))
        page.wait_for_function(
            "(() => { const p = document.querySelector('aside');"
            " return p.scrollTop > 0 && p.scrollTop + p.clientHeight >= p.scrollHeight - 1 })()")
        assert page.evaluate("window.scrollY") == 0
        assert [view.bounding_box() for view in surfaces.all()] == previews
        assert transport.bounding_box() == timeline
        print(f"{width}x{height} preview height {previews[0]['height']:.0f}px")
    panel.evaluate("p => p.scrollTop = 0")


def check_overlays(page, custom, reference, output):
    page.set_viewport_size({"width": 2200, "height": 1380})
    style = page.add_style_tag(content=".views .surface{width:600px;height:1040px}")
    face = (164, 14, 438, 220)

    def faces():
        return [pixels(pane).crop(face) for pane in (custom, reference)]

    neutral = faces()
    labels = ("Question mark", "Sweat drop", "Angry shadow", "Left purple iris",
              "Right purple iris", "Lollipop", "Glasses on face", "Sunglasses on head")
    gallery = Image.new("RGB", (neutral[0].width * 4, (neutral[0].height + 24) * 2), "#fffdf9")
    draw = ImageDraw.Draw(gallery)
    for index, label in enumerate(labels):
        slider = page.get_by_label(label, exact=True)
        slider.fill("1")
        custom_face, ref_face = faces()
        for source, baseline in zip((custom_face, ref_face), neutral, strict=True):
            change = ImageChops.difference(source, baseline).convert("L")
            assert change.getextrema()[1] > 12, label
        x = index % 4 * custom_face.width
        y = index // 4 * (custom_face.height + 24)
        gallery.paste(custom_face, (x, y))
        draw.text((x + 6, y + custom_face.height + 5), label, fill="#322e29")
        slider.fill("0")
    if output:
        gallery.save(output / "overlays.png")
    iris = page.get_by_label("Left purple iris", exact=True)
    iris.fill("0.75")
    eye_style = page.get_by_label("Eye expression", exact=True)
    eye_style.select_option("1")
    assert iris.input_value() == "0.75"
    eye_style.select_option("2")
    assert iris.is_disabled() and iris.input_value() == "0"
    eye_style.select_option("0")
    assert iris.is_enabled() and iris.input_value() == "0.75"
    page.locator("#reset").click()
    style.evaluate("s => s.remove()")
    page.set_viewport_size({"width": 1280, "height": 1000})


def compare(a, b):
    background = Image.new("RGB", a.size, (48, 68, 78))

    def foreground(image):
        return ImageChops.difference(image, background).convert("L").point(lambda n: 255 if n > 8
                                                                          else 0)

    ma, mb = foreground(a), foreground(b)
    union = ImageChops.lighter(ma, mb)
    intersection = ImageChops.darker(ma, mb)
    diff = ImageChops.difference(a, b)
    return {"silhouette_iou": round(intersection.histogram()[255] / union.histogram()[255], 4),
            "mean_foreground_rgb_error": round(sum(ImageStat.Stat(diff, union).mean) / 3, 2)}


def run(browser_path, output=None):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = subprocess.Popen(["node", "server.mjs", "--port", str(port)], cwd=STUDIO)
    if output:
        output.mkdir(parents=True, exist_ok=True)
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(executable_path=browser_path,
                                         args=["--enable-unsafe-swiftshader"])
            page = browser.new_page(viewport={"width": 1280, "height": 1000})
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto(f"http://127.0.0.1:{port}")
            page.get_by_role("heading", name="Aiko", exact=True).wait_for(timeout=45000)
            check_inspector_scroll(page)
            page.set_viewport_size({"width": 1280, "height": 1000})
            style = ".views .surface{width:300px;height:520px;background:#30444e}"
            page.add_style_tag(content=style)
            page.locator("#motion").select_option("")
            page.locator("#loadRef").click()
            page.locator("#neutral").wait_for(timeout=90000)
            custom, reference = page.locator("#atlas"), page.locator("#reference")
            results = {}

            def compare_pose(name):
                a, b = pixels(custom), pixels(reference)
                results[name] = compare(a, b)
                if output:
                    pair = Image.new("RGB", (a.width*2, a.height))
                    pair.paste(a)
                    pair.paste(b, (a.width, 0))
                    pair.save(output / (name + ".png"))
                return a

            check_tail_gestures(page, custom, reference, output)
            check_tail_physics(page)
            check_overlays(page, custom, reference, output)
            neutral = compare_pose("casual")
            page.locator("#bones").check()
            assert ImageChops.difference(pixels(custom), neutral).getbbox()
            page.locator("#bones").uncheck()
            for control in ("Ponytail sway", "Waist ribbon sway", "Left elbow bend",
                            "Right knee bend", "Left foot tilt"):
                slider = page.get_by_label(control, exact=True)
                slider.fill("8")
                assert ImageChops.difference(pixels(custom), neutral).getbbox(), control
                slider.fill("0")
            for label, value, name in [
                ("Left ear flex", "1", "ear-left"), ("Right ear flex", "-1", "ear-right"),
                ("Chest lift", "1", "chest"), ("Eye opening", "0", "blink"),
                ("Gaze horizontal", "1", "gaze"), ("Head turn", "24", "turn")]:
                page.get_by_label(label, exact=True).fill(value)
                frame = compare_pose(name)
                assert ImageChops.difference(frame, neutral).getbbox(), name
                page.locator("#reset").click()
            for value, name in [("1", "pajamas-hood-up"), ("2", "pajamas-hood-down")]:
                page.get_by_label("Outfit", exact=True).select_option(value)
                clothes = compare_pose(name)
                assert page.get_by_label("Casual arm pose").is_disabled()
                for control in ("Left knee bend", "Right foot tilt", "Left elbow bend"):
                    page.get_by_label(control, exact=True).fill("8")
                    assert ImageChops.difference(pixels(custom), clothes).getbbox(), control
                    page.get_by_label(control, exact=True).fill("0")
            page.locator("#reset").click()
            page.get_by_label("Casual arm pose").select_option("1")
            compare_pose("crossed-arms")
            page.locator("#reset").click()
            page.get_by_label("Left wrist bend").fill("24")
            wrist = pixels(custom)
            assert ImageChops.difference(neutral, wrist).getbbox()
            page.locator("#reset").click()
            page.get_by_label("Toothy grin", exact=True).fill("0.45")
            half_grin = compare_pose("grin-transition")
            page.get_by_label("Toothy grin", exact=True).fill("1")
            assert page.get_by_label("Mouth opening").is_disabled()
            grin = compare_pose("grin")
            assert ImageChops.difference(half_grin, grin).getbbox()
            page.get_by_label("Jaw drop").fill("0.7")
            assert ImageChops.difference(grin, compare_pose("grin-jaw")).getbbox()
            page.locator("#motion").select_option("tail")
            page.get_by_label("Layer with idle").uncheck()
            first = compare_pose("tail-start")
            assert page.get_by_label("Tail swing phase", exact=True).input_value() == "-1"
            for name, seconds in [("outbound", "0.37"), ("return", "1.41")]:
                page.locator("#seek").fill(seconds)
                frame = compare_pose("tail-" + name)
                assert ImageChops.difference(first, frame).getbbox()
            page.locator("#seek").fill("2")
            assert page.get_by_label("Tail swing phase", exact=True).input_value() == "1"
            seam = ImageChops.difference(first, compare_pose("tail-end"))
            # The geometry check requires exact equality. Allow subpixel raster noise here.
            assert max(ImageStat.Stat(seam).mean) < .1, seam.getbbox()
            if output:
                save_tail_loop(page, custom, reference, output)
            for motion, seconds, control, expected in [
                ("nod", "0.3", "Head pitch", "-15"), ("shake", "0.7", "Head turn", "15"),
                ("puzzled", "0.48", "Question mark", "1"),
            ]:
                page.locator("#motion").select_option(motion)
                page.locator("#seek").fill(seconds)
                assert page.get_by_label(control, exact=True).input_value() == expected
                assert ImageChops.difference(compare_pose(motion), neutral).getbbox()
            page.locator("#motion").select_option("bow")
            page.locator("#seek").fill("0.3")
            bow = compare_pose("bow")
            page.locator("#next").click()
            page.locator("#prev").click()
            assert abs(float(page.locator("#seek").input_value()) - .3) < 1e-6
            assert page.get_by_label("Head pitch", exact=True).input_value() == "-20"
            bow_delta = ImageStat.Stat(ImageChops.difference(pixels(custom), bow)).mean
            assert max(bow_delta) < .1, bow_delta
            page.locator("#follow").check()
            custom.hover(position={"x": 240, "y": 120})
            assert float(page.get_by_label("Gaze horizontal", exact=True).input_value()) > .3
            compare_pose("pointer")
            assert not errors, errors
            print(json.dumps(results, indent=2))
            assert all(r["silhouette_iou"] > .93 and r["mean_foreground_rgb_error"] < 9
                       for r in results.values()), results
            if output:
                (output / "metrics.json").write_text(json.dumps(results, indent=2))
            browser.close()
    finally:
        server.terminate()
        server.wait(timeout=10)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--browser")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    run(args.browser, args.output)

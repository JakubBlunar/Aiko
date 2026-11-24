"""Exercise layered Aiko playback and scrubbing in a running Rig Studio."""
import argparse
import base64
import json
from io import BytesIO
from pathlib import Path

from PIL import Image
from playwright.sync_api import expect, sync_playwright


def check_tail_handoff(page):
    # Drive the visible controls through the turn, interrupt at several points,
    # and compare the same future frames with an uninterrupted playback.
    result = page.evaluate("""() => {
        const seek = document.getElementById('seek'), motion = document.getElementById('motion')
        const tail = document.getElementById('param-tail.curl'), play = document.getElementById('play')
        function choose(id) { motion.value = id; motion.dispatchEvent(new Event('change')) }
        function at(t) { seek.value = t; seek.dispatchEvent(new Event('input')); return tail.valueAsNumber }
        function begin() {
            choose(''); choose('@idle'); at(3.75)
            const before = tail.valueAsNumber
            play.click(); choose('tail'); play.click()
            if (tail.valueAsNumber !== before) throw Error('Tail jumped at launch')
        }
        begin()
        const full = Array.from({length:217}, (_,i) => at(i/30))
        let peak = 0
        for (let i=1;i<full.length;i++) {
            const delta = Math.abs(full[i]-full[i-1])
            peak = Math.max(peak, Math.min(delta,2-delta))
        }
        for (const frame of [18,90,162]) {
            begin(); at(frame/30); play.click(); choose('wrist'); play.click()
            for (const dt of [0,6,18,42]) {
                if (Math.abs(at(dt/30)-full[frame+dt])>1e-8) throw Error('Interrupted turn snapped')
            }
        }
        begin(); at(3); choose('tail-sway')
        let previous = at(0)
        for (let i=1;i<=30;i++) {
            const next = at(i/30), delta = Math.abs(next-previous)
            if (Math.min(delta,2-delta) > .06) throw Error('Changing sway interrupted the turn')
            previous = next
        }
        choose(''); choose('@idle')
        const idleEnd = at(3.75+6)
        return {peak, endError:Math.abs(full[180]-idleEnd)}
    }""")
    assert result["peak"] < .03 and result["endError"] < 1e-8, result


def run(url, browser_path, output):
    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=browser_path,
                                     args=["--enable-unsafe-swiftshader"])
        try:
            page = browser.new_page(viewport={"width": 1380, "height": 880})
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto(url)
            motion, seek = page.locator("#motion"), page.locator("#seek")
            expect(motion).to_have_value("@idle", timeout=45000)
            expect(seek).to_have_attribute("max", "12")
            channels = [page.get_by_label(label, exact=True) for label in
                        ("Chest lift", "Tail swing phase", "Right little bend",
                         "Left index bend", "Left wrist bend")]

            def pose(seconds):
                seek.fill(str(seconds))
                return [float(c.input_value()) for c in channels]

            start, peak = pose(0), pose(.75)
            assert all(a != b for a, b in zip(start[:3], peak[:3], strict=True))
            assert pose(12) == start
            page.get_by_text("Idle layers", exact=True).click()
            tail_strength = page.get_by_label("Light tail sway", exact=True)
            tail_strength.fill("0")
            muted = pose(.75)
            assert muted[1] == 0 and muted[:1] + muted[2:] == peak[:1] + peak[2:]
            page.get_by_text("Edit joint pivot", exact=True).click()
            with page.expect_download() as saved:
                page.get_by_role("button", name="Export rig.json").click()
            exported = json.loads(saved.value.path().read_bytes())
            assert next(layer["weight"] for layer in exported["idleMotion"]["layers"]
                        if layer["clip"] == "tail-sway") == 0
            tail_strength.fill("100")
            settled = pose(2.4)
            motion.select_option("wrist")
            mixed = pose(.75)
            assert mixed[:3] == peak[:3]
            assert mixed[3] != peak[3] and mixed[4] != 0
            assert pose(2.4) == settled
            assert pose(.75) == mixed
            page.locator("#next").click()
            page.locator("#prev").click()
            assert [float(c.input_value()) for c in channels] == mixed
            page.get_by_label("Layer with idle").uncheck()
            expect(seek).to_have_attribute("max", "2")
            solo = pose(.75)
            assert solo[:3] == [0, 0, 0] and solo[3:] == mixed[3:]
            page.get_by_label("Layer with idle").check()
            expect(seek).to_have_attribute("max", "12")
            pose(3.75)
            # Changing gestures in flight must carry the background phase forward.
            before, after = page.evaluate("""() => {
                const values = () => ['body.breath','tail.curl','finger.right.little'].map(
                    id => document.getElementById('param-'+id).valueAsNumber)
                const play = document.getElementById('play'), motion = document.getElementById('motion')
                play.click()
                const before = values()
                motion.value = 'nod'
                motion.dispatchEvent(new Event('change'))
                if (play.textContent !== 'Pause') throw Error('Playback stopped')
                play.click()
                return [before,values()]
            }""")
            assert before == after
            check_tail_handoff(page)
            if output:
                # Capture WebGL during the same draw, before its back buffer clears.
                page.add_style_tag(content="#atlas {width:310px;height:520px}")
                motion.select_option("wrist")
                frames = []
                for i in range(144):
                    data = page.evaluate("""t => {
                        const seek = document.getElementById('seek')
                        seek.value = t
                        seek.dispatchEvent(new Event('input'))
                        const src = document.getElementById('atlas')
                        const dst = document.createElement('canvas')
                        dst.width = 310; dst.height = 520
                        const ctx = dst.getContext('2d')
                        ctx.fillStyle = '#545963'
                        ctx.fillRect(0,0,310,520)
                        ctx.drawImage(src,0,0,310,520)
                        return dst.toDataURL().split(',')[1]
                    }""", i/12)
                    with Image.open(BytesIO(base64.b64decode(data))) as frame:
                        frames.append(frame.convert("RGB"))
                frames[0].save(output, save_all=True, append_images=frames[1:],
                               duration=[80, 80, 90] * 48, loop=0)
            assert not errors, errors
            print("Layered idle, greeting, solo, tail transitions and reverse stepping passed.")
        finally:
            browser.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:5177")
    parser.add_argument("--browser")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    run(args.url, args.browser, args.output)

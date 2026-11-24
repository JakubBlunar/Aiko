"""Replay real app actions on the private Alexia wardrobe in a local browser."""
import argparse
import base64
from dataclasses import asdict
from io import BytesIO
from pathlib import Path
import subprocess
import sys

from PIL import Image, ImageChops
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from app.core.persona.avatar_profile import from_disk  # noqa: E402


def run(url, browser_path, output):
    profile = asdict(from_disk(ROOT / "data/personas/active/Alexia"))
    build = """const {buildSync}=require('./web/node_modules/esbuild')
const build=buildSync({entryPoints:['tools/rig-studio/test/channel-replay.ts'],bundle:true,
  globalName:'channelReplay',platform:'browser',write:false})
process.stdout.write(build.outputFiles[0].text)"""
    bundle = subprocess.check_output(["node", "-e", build], cwd=ROOT).decode()
    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=browser_path, args=["--enable-unsafe-swiftshader"])
        try:
            page = browser.new_page()
            page.goto(url.rstrip("/") + "/local/info")
            page.add_script_tag(content=bundle)
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
            page.evaluate("async ([p,c]) => { window.replay = await channelReplay.start(p,c) }",
                          [profile, bool(output)])
            frames, samples = [], {}
            for i in range(211):
                frame = page.evaluate("i => replay.frame(i)", i)
                samples[i] = frame["values"]
                if frame.get("png") and (output or i in (0, 112)):
                    with Image.open(BytesIO(base64.b64decode(frame["png"]))) as pic:
                        assert pic.getchannel("A").getbbox()
                        image = Image.new("RGB", pic.size, "#545963")
                        image.paste(pic, mask=pic.getchannel("A"))
                        frames.append(image)
            page.evaluate("() => replay.stop()")
            assert samples[15]["face.grin"] == 1 and samples[15]["head.pitch"] < -14
            assert samples[74]["mouth.open"] > .65 and samples[74]["face.grin"] == 0
            assert samples[109]["face.tears"] > .4
            assert samples[112]["eyes.style"] == 1 and samples[112]["face.tears"] == 0
            assert samples[160]["eyes.style"] == 0 and samples[160]["face.tears"] > .4
            assert samples[150]["body.pitch"] < samples[139]["body.pitch"] - 5
            assert samples[150]["head.pitch"] < -18
            assert samples[180]["face.tears"] == 0
            assert samples[200]["head.yaw"] < -10 and samples[210]["head.yaw"] > 10
            breaths = [s["body.breath"] for s in samples.values()]
            assert max(breaths) - min(breaths) > .2
            assert not errors, errors
            assert ImageChops.difference(frames[0], frames[len(frames)//2]).getbbox()
            if output:
                frames[0].save(output, save_all=True, append_images=frames[1:],
                               duration=([70, 60, 70] * len(frames))[:len(frames)], loop=0)
            print("211 rendered frames passed, including the tail's return to idle.")
        finally:
            browser.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:5177")
    parser.add_argument("--browser")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    run(args.url, args.browser, args.output)

"""Exercise authoring reloads in a disposable copy of the studio and a fictional rig."""
import argparse
import json
import os
import shutil
import socket
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory

from playwright.sync_api import expect, sync_playwright

STUDIO = Path(__file__).resolve().parents[1]
ROOT = STUDIO.parents[1]


def run(browser_path):
    scratch = ROOT / ".tmp-tests"
    scratch.mkdir(exist_ok=True)
    with TemporaryDirectory(dir=scratch, prefix="run-rig-reload-") as tmp:
        stage = Path(tmp)
        for name in ["src", "example"]:
            shutil.copytree(STUDIO / name, stage / name)
        for name in ["server.mjs", "build.mjs", "dev-watch.mjs", "index.html", "style.css",
                     "tsconfig.json", "package.json"]:
            shutil.copy2(STUDIO / name, stage / name)
        local = stage / "character"
        shutil.copytree(stage / "example", local)
        env = dict(os.environ, NODE_PATH=str(ROOT / "web/node_modules"))
        subprocess.run(["node", "build.mjs"], cwd=stage, env=env, check=True)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        server = subprocess.Popen(["node", "server.mjs", "--port", str(port), "--rig", str(local)],
                                  cwd=stage, env=env)
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(executable_path=browser_path)
                page = browser.new_page()
                page.goto(f"http://127.0.0.1:{port}")
                expect(page.locator("#title")).to_have_text("Pip")
                page.locator("#motion").select_option("wave")
                page.locator("#seek").fill("0.75")
                page.get_by_label("Head tilt", exact=True).fill("13")
                page.locator("#bones").check()
                rig = json.loads((local / "rig.json").read_text())
                rig["name"] = "Pip revised"
                (local / "rig.json").write_text(json.dumps(rig))
                expect(page.locator("#title")).to_have_text("Pip revised")
                expect(page.locator("#seek")).to_have_value("0.75")
                expect(page.get_by_label("Head tilt", exact=True)).to_have_value("13")
                neutral = page.locator("#custom").evaluate("(canvas) => canvas.toDataURL()")
                art = local / "head.svg"
                art.write_text(art.read_text().replace("#dc8855", "#154bc9"))
                changed = "old => document.querySelector('#custom').toDataURL() !== old"
                page.wait_for_function(changed, arg=neutral)
                expect(page.locator("#seek")).to_have_value("0.75")
                css = stage / "style.css"
                with css.open("a", encoding="utf-8") as file:
                    file.write("\n:root{--reload-proof:ready}\n")
                page.wait_for_function(
                    "getComputedStyle(document.documentElement).getPropertyValue('--reload-proof')"
                    ".trim() === 'ready'")
                source = stage / "src/studio.ts"
                original = source.read_text(encoding="utf-8")
                source.write_text(original + "\nconst broken: number = 'invalid'\n",
                                  encoding="utf-8")
                expect(page.locator("#status")).to_contain_text("build failed", timeout=15000)
                expect(page.locator("#motion")).to_have_value("wave")
                source.write_text(original + "\ndocument.title = 'Reload verified'\n",
                                  encoding="utf-8")
                expect(page).to_have_title("Reload verified", timeout=15000)
                expect(page.locator("#source")).to_have_value("local")
                expect(page.locator("#seek")).to_have_value("0.75")
                expect(page.get_by_label("Head tilt", exact=True)).to_have_value("13")
                expect(page.locator("#bones")).to_be_checked()
                page.locator("#folder").set_input_files(str(stage / "example"))
                expect(page.locator("#source")).to_have_value("folder")
                source.write_text(original + "\ndocument.title = 'Folder restored'\n",
                                  encoding="utf-8")
                expect(page).to_have_title("Folder restored", timeout=15000)
                expect(page.locator("#source")).to_have_value("folder")
                expect(page.locator("#title")).to_have_text("Pip")
                browser.close()
        finally:
            server.terminate()
            server.wait(timeout=10)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--browser")
    run(parser.parse_args().browser)
    print("Rig changes, compile recovery and inspection restoration passed")

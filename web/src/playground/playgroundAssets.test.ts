import { afterEach, describe, expect, it } from "vitest";
import { mkdtempSync, mkdirSync, realpathSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { resolvePlaygroundAsset } from "../../scripts/playgroundAssets";

const runs: string[] = [];
function fixture() {
  const run = mkdtempSync(join(tmpdir(), "avatar-assets-"));
  runs.push(run);
  const root = join(run, "model");
  mkdirSync(root);
  writeFileSync(join(root, "face space.json"), "{}");
  writeFileSync(join(run, "outside.json"), "{}");
  return { run, root };
}
afterEach(() => runs.splice(0).forEach((run) => rmSync(run, { recursive: true, force: true })));

describe("playground model assets", () => {
  it("serves model files with URL-encoded names and query strings", () => {
    const { root } = fixture();
    expect(resolvePlaygroundAsset(root, "/avatar/face%20space.json?v=1"))
      .toBe(realpathSync(join(root, "face space.json")));
  });
  it("refuses parent traversal, missing files, malformed paths and directories", () => {
    const { root } = fixture();
    for (const url of ["/avatar/../outside.json", "/avatar/%2e%2e/outside.json",
      "/avatar/missing.json", "/avatar/%GG", "/avatar/", "/elsewhere/face.json"]) {
      expect(resolvePlaygroundAsset(root, url)).toBeNull();
    }
  });
  it("refuses symlink escapes from the chosen model directory", ({ skip }) => {
    const { run, root } = fixture();
    try { symlinkSync(run, join(root, "escape"), "junction"); } catch { skip(); return; }
    expect(resolvePlaygroundAsset(root, "/avatar/escape/outside.json")).toBeNull();
  });
});

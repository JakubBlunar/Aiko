import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

const here = dirname(fileURLToPath(import.meta.url));
const source = readFileSync(resolve(here, "useAssistantSocket.ts"), "utf-8");

describe("sleep socket hydration", () => {
  it("hydrates sleep from hello before incremental events arrive", () => {
    expect(source).toMatch(/if \(evt\.sleep\)/);
    expect(source).toMatch(/store\.setSleep\(evt\.sleep\)/);
  });

  it("applies sleep lifecycle broadcasts", () => {
    expect(source).toMatch(/case "sleep_state_changed":/);
    expect(source).toMatch(/store\.setSleep\(evt\.snapshot\)/);
  });
});

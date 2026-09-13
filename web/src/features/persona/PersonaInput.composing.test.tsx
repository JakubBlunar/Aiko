import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));

describe("PersonaInput composing parity", () => {
  it("emits onComposing and never sends draft text", () => {
    const source = readFileSync(resolve(here, "PersonaInput.tsx"), "utf-8");
    expect(source).toMatch(/onComposing\?\(active: boolean\): void/);
    expect(source).toMatch(/emitComposing\(true\)/);
    expect(source).not.toMatch(/type:\s*"composing".*draft/);
  });

  it("persona window puts composing on the wire without draft", () => {
    const source = readFileSync(resolve(here, "PersonaWindow.tsx"), "utf-8");
    expect(source).toMatch(/type:\s*"composing"/);
    expect(source).toMatch(/surface:\s*"persona"/);
    expect(source).not.toMatch(/draft:/);
  });
});

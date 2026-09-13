import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

import { describe, expect, it } from "vitest";

const here = dirname(fileURLToPath(import.meta.url));
const toggleSource = readFileSync(resolve(here, "LiveModeToggle.tsx"), "utf-8");
const chatSource = readFileSync(resolve(here, "ChatView.tsx"), "utf-8");
const barSource = readFileSync(
  resolve(here, "../shell/MobileTopBar.tsx"),
  "utf-8",
);

describe("LiveModeToggle header wiring", () => {
  it("patches behavior_posture and never starts the microphone", () => {
    expect(toggleSource).toMatch(/enableLivePresence/);
    expect(toggleSource).toMatch(/disableLivePresence/);
    expect(toggleSource).not.toMatch(/voice_start/);
    expect(toggleSource).not.toMatch(/MicButton/);
    expect(toggleSource).toMatch(/always-listening/);
  });

  it("sits in the chat header, not the composer mic row", () => {
    expect(chatSource).toMatch(/<LiveModeToggle\s*\/>/);
    const composerIdx = chatSource.indexOf("<MicButton");
    const liveIdx = chatSource.indexOf("<LiveModeToggle");
    expect(liveIdx).toBeGreaterThan(0);
    expect(composerIdx).toBeGreaterThan(liveIdx);
  });

  it("uses the compact bar variant on the phone top bar", () => {
    expect(barSource).toMatch(/<LiveModeToggle\s+variant="bar"\s*\/>/);
  });
});

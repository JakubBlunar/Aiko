import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

/**
 * The avatar rail's status footer must not change height.
 *
 * The Live2D canvas sits in a ``flex-1`` box directly above it, so the
 * footer growing or losing a line re-lays-out that box, and
 * ``Live2DAvatar``'s ResizeObserver turns the change into an
 * ``app.resize()`` plus a ``fitModelToContainer()`` refit -- the model
 * rescales and the avatar visibly jumps mid-sentence. The trigger is
 * ordinary: the caption swaps a wrapping world string ("the mirror
 * corner · curled up · watching screens") for a one-line "speaking"
 * every time she starts talking.
 *
 * Vitest runs under Node without jsdom (see ``vitest.config.ts``), so
 * these are source checks rather than rendered-layout assertions.
 */
const here = dirname(fileURLToPath(import.meta.url));
const panelSource = readFileSync(resolve(here, "AvatarPanel.tsx"), "utf-8");
const avatarSource = readFileSync(resolve(here, "Live2DAvatar.tsx"), "utf-8");

describe("AvatarPanel status footer", () => {
  it("reserves a fixed two-line slot for the activity line", () => {
    // h-7 (28px) == two 14px line boxes. If either number moves, the
    // other has to move with it or the slot stops being exactly two
    // lines and the jump comes back.
    expect(panelSource).toMatch(/h-7/);
    expect(panelSource).toMatch(/leading-\[14px\]/);
  });

  it("clamps the activity line so it can never reach a third row", () => {
    // The panel is user-resizable and location names are user-supplied,
    // so a reserved height alone is not enough -- the text has to be
    // capped too.
    expect(panelSource).toMatch(/line-clamp-2/);
  });

  it("keeps the mood line to a single row", () => {
    // "Enthusiastic" fits at the default width but not at every width
    // the drag handle allows.
    expect(panelSource).toMatch(/truncate text-sm/);
    expect(panelSource).toMatch(/leading-5/);
  });

  it("stops the footer being squeezed by the flex column", () => {
    expect(panelSource).toMatch(/w-full max-w-xs shrink-0/);
  });

  it("still shows speaking / voice mode / world caption in that order", () => {
    expect(panelSource).toMatch(/ttsState === "speaking"/);
    expect(panelSource).toMatch(/voiceMode !== "off"/);
    expect(panelSource).toMatch(/worldCaption\(world\) \|\| "idle"/);
  });
});

describe("why the footer height matters", () => {
  it("the avatar box above it is still flex-1", () => {
    // This is the coupling the fixed height exists to defuse. If the
    // avatar ever stops being flex-sized, revisit the footer.
    expect(panelSource).toMatch(/flex w-full flex-1 items-center/);
  });

  it("Live2DAvatar still refits on container resize", () => {
    expect(avatarSource).toMatch(/new ResizeObserver\(handleResize\)/);
    expect(avatarSource).toMatch(/fitModelToContainer\(model, app/);
  });
});

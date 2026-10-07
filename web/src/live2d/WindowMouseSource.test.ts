import { afterEach, describe, expect, it, vi } from "vitest";
import { WindowMouseSource } from "./WindowMouseSource";

afterEach(() => vi.unstubAllGlobals());

function setup() {
  const windowTarget = new EventTarget();
  vi.stubGlobal("window", Object.assign(windowTarget, { innerWidth: 390, innerHeight: 844 }));
  vi.stubGlobal("document", { hasFocus: () => true });
  const container = {
    getBoundingClientRect: () => ({ left: 16, top: 72, width: 190, height: 230 }),
  } as unknown as HTMLElement;
  const source = new WindowMouseSource({ container, now: () => 1_000 });
  const unsubscribe = source.subscribe();
  const move = (pointerType: string, clientX: number, clientY: number) => {
    const event = Object.assign(new Event("pointermove"), { pointerType, clientX, clientY });
    windowTarget.dispatchEvent(event);
  };
  return { source, move, unsubscribe };
}

describe("WindowMouseSource", () => {
  it("does not treat touch scrolling as a cursor-follow target", () => {
    const { source, move, unsubscribe } = setup();
    try {
      move("touch", 300, 700);
      move("touch", 300, 200);
      expect(source.snapshot()).toMatchObject({ x: null, y: null, lastMoveAt: 0 });
    } finally {
      unsubscribe();
    }
  });

  it("still follows the mouse, without replacing its target with a touch swipe", () => {
    const { source, move, unsubscribe } = setup();
    try {
      move("mouse", 320, 240);
      expect(source.snapshot()).toMatchObject({ x: 320, y: 240, lastMoveAt: 1_000 });
      move("touch", 50, 700);
      expect(source.snapshot()).toMatchObject({ x: 320, y: 240, lastMoveAt: 1_000 });
    } finally {
      unsubscribe();
    }
  });
});
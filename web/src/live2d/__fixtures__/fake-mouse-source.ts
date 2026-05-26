/**
 * Stub ``MouseSource`` for engine + gaze tests.
 *
 * Production wires ``window`` mouse events through a real source;
 * tests just push canned snapshots in. ``subscribe`` returns a noop
 * since there's nothing to listen to.
 */
import type { MouseSource } from "../AvatarEngine";
import type { MouseSnapshot } from "../types";

export class FakeMouseSource implements MouseSource {
  current: MouseSnapshot = {
    x: null,
    y: null,
    lastMoveAt: 0,
    windowFocused: true,
    containerRect: { left: 0, top: 0, width: 800, height: 600 },
    viewportWidth: 1600,
    viewportHeight: 900,
  };
  subscribeCount = 0;
  unsubscribeCount = 0;

  snapshot(): MouseSnapshot {
    return this.current;
  }

  subscribe(): () => void {
    this.subscribeCount += 1;
    return () => {
      this.unsubscribeCount += 1;
    };
  }
}

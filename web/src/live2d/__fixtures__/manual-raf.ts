/**
 * Manual RAF scheduler for engine tests.
 *
 * The engine takes ``scheduleFrame`` / ``cancelFrame`` through
 * ``EngineDependencies`` so we can drive its RAF loops one frame at
 * a time. Production wires ``requestAnimationFrame`` /
 * ``cancelAnimationFrame``; this fake records callbacks in a queue
 * and lets tests advance them with ``flush(n)`` or ``flushUntil``.
 *
 * The RAFs the engine schedules are *self-rescheduling* — every
 * tick re-queues the next one. Tests typically pair this with
 * ``FakeClock`` so each ``flush()`` corresponds to a deterministic
 * dt.
 */
export class ManualRaf {
  /** Pending RAF callbacks, in registration order. */
  private _queue: Array<{ id: number; cb: FrameRequestCallback }> = [];
  private _nextId = 1;
  private _cancelled: Set<number> = new Set();

  schedule = (cb: FrameRequestCallback): number => {
    const id = this._nextId++;
    this._queue.push({ id, cb });
    return id;
  };

  cancel = (handle: number): void => {
    this._cancelled.add(handle);
    this._queue = this._queue.filter((entry) => entry.id !== handle);
  };

  /** Number of pending callbacks the engine has queued. */
  get pending(): number {
    return this._queue.length;
  }

  /** Run the next ``count`` RAF callbacks (default 1). The engine
   * will typically re-queue itself, so a single ``flush()`` advances
   * one frame for every running RAF loop. */
  flush(count: number = 1): void {
    for (let i = 0; i < count; i += 1) {
      const entry = this._queue.shift();
      if (!entry) {
        return;
      }
      if (this._cancelled.has(entry.id)) {
        continue;
      }
      entry.cb(0);
    }
  }

  /** Repeatedly flush() while the predicate returns true. Bounded
   * to ``maxFlushes`` to avoid runaway loops in broken tests. */
  flushUntil(predicate: () => boolean, maxFlushes: number = 1_000): void {
    for (let i = 0; i < maxFlushes; i += 1) {
      if (!predicate()) {
        return;
      }
      this.flush(1);
    }
    throw new Error(`ManualRaf.flushUntil exceeded ${maxFlushes} flushes`);
  }
}

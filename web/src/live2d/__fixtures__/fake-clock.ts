/**
 * Manual ``performance.now()`` driver for channel tests.
 *
 * Channels accept their time source through ``ChannelDeps.now`` (see
 * ``../types.ts``) so unit tests can drive the clock deterministically
 * without ``vi.useFakeTimers`` (which adds a lot of noise once you also
 * need a fake RAF + fake event source). ``advance`` returns the new
 * timestamp so tests can fluently chain calls:
 *
 *     const clock = new FakeClock();
 *     channel.tickTier3(clock.advance(16), 0.016);
 *     channel.tickTier3(clock.advance(16), 0.016);
 *
 * The starting value defaults to a small positive number so any
 * ``deadline = now + duration`` arithmetic stays unambiguously
 * positive.
 */
export class FakeClock {
  private _now: number;

  constructor(start: number = 1_000) {
    this._now = start;
  }

  now = (): number => this._now;

  /** Advance the clock by ``ms`` and return the new ``now()`` value. */
  advance(ms: number): number {
    this._now += ms;
    return this._now;
  }

  /** Set the clock to an absolute timestamp. Useful when emulating
   * a wall-clock to perf-clock conversion at engine ingest. */
  set(value: number): void {
    this._now = value;
  }
}

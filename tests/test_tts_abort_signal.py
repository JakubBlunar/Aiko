"""P25 -- an interrupted utterance must be distinguishable from a finished one.

Audio is scheduled on the *client* (browser ``AudioBufferSourceNode``s),
so ``TtsQueue.stop()`` only silences what hasn't been sent yet. Whatever
the client already buffered plays out regardless — which on barge-in
means Aiko talks over the user for the length of that buffer. The client
needs to drop its buffer, but only on an abort: flushing on a natural end
would clip the tail off every reply.

Both paths emit the same ``end`` event, so the only thing separating them
is the ``aborted`` flag these tests pin.
"""
from __future__ import annotations

import unittest

from app.core.voice.tts_queue import TtsQueue


class _Engine:
    """Plays synchronously unless told to hold the chunk open."""

    def __init__(self, *, hold: bool = False) -> None:
        self.hold = hold
        self.spoken: list[str] = []
        self.stops = 0
        self._on_done = None

    def speak_async(self, text, *, reaction=None, on_done=None,
                    on_amplitude=None, speed=None, gain_db=0.0) -> None:
        self.spoken.append(text)
        if self.hold:
            self._on_done = on_done
            return
        if on_done is not None:
            on_done()

    def finish(self) -> None:
        if self._on_done is not None:
            done, self._on_done = self._on_done, None
            done()

    def stop(self) -> None:
        self.stops += 1


class AbortFlagTests(unittest.TestCase):
    def setUp(self) -> None:
        self.events: list[tuple[str, dict]] = []
        self.engine = _Engine(hold=True)
        self.queue = TtsQueue(
            self.engine,
            state_listener=lambda event, payload: self.events.append(
                (event, dict(payload or {})),
            ),
        )

    def _ends(self) -> list[dict]:
        return [payload for event, payload in self.events if event == "end"]

    def test_stop_mid_utterance_marks_the_end_aborted(self) -> None:
        self.queue.enqueue("hello there")
        self.queue.stop()
        ends = self._ends()
        self.assertEqual(len(ends), 1)
        self.assertTrue(ends[0].get("aborted"))

    def test_a_natural_end_is_not_marked_aborted(self) -> None:
        # The client must let this one play out; flushing here would clip
        # the tail off every single reply.
        self.queue.enqueue("hello there")
        self.engine.finish()
        ends = self._ends()
        self.assertEqual(len(ends), 1)
        self.assertFalse(ends[0].get("aborted"))

    def test_stop_still_reaches_the_engine(self) -> None:
        self.queue.enqueue("hello there")
        self.queue.stop()
        self.assertEqual(self.engine.stops, 1)

    def test_stop_while_idle_emits_no_end_at_all(self) -> None:
        # Nothing was playing, so there is no buffer to flush and no
        # spurious "she stopped talking" for the client to react to.
        self.queue.stop()
        self.assertEqual(self._ends(), [])

    def test_stop_drops_the_queued_remainder(self) -> None:
        self.queue.enqueue("first")
        self.queue.enqueue("second")
        self.queue.stop()
        self.assertEqual(self.engine.spoken, ["first"])
        self.assertFalse(self.queue.is_active())

    def test_only_one_end_for_a_stopped_utterance(self) -> None:
        # A late ``on_done`` from the engine after stop() must not add a
        # second, un-aborted end that would look like a natural finish.
        self.queue.enqueue("hello there")
        self.queue.stop()
        self.engine.finish()
        self.assertEqual(len(self._ends()), 1)


if __name__ == "__main__":
    unittest.main()

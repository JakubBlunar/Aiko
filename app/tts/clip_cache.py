"""One-shot cache for pre-synthesised clips, shared by the TTS engines.

Why this exists
---------------
:class:`~app.core.voice.tts_queue.TtsQueue` prefetches the next sentence
while the current one plays, because both engines generate a whole clip
before emitting a byte -- so without a prefetch every sentence boundary
costs a full synthesis of dead air. The prefetch calls ``generate_audio``
and *discards the return value*, which only works if the engine
remembered the clip. pocket-tts did; Chatterbox did not, so its prefetch
did the work and threw it away, and playback then synthesised the same
sentence a second time behind the same pipe lock.

Hence a shared implementation rather than a second copy: whether the
prefetch pays off is a property of the queue's contract, not of the
model, and an engine that quietly lacks the cache looks fine in every
test and is 2 seconds slower per sentence in use.

In-flight coordination, not just storage
----------------------------------------
Storage alone is not enough, and this is the part a plain dict gets
wrong. The interesting case is playback asking for a clip the prefetch is
*still generating*. A cache that only knows "present or absent" reports a
miss, and the caller starts a second synthesis of the same text -- worse
than no prefetch, because the two contend for one engine.

So a key can be in three states: cached, in flight, or absent. A caller
arriving mid-flight waits for the owner and takes its result. Only a
caller finding the key absent generates, and it becomes the owner.

Consume-once
------------
:meth:`warm` returns the clip and leaves it cached; :meth:`discard` drops
it after playback. Deliberately not a read-through cache that keeps
entries: replaying identical text is rare, and both engines sample
stochastically, so a retained entry would make a repeated line play back
*identically* -- the one case where a cache hit is audible as a cache
hit.
"""

from __future__ import annotations

import logging
import threading
from collections import OrderedDict
from collections.abc import Callable
from typing import Any

log = logging.getLogger(__name__)

#: Entries kept before the oldest is evicted. The queue prefetches one
#: sentence ahead, so anything above a couple is slack for a burst of
#: short chunks; eight matches what pocket-tts used and a clip is a few
#: hundred kilobytes, so the ceiling is about memory rather than hits.
DEFAULT_LIMIT = 8


class ClipCache:
    """Keyed, bounded, one-shot store with in-flight de-duplication."""

    def __init__(self, limit: int = DEFAULT_LIMIT) -> None:
        self._limit = max(1, int(limit))
        self._lock = threading.Lock()
        self._entries: OrderedDict[str, Any] = OrderedDict()
        self._inflight: dict[str, threading.Event] = {}

    @staticmethod
    def key(text: str, temp: float | None = None) -> str:
        """Identify a clip by what actually changes the audio.

        Text and temperature, and nothing else. **Speed is deliberately
        absent**: it is applied at playback by the time-stretch, not at
        generation, so keying on it only manufactured misses. And it
        missed in the common case rather than a rare one -- the prefetch
        guesses speed from the cadence layer's hint while ``speak_async``
        pins it to 1.0 whenever the runtime speed gate is off, which is
        the default. So the two disagreed on nearly every sentence that
        carried any prosody, and the entry sat in the cache unread until
        it was evicted.
        """
        if temp is None:
            return text
        return f"{text}||t{float(temp):.3f}"

    def warm(
        self,
        key: str,
        factory: Callable[[], Any],
    ) -> Any:
        """The clip for ``key``, generated at most once across threads.

        Returns it *and leaves it cached*, so a prefetch and the playback
        that follows are one synthesis. Returns ``None`` if the factory
        did, without caching the failure -- a transient engine error
        should not pin a silent sentence.
        """
        while True:
            with self._lock:
                if key in self._entries:
                    self._entries.move_to_end(key)
                    return self._entries[key]
                waiter = self._inflight.get(key)
                if waiter is None:
                    owner = threading.Event()
                    self._inflight[key] = owner
                    break

            # Someone else is generating this. Wait rather than start a
            # second synthesis: they contend for one engine, and on a
            # sidecar they serialise on its pipe, so duplicating the work
            # costs the full generation time twice over.
            if not waiter.wait(timeout=_WAIT_TIMEOUT_S):
                log.debug("clip cache: waited out an in-flight entry, generating")
                return factory()
            # Loop rather than assume: the owner may have failed, in
            # which case the entry is absent and this caller takes over.

        try:
            value = factory()
        except BaseException:
            with self._lock:
                self._inflight.pop(key, None)
            owner.set()
            raise

        with self._lock:
            if value is not None:
                self._entries[key] = value
                while len(self._entries) > self._limit:
                    self._entries.popitem(last=False)
            self._inflight.pop(key, None)
        owner.set()
        return value

    def discard(self, key: str) -> None:
        """Drop an entry once it has been played."""
        with self._lock:
            self._entries.pop(key, None)

    def clear(self) -> None:
        """Drop everything. Called on stop and on a voice change, where
        keeping clips would replay the previous voice."""
        with self._lock:
            self._entries.clear()

    def pending(self) -> int:
        """Cached entry count. For tests and status reporting."""
        with self._lock:
            return len(self._entries)


#: Ceiling on waiting for another thread's synthesis. Long enough to
#: cover a slow cold generation, short enough that a wedged engine does
#: not hold a sentence forever -- the fallback is to generate it here.
_WAIT_TIMEOUT_S = 30.0


class SynthesisGate:
    """Yields the engine to the sentence being spoken, not the next one.

    A prefetch is only ever an optimisation, so it must never delay the
    audio the listener is waiting on. Left unordered it can: both engines
    synthesise under a single lock -- the sidecar's pipe is
    one-request-at-a-time by protocol -- so if the prefetch of sentence 2
    claims it first, sentence 1 waits out a whole generation before its
    first byte. On Chatterbox that is a two second silence at the top of
    the reply, trading a gap the listener notices mid-turn for one they
    notice immediately.

    So playback *claims* the gate synchronously, before its worker thread
    starts, and the prefetch waits for idle. Claiming from the caller's
    thread is the point: doing it inside the worker would leave the
    ordering to the scheduler, which is the race being closed.
    """

    def __init__(self) -> None:
        self._cv = threading.Condition()
        self._claims = 0

    def claim(self) -> None:
        """Announce a synthesis that something is waiting to hear."""
        with self._cv:
            self._claims += 1

    def release(self) -> None:
        """Report that synthesis done; a prefetch may proceed. Safe to
        call more often than :meth:`claim` -- the count floors at zero
        rather than going negative and wedging the gate shut."""
        with self._cv:
            self._claims = max(0, self._claims - 1)
            self._cv.notify_all()

    def wait_for_idle(self, timeout: float = _WAIT_TIMEOUT_S) -> bool:
        """Block while a claimed synthesis is outstanding.

        Returns False on timeout, where the caller should give up rather
        than push in: a prefetch is worth nothing if the engine is stuck.
        """
        with self._cv:
            return self._cv.wait_for(lambda: self._claims == 0, timeout=timeout)

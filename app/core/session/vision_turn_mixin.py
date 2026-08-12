"""H25 show-and-tell — look at a shared image *during* the turn.

Before this, attaching a photo produced a work ticket rather than a
reaction. The chat model saw only the user's caption plus a hint telling
it to spawn a ``describe_image`` workflow, so the honest reply it could
give was an acknowledgement. Three real shares from the transcript::

    "Can you look at this? I am curious what you will say."
        -> "Ooh, on it - I'll take a look and tell you what I find."
    "sent you image how you look right now"
        -> "Okay, I'm on it - I'll take a look at the picture you sent."
    "Look how nice it was in the top."   (a 4.3 MB mountain photo)
        -> "On it."

The description did eventually arrive on a later turn, but by then the
moment had passed, and nothing durable was written: the only trace left
in memory was *"Jacob sent Aiko an image of her avatar"* — the act of
sending, never what was in it.

This mixin closes both halves. Before prompt assembly, any image on the
turn goes to the **local** vision model and the description is stashed
for :meth:`_render_seen_image_block` to render, so Aiko's first reply is
about the picture. After the turn, :meth:`_drain_turn_vision` hands the
same description to the memory write-back so it is still there next week.

Two design choices worth keeping:

**It is not a tool.** The chat model never decides whether to look. An
image on the turn means we look, full stop. Making it a tool would
reintroduce the failure it fixes — a model that is busy, terse, or simply
unlucky skips the call and answers "nice!" about pixels it never saw.

**The pixels stay local.** ``main_chat`` may be a hosted route; the
worker is local Ollama. Only the *text* of what Aiko saw crosses that
line, so sharing a photo never uploads it anywhere.

**It queues, rather than racing.** The first build went straight to the
raw worker client, outside the LLM priority gate, on the theory that a
user-blocking call should not wait for anything. The opposite happened:
one GPU means an ungated call does not skip the queue, it just waits
*invisibly* inside Ollama with no idea how long it has been there — and
the first real share landed on top of a running background worker,
burned the whole budget queueing, and produced "I can tell you attached
an image but I can't see what's in it". So the pass now takes the gate
at :data:`~app.llm.llm_gate.USER_BLOCKING`, ahead of every worker, and
budgets the two waits separately: one for the slot, one for the call.

**Waiting really is the best available answer**, which is worth stating
because the tempting fix is to let the vision call run *alongside* the
worker instead of behind it. Measured on this machine, against one
model on one GPU: a 24-second generation run alone became 52 seconds
when a second request joined it, and the second request — a four-token
reply that takes 0.6s by itself — took 49. Ollama time-slices rather
than overlapping, so concurrency makes both callers slower and the
user-blocking one is not spared. Raising the gate's ``max_concurrency``
to get the photo answered sooner would do the exact opposite.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

from app.core.tasks.attachments import ATTACHMENTS_DIR, ATTACHMENTS_LABEL
from app.core.vision.image_describe import (
    VisionUnavailable,
    describe_image_bytes,
)
from app.llm.llm_gate import USER_BLOCKING as _USER_BLOCKING

log = logging.getLogger("app.session.vision_turn")


@dataclass(slots=True)
class SeenImage:
    """One image from this turn, as Aiko saw it."""

    filename: str
    rel_path: str
    description: str
    elapsed_seconds: float


class VisionTurnMixin:
    """Synchronous in-turn vision pass over a message's image attachments."""

    # ── the pass ─────────────────────────────────────────────────────

    def _maybe_describe_turn_images(
        self,
        *,
        on_tts_chunk: Callable[[str, str], None] | None = None,
    ) -> list[SeenImage]:
        """Describe this turn's images before the prompt is assembled.

        Returns what was seen (also stashed on ``_turn_vision_seen`` for
        the prompt provider). Every failure path returns an empty list:
        not seeing the picture must degrade to the old behaviour — she
        answers the caption and can still be asked to look properly —
        rather than breaking the turn.
        """
        self._turn_vision_seen = []
        attachments = list(getattr(self, "_active_turn_attachments", None) or [])
        if not attachments:
            return []

        cfg = getattr(self._settings.agent, "vision", None)
        if cfg is None or not bool(getattr(cfg, "enabled", False)):
            return []
        if not bool(getattr(cfg, "in_turn_enabled", True)):
            return []

        images = [
            a for a in attachments
            if isinstance(a, dict) and str(a.get("kind") or "") == "image"
        ]
        if not images:
            return []
        cap = max(1, int(getattr(cfg, "in_turn_max_images", 2)))
        images = images[:cap]

        client = getattr(self, "_worker_client_inner", None)
        if client is None:
            log.debug("in-turn vision: no worker client; skipping")
            return []
        model = (str(getattr(cfg, "model", "") or "").strip()
                 or str(getattr(self, "_effective_worker_model", "") or ""))
        prompt = str(getattr(cfg, "in_turn_prompt", "") or "").strip()
        max_edge = int(getattr(cfg, "max_edge", 1024))
        max_bytes = int(getattr(cfg, "max_bytes", 8 * 1024 * 1024))
        budget = float(getattr(cfg, "in_turn_timeout_seconds", 25))
        wait_budget = float(getattr(cfg, "in_turn_wait_seconds", 45))

        # The wait is real (seconds), so say something first. Without this
        # the voice path is indistinguishable from a hang, and the D3 web
        # search proved the filler is what makes the pause read as
        # deliberate.
        self._announce_looking(on_tts_chunk)

        started = time.perf_counter()
        seen: list[SeenImage] = []
        # Both budgets are shared across the images on the message, and
        # they are tracked apart. Charging queue time to the call budget
        # would mean that waiting out one background worker silently
        # cancels the *second* photo — the pass would report "budget
        # spent" having spent none of it on looking at anything.
        call_spent = 0.0
        queue_spent = 0.0
        for att in images:
            call_left = budget - call_spent
            if call_left <= 1.0:
                log.info("in-turn vision: call budget spent, skipping the rest")
                break
            raw = self._read_attachment_bytes(att, max_bytes=max_bytes)
            if raw is None:
                continue
            attempt = time.perf_counter()
            described, queued = self._describe_within(
                raw,
                client=client,
                model=model,
                prompt=prompt,
                max_edge=max_edge,
                timeout=call_left,
                gate=getattr(self, "_worker_llm_gate", None),
                wait_timeout=max(0.0, wait_budget - queue_spent),
            )
            queue_spent += queued
            call_spent += max(0.0, (time.perf_counter() - attempt) - queued)
            if described is None:
                continue
            seen.append(
                SeenImage(
                    filename=str(att.get("filename") or "image"),
                    rel_path=str(att.get("rel_path") or ""),
                    description=described.description,
                    elapsed_seconds=described.elapsed_seconds,
                )
            )

        self._turn_vision_seen = seen
        if seen:
            log.info(
                "in-turn vision: described %d image(s) in %.1fs total",
                len(seen),
                time.perf_counter() - started,
            )
        return seen

    @staticmethod
    def _describe_within(
        raw: bytes,
        *,
        client: Any,
        model: str,
        prompt: str,
        max_edge: int,
        timeout: float,
        gate: Any | None = None,
        wait_timeout: float = 180.0,
    ) -> tuple[Any | None, float]:
        """Describe an image, under two separate deadlines.

        Returns ``(seen_or_None, queued_seconds)``. The caller needs the
        queue time back to keep the two budgets apart across images.

        ``wait_timeout`` bounds how long we queue for the GPU;
        ``timeout`` bounds the call itself once we have it. Two clocks
        rather than one because they fail for different reasons and want
        different answers: queueing behind a 30-second background worker
        is normal and worth waiting out — he shared a photo knowing the
        local model is not instant — whereas a *call* that runs 30
        seconds means the model is being reloaded under VRAM pressure and
        the turn should stop waiting. One combined budget cannot tell
        those apart, and the first version spent its whole budget in the
        queue and then reported that it could not see the picture.

        The Ollama client's own timeout is fixed at construction and
        sized for background work, far too generous for a turn someone is
        watching.

        An abandoned call is left to finish on its daemon thread rather
        than cancelled — Ollama has no cancel, and letting it complete
        leaves the model warm, so the *next* share is fast. Its result is
        dropped, **but its gate slot is held until it genuinely ends**:
        releasing at the point we stopped waiting would let a worker
        start against a GPU that is still busy, which is the contention
        this gate exists to prevent.
        """
        result: dict[str, Any] = {}
        holding = threading.Event()
        priority = _USER_BLOCKING

        def _work() -> None:
            if gate is not None:
                waited = gate.acquire(priority, timeout=max(0.0, wait_timeout))
                if waited is None:
                    result["queued"] = max(0.0, wait_timeout)
                    result["error"] = (
                        f"gave up waiting {wait_timeout:.0f}s for the GPU"
                    )
                    holding.set()
                    return
                result["queued"] = waited
                if waited > 1.0:
                    log.info(
                        "in-turn vision: waited %.1fs for the GPU", waited,
                    )
            holding.set()
            try:
                result["seen"] = describe_image_bytes(
                    raw,
                    client=client,
                    model=model,
                    prompt=prompt,
                    max_edge=max_edge,
                )
            except VisionUnavailable as exc:
                result["error"] = str(exc)
            except Exception as exc:  # noqa: BLE001 - logged by the caller
                result["error"] = repr(exc)
            finally:
                if gate is not None:
                    gate.release(priority)

        worker = threading.Thread(
            target=_work, name="in-turn-vision", daemon=True,
        )
        worker.start()
        # Phase one: the queue. Phase two: the call. `holding` separates
        # them, so a slow queue cannot eat the call's budget.
        if not holding.wait(timeout=max(1.0, wait_timeout) + 1.0):
            log.warning("in-turn vision: gate wait did not settle; skipping")
            return None, max(0.0, wait_timeout)
        queued = float(result.get("queued") or 0.0)
        worker.join(timeout=max(1.0, timeout))
        if worker.is_alive():
            log.warning(
                "in-turn vision: the model took over %.0fs; falling back to "
                "the background describe_image path",
                timeout,
            )
            return None, queued
        if "error" in result:
            log.warning("in-turn vision: %s", result["error"])
            return None, queued
        return result.get("seen"), queued

    def _read_attachment_bytes(
        self, attachment: dict[str, Any], *, max_bytes: int,
    ) -> bytes | None:
        """Load one attachment off disk, or ``None`` if it can't be used."""
        rel = str(attachment.get("rel_path") or "")
        prefix = f"{ATTACHMENTS_LABEL}:"
        if not rel.startswith(prefix):
            # Only the managed attachments root is in scope here. Anything
            # else is a path we have not sandbox-checked.
            log.debug("in-turn vision: skipping non-attachment path %r", rel)
            return None
        name = rel[len(prefix):].strip()
        if not name or "/" in name or "\\" in name or name.startswith("."):
            log.debug("in-turn vision: refusing suspicious name %r", name)
            return None
        path = ATTACHMENTS_DIR / name
        try:
            if not path.is_file():
                log.debug("in-turn vision: attachment missing on disk: %s", name)
                return None
            if os.path.getsize(path) > max_bytes:
                log.info("in-turn vision: attachment over byte cap: %s", name)
                return None
            return path.read_bytes()
        except OSError as exc:
            log.info("in-turn vision: could not read %s (%s)", name, exc)
            return None

    def _announce_looking(
        self, on_tts_chunk: Callable[[str, str], None] | None,
    ) -> None:
        """Speak a short "let me look" line before the vision call."""
        if on_tts_chunk is None:
            return
        try:
            from app.core.voice.filler_injector import pick_looking_filler

            phrase, reaction = pick_looking_filler(
                getattr(self, "_last_reaction", None)
            )
            on_tts_chunk(phrase, reaction)
        except Exception:
            log.debug("looking-filler emit failed", exc_info=True)

    # ── post-turn drain ──────────────────────────────────────────────

    def _drain_turn_vision(self) -> list[SeenImage]:
        """Take this turn's descriptions for the post-turn memory write."""
        seen = list(getattr(self, "_turn_vision_seen", None) or [])
        self._turn_vision_seen = []
        return seen

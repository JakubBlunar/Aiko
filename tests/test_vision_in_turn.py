"""H25 show-and-tell: the in-turn vision pass, block, and memory write.

The vision call is faked (a subclass of :class:`OllamaClient` returning a
canned description) so nothing here touches a model or the network.

The behaviour under test is narrow but load-bearing: before H25, sharing
a photo produced "On it." and left nothing behind but the fact that a
file had arrived. These tests pin the three things that changed — she
looks during the turn, the prompt says she is looking rather than
planning to, and what she saw survives the turn.
"""
from __future__ import annotations

import io
import threading
import time
import unittest
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest import mock

from PIL import Image

from app.core.session.inner_life_part4 import InnerLifePart4Mixin
from app.core.session.vision_turn_mixin import SeenImage, VisionTurnMixin
from app.core.vision.image_describe import (
    VisionUnavailable,
    describe_image_bytes,
    prepare_image,
)
from app.core.vision.image_memory import (
    distil_image_memory,
    write_image_memory,
)
from app.llm.llm_gate import MAINTENANCE_WORKER, LlmPriorityGate
from app.llm.ollama_client import OllamaClient


def _png(width: int, height: int) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), (120, 80, 40)).save(buf, format="PNG")
    return buf.getvalue()


class _FakeVisionClient(OllamaClient):
    """OllamaClient whose ``chat`` is canned (no network)."""

    def __init__(
        self,
        response: str = "A tabby cat asleep on a blue couch.",
        raise_exc: Exception | None = None,
    ) -> None:
        self.calls: list[dict[str, Any]] = []
        self._response = response
        self._raise = raise_exc

    def chat(  # type: ignore[override]
        self,
        messages: list[dict[str, Any]],
        options: dict[str, object] | None = None,
        model: str | None = None,
        think: bool = False,
        *,
        surface: str = "chat",
    ) -> str:
        self.calls.append({"messages": messages, "model": model})
        if self._raise is not None:
            raise self._raise
        return self._response


@dataclass
class _VisionCfg:
    enabled: bool = True
    model: str = ""
    max_bytes: int = 8 * 1024 * 1024
    max_edge: int = 1024
    in_turn_enabled: bool = True
    in_turn_max_images: int = 2
    in_turn_timeout_seconds: int = 25
    in_turn_wait_seconds: int = 45
    in_turn_prompt: str = "Describe this image."


class _Agent:
    def __init__(self, vision: _VisionCfg) -> None:
        self.vision = vision


class _Settings:
    def __init__(self, vision: _VisionCfg) -> None:
        self.agent = _Agent(vision)


class _Session(VisionTurnMixin):
    """Minimal host exposing only what the mixin reads."""

    def __init__(self, vision: _VisionCfg, client: Any) -> None:
        self._settings = _Settings(vision)
        self._worker_client_inner = client
        self._effective_worker_model = "qwen3.6:27b"
        self._active_turn_attachments: list[dict[str, Any]] = []
        self._turn_vision_seen: list[SeenImage] = []
        self._last_reaction = "curious"
        self.spoken: list[tuple[str, str]] = []

    def tts(self, phrase: str, reaction: str) -> None:
        self.spoken.append((phrase, reaction))


# ── downscale ────────────────────────────────────────────────────────


class PrepareImageTests(unittest.TestCase):
    def test_large_image_is_shrunk_to_the_long_edge(self) -> None:
        out, width, height = prepare_image(_png(3024, 4032), max_edge=1024)
        self.assertEqual(max(width, height), 1024)
        self.assertEqual((width, height), (768, 1024))
        with Image.open(io.BytesIO(out)) as img:
            self.assertEqual(img.size, (768, 1024))

    def test_small_image_passes_through_untouched(self) -> None:
        raw = _png(320, 240)
        out, width, height = prepare_image(raw, max_edge=1024)
        self.assertIs(out, raw)
        self.assertEqual((width, height), (320, 240))

    def test_absurd_max_edge_is_floored_not_honoured(self) -> None:
        # A 1px "budget" would destroy the image; the floor protects it.
        _, width, height = prepare_image(_png(2000, 2000), max_edge=1)
        self.assertGreaterEqual(max(width, height), 256)

    def test_undecodable_bytes_are_sent_rather_than_dropped(self) -> None:
        # Ollama may understand a format Pillow refuses; refusing to look
        # at all would be the worse failure.
        raw = b"not an image at all"
        out, width, height = prepare_image(raw, max_edge=1024)
        self.assertIs(out, raw)
        self.assertEqual((width, height), (0, 0))


class DescribeImageBytesTests(unittest.TestCase):
    def test_returns_text_and_sends_a_downscaled_image(self) -> None:
        client = _FakeVisionClient()
        seen = describe_image_bytes(
            _png(2048, 2048),
            client=client,
            model="qwen3.6:27b",
            prompt="Describe this image.",
            max_edge=512,
        )
        self.assertEqual(seen.description, "A tabby cat asleep on a blue couch.")
        self.assertEqual((seen.width, seen.height), (512, 512))
        self.assertLess(seen.sent_bytes, seen.source_bytes)
        message = client.calls[0]["messages"][0]
        self.assertEqual(message["content"], "Describe this image.")
        self.assertEqual(len(message["images"]), 1)

    def test_non_ollama_client_is_refused_rather_than_sent_blind(self) -> None:
        # Sending no image to a client that can't take one would produce a
        # confident description of nothing.
        with self.assertRaises(VisionUnavailable) as ctx:
            describe_image_bytes(
                _png(64, 64), client=object(), model="m",
                prompt="p", max_edge=1024,
            )
        self.assertIn("can't accept images", str(ctx.exception))

    def test_empty_model_reply_is_an_error(self) -> None:
        with self.assertRaises(VisionUnavailable):
            describe_image_bytes(
                _png(64, 64), client=_FakeVisionClient(response="  "),
                model="m", prompt="p", max_edge=1024,
            )

    def test_call_failure_becomes_an_actionable_reason(self) -> None:
        client = _FakeVisionClient(raise_exc=RuntimeError("model not found"))
        with self.assertRaises(VisionUnavailable) as ctx:
            describe_image_bytes(
                _png(64, 64), client=client, model="ghost:9b",
                prompt="p", max_edge=1024,
            )
        self.assertIn("not installed locally", str(ctx.exception))


# ── the in-turn pass ─────────────────────────────────────────────────


class InTurnVisionPassTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.attachments_dir = Path(self._tmp.name)
        patcher = mock.patch(
            "app.core.session.vision_turn_mixin.ATTACHMENTS_DIR",
            self.attachments_dir,
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._tmp.cleanup)

    def _write(self, name: str, width: int = 640, height: int = 480) -> dict:
        (self.attachments_dir / name).write_bytes(_png(width, height))
        return {
            "id": name,
            "filename": f"my-{name}",
            "kind": "image",
            "rel_path": f"Attachments:{name}",
            "bytes": 1234,
        }

    def test_shared_image_is_described_during_the_turn(self) -> None:
        session = _Session(_VisionCfg(), _FakeVisionClient())
        session._active_turn_attachments = [self._write("a.png")]

        seen = session._maybe_describe_turn_images(on_tts_chunk=session.tts)

        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0].description, "A tabby cat asleep on a blue couch.")
        self.assertEqual(seen[0].rel_path, "Attachments:a.png")
        self.assertEqual(session._turn_vision_seen, seen)

    def test_a_filler_is_spoken_before_the_wait(self) -> None:
        session = _Session(_VisionCfg(), _FakeVisionClient())
        session._active_turn_attachments = [self._write("a.png")]

        session._maybe_describe_turn_images(on_tts_chunk=session.tts)

        self.assertEqual(len(session.spoken), 1)
        self.assertTrue(session.spoken[0][0].strip())

    def test_no_filler_when_there_is_nothing_to_look_at(self) -> None:
        session = _Session(_VisionCfg(), _FakeVisionClient())
        session._active_turn_attachments = [
            {"kind": "text", "rel_path": "Attachments:notes.txt"},
        ]

        self.assertEqual(
            session._maybe_describe_turn_images(on_tts_chunk=session.tts), [],
        )
        self.assertEqual(session.spoken, [])

    def test_disabled_in_turn_flag_skips_the_pass(self) -> None:
        session = _Session(
            _VisionCfg(in_turn_enabled=False), _FakeVisionClient(),
        )
        session._active_turn_attachments = [self._write("a.png")]
        self.assertEqual(session._maybe_describe_turn_images(), [])

    def test_vision_disabled_skips_the_pass(self) -> None:
        session = _Session(_VisionCfg(enabled=False), _FakeVisionClient())
        session._active_turn_attachments = [self._write("a.png")]
        self.assertEqual(session._maybe_describe_turn_images(), [])

    def test_image_cap_bounds_the_worst_case_wait(self) -> None:
        client = _FakeVisionClient()
        session = _Session(_VisionCfg(in_turn_max_images=2), client)
        session._active_turn_attachments = [
            self._write("a.png"), self._write("b.png"), self._write("c.png"),
        ]
        self.assertEqual(len(session._maybe_describe_turn_images()), 2)
        self.assertEqual(len(client.calls), 2)

    def test_a_failing_vision_call_degrades_instead_of_breaking_the_turn(
        self,
    ) -> None:
        client = _FakeVisionClient(raise_exc=RuntimeError("connection refused"))
        session = _Session(_VisionCfg(), client)
        session._active_turn_attachments = [self._write("a.png")]
        self.assertEqual(session._maybe_describe_turn_images(), [])

    def test_a_slow_model_is_abandoned_rather_than_hanging_the_turn(
        self,
    ) -> None:
        # A cold model reloading under VRAM pressure has been measured
        # taking minutes; the turn falls back to the background path.
        class _Slow(_FakeVisionClient):
            def chat(self, *a: Any, **k: Any) -> str:
                time.sleep(5)
                return "too late"

        session = _Session(_VisionCfg(in_turn_timeout_seconds=1), _Slow())
        session._active_turn_attachments = [self._write("a.png")]

        started = time.perf_counter()
        self.assertEqual(session._maybe_describe_turn_images(), [])
        self.assertLess(time.perf_counter() - started, 4.0)

    def test_budget_is_shared_across_images_not_per_image(self) -> None:
        class _Slow(_FakeVisionClient):
            def chat(self, *a: Any, **k: Any) -> str:
                time.sleep(2)
                return "late"

        session = _Session(
            _VisionCfg(in_turn_timeout_seconds=2, in_turn_max_images=3),
            _Slow(),
        )
        session._active_turn_attachments = [
            self._write("a.png"), self._write("b.png"), self._write("c.png"),
        ]
        started = time.perf_counter()
        session._maybe_describe_turn_images()
        # Three images at a 2s each would be 6s; the shared budget caps it.
        self.assertLess(time.perf_counter() - started, 5.0)

    def test_she_waits_for_a_worker_that_holds_the_gpu(self) -> None:
        """The bug from the first real share.

        A background worker held the one GPU, the vision call queued
        invisibly inside Ollama, the budget ran out before the model ever
        started, and she said she could tell an image was attached but
        not what was in it. Queueing is now explicit and has its own
        clock, so waiting out a worker is a wait rather than a failure.
        """
        gate = LlmPriorityGate(max_concurrency=1)
        gate.acquire(MAINTENANCE_WORKER)
        threading.Timer(0.4, gate.release, args=(MAINTENANCE_WORKER,)).start()

        session = _Session(
            _VisionCfg(in_turn_timeout_seconds=5, in_turn_wait_seconds=10),
            _FakeVisionClient(),
        )
        session._worker_llm_gate = gate
        session._active_turn_attachments = [self._write("a.png")]

        seen = session._maybe_describe_turn_images()

        self.assertEqual(len(seen), 1)
        self.assertEqual(gate.stats()["inflight"], 0, "slot must be given back")

    def test_the_queue_wait_does_not_eat_the_call_budget(self) -> None:
        # The whole point of two clocks: a 3s queue plus a 1s call must
        # succeed under a 2s *call* budget, where one combined budget
        # would have expired before the model was even reached.
        gate = LlmPriorityGate(max_concurrency=1)
        gate.acquire(MAINTENANCE_WORKER)
        threading.Timer(3.0, gate.release, args=(MAINTENANCE_WORKER,)).start()

        class _Slowish(_FakeVisionClient):
            def chat(self, *a: Any, **k: Any) -> str:
                time.sleep(1.0)
                return "a quiet room, late afternoon light"

        session = _Session(
            _VisionCfg(in_turn_timeout_seconds=2, in_turn_wait_seconds=10),
            _Slowish(),
        )
        session._worker_llm_gate = gate
        session._active_turn_attachments = [self._write("a.png")]

        seen = session._maybe_describe_turn_images()

        self.assertEqual(len(seen), 1)

    def test_queueing_for_the_first_image_does_not_cancel_the_second(
        self,
    ) -> None:
        """Waiting is not looking, and must not be billed as if it were.

        The call budget is shared across a message's images so the turn
        cannot hang, but it was measured from before the *queue* wait.
        One slow worker in front of the first photo therefore consumed
        the whole budget without a single call having been made, and the
        second photo was dropped as "budget spent" — an invisible loss
        that only appears when someone shares two pictures at once.
        """
        gate = LlmPriorityGate(max_concurrency=1)
        gate.acquire(MAINTENANCE_WORKER)
        threading.Timer(2.0, gate.release, args=(MAINTENANCE_WORKER,)).start()

        client = _FakeVisionClient()
        session = _Session(
            _VisionCfg(
                in_turn_timeout_seconds=3,
                in_turn_wait_seconds=10,
                in_turn_max_images=2,
            ),
            client,
        )
        session._worker_llm_gate = gate
        session._active_turn_attachments = [
            self._write("a.png"), self._write("b.png"),
        ]

        seen = session._maybe_describe_turn_images()

        self.assertEqual(len(seen), 2, "the second image was dropped")
        self.assertEqual(len(client.calls), 2)

    def test_the_shared_queue_budget_is_not_per_image(self) -> None:
        # The other half of the same accounting: queue time *is* shared,
        # so two images behind a wedged worker cost one wait, not two.
        gate = LlmPriorityGate(max_concurrency=1)
        gate.acquire(MAINTENANCE_WORKER)  # never released

        session = _Session(
            _VisionCfg(
                in_turn_timeout_seconds=5,
                in_turn_wait_seconds=1,
                in_turn_max_images=2,
            ),
            _FakeVisionClient(),
        )
        session._worker_llm_gate = gate
        session._active_turn_attachments = [
            self._write("a.png"), self._write("b.png"),
        ]

        started = time.perf_counter()
        self.assertEqual(session._maybe_describe_turn_images(), [])
        self.assertLess(time.perf_counter() - started, 3.0)

    def test_giving_up_on_the_queue_is_survivable(self) -> None:
        gate = LlmPriorityGate(max_concurrency=1)
        gate.acquire(MAINTENANCE_WORKER)  # never released

        session = _Session(
            _VisionCfg(in_turn_timeout_seconds=5, in_turn_wait_seconds=1),
            _FakeVisionClient(),
        )
        session._worker_llm_gate = gate
        session._active_turn_attachments = [self._write("a.png")]

        started = time.perf_counter()
        self.assertEqual(session._maybe_describe_turn_images(), [])
        self.assertLess(time.perf_counter() - started, 4.0)
        self.assertEqual(gate.stats()["queued"], 0, "must not linger in the heap")

    def test_an_abandoned_call_keeps_its_slot_until_it_really_ends(
        self,
    ) -> None:
        """Releasing when we stop *waiting* would release a busy GPU.

        The abandoned call is still running — Ollama has no cancel — so
        handing the slot on at that moment invites a worker to start
        against a card that is still occupied, which is exactly the
        contention the gate exists to prevent.
        """
        gate = LlmPriorityGate(max_concurrency=1)

        class _Slow(_FakeVisionClient):
            def chat(self, *a: Any, **k: Any) -> str:
                time.sleep(3.0)
                return "too late"

        session = _Session(
            _VisionCfg(in_turn_timeout_seconds=2, in_turn_wait_seconds=5),
            _Slow(),
        )
        session._worker_llm_gate = gate
        session._active_turn_attachments = [self._write("a.png")]

        self.assertEqual(session._maybe_describe_turn_images(), [])
        # Given up on, but still holding — the model is still running.
        self.assertEqual(gate.stats()["inflight"], 1)
        # ...and released once it genuinely finishes.
        deadline = time.perf_counter() + 4.0
        while time.perf_counter() < deadline:
            if gate.stats()["inflight"] == 0:
                break
            time.sleep(0.05)
        self.assertEqual(gate.stats()["inflight"], 0)

    def test_no_gate_configured_still_works(self) -> None:
        session = _Session(_VisionCfg(), _FakeVisionClient())
        session._worker_llm_gate = None
        session._active_turn_attachments = [self._write("a.png")]
        self.assertEqual(len(session._maybe_describe_turn_images()), 1)

    def test_paths_outside_the_attachments_root_are_refused(self) -> None:
        client = _FakeVisionClient()
        session = _Session(_VisionCfg(), client)
        session._active_turn_attachments = [{
            "kind": "image",
            "filename": "escape.png",
            "rel_path": "Attachments:../../secrets.png",
        }]
        self.assertEqual(session._maybe_describe_turn_images(), [])
        self.assertEqual(client.calls, [])

    def test_non_attachment_roots_are_refused(self) -> None:
        client = _FakeVisionClient()
        session = _Session(_VisionCfg(), client)
        session._active_turn_attachments = [
            {"kind": "image", "rel_path": "Documents:private.png"},
        ]
        self.assertEqual(session._maybe_describe_turn_images(), [])
        self.assertEqual(client.calls, [])

    def test_oversized_attachment_is_skipped(self) -> None:
        client = _FakeVisionClient()
        session = _Session(_VisionCfg(max_bytes=10), client)
        session._active_turn_attachments = [self._write("a.png")]
        self.assertEqual(session._maybe_describe_turn_images(), [])

    def test_previous_turn_results_never_leak_forward(self) -> None:
        session = _Session(_VisionCfg(), _FakeVisionClient())
        session._turn_vision_seen = [
            SeenImage("old.png", "Attachments:old.png", "an old photo", 1.0),
        ]
        session._active_turn_attachments = []
        self.assertEqual(session._maybe_describe_turn_images(), [])
        self.assertEqual(session._turn_vision_seen, [])

    def test_drain_hands_over_once(self) -> None:
        session = _Session(_VisionCfg(), _FakeVisionClient())
        session._active_turn_attachments = [self._write("a.png")]
        session._maybe_describe_turn_images()
        self.assertEqual(len(session._drain_turn_vision()), 1)
        self.assertEqual(session._drain_turn_vision(), [])


# ── memory write-back ────────────────────────────────────────────────


_UNSET = object()


class _Embedder:
    def embed(self, text: str) -> list[float]:
        return [float(len(text))]


class _Memory:
    def __init__(self, mem_id: int = 7) -> None:
        self.id = mem_id

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id}


class _MemoryStore:
    """Records ``add`` calls. ``result=None`` mimics the dedupe path."""

    def __init__(self, result: Any = _UNSET) -> None:
        self.calls: list[dict[str, Any]] = []
        self._result = _Memory() if result is _UNSET else result

    def add(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return self._result


class ImageMemoryTests(unittest.TestCase):
    def test_description_is_distilled_into_one_line(self) -> None:
        def chat(messages: list[dict[str, Any]], **_: Any) -> str:
            return "Jacob showed off the view from the summit at sunset."

        line = distil_image_memory(
            description="This image shows a mountain vista...",
            user_text="Look how nice it was in the top.",
            user_name="Jacob",
            chat=chat,
        )
        self.assertEqual(
            line, "Jacob showed off the view from the summit at sunset.",
        )

    def test_skip_verdict_writes_nothing(self) -> None:
        line = distil_image_memory(
            description="A blurry grey rectangle.",
            user_text="", user_name="Jacob",
            chat=lambda *_a, **_k: "SKIP",
        )
        self.assertEqual(line, "")

    def test_quoted_reply_is_unwrapped(self) -> None:
        line = distil_image_memory(
            description="d", user_text="", user_name="Jacob",
            chat=lambda *_a, **_k: '"Jacob showed his new desk."',
        )
        self.assertEqual(line, "Jacob showed his new desk.")

    def test_overlong_reply_is_truncated_on_a_word_boundary(self) -> None:
        line = distil_image_memory(
            description="d", user_text="", user_name="Jacob",
            chat=lambda *_a, **_k: "word " * 200,
        )
        self.assertLessEqual(len(line), 321)
        self.assertTrue(line.endswith("…"))

    def test_distil_failure_writes_nothing(self) -> None:
        def boom(*_a: Any, **_k: Any) -> str:
            raise RuntimeError("worker down")

        self.assertEqual(
            distil_image_memory(
                description="d", user_text="", user_name="Jacob", chat=boom,
            ),
            "",
        )

    def test_memory_carries_attachment_provenance(self) -> None:
        from app.core.infra import timephrase

        store = _MemoryStore()
        mem_id = write_image_memory(
            line="Jacob showed the view from the summit at sunset.",
            filename="20260627.jpg",
            rel_path="Attachments:79e981.jpg",
            source_message_id=1434,
            now=timephrase.utcnow(),
            embedder=_Embedder(),
            memory_store=store,
        )
        self.assertEqual(mem_id, 7)
        call = store.calls[0]
        self.assertEqual(call["kind"], "event")
        self.assertEqual(call["tier"], "long_term")
        self.assertEqual(call["source_message_id"], 1434)
        # The provenance is what lets a later caller get from "she
        # remembers the mountain" back to the file on disk.
        self.assertEqual(
            call["metadata"]["source_attachment"], "Attachments:79e981.jpg",
        )
        self.assertEqual(call["metadata"]["source"], "shared_image")

    def test_dedupe_is_reported_as_no_write(self) -> None:
        from app.core.infra import timephrase

        store = _MemoryStore(result=None)
        self.assertIsNone(
            write_image_memory(
                line="same photo again", filename="a.png",
                rel_path="Attachments:a.png", source_message_id=None,
                now=timephrase.utcnow(), embedder=_Embedder(),
                memory_store=store,
            )
        )

    def test_empty_line_never_reaches_the_store(self) -> None:
        from app.core.infra import timephrase

        store = _MemoryStore()
        self.assertIsNone(
            write_image_memory(
                line="   ", filename="a.png", rel_path="Attachments:a.png",
                source_message_id=None, now=timephrase.utcnow(),
                embedder=_Embedder(), memory_store=store,
            )
        )
        self.assertEqual(store.calls, [])


# ── the prompt block ─────────────────────────────────────────────────


class _BlockHost(InnerLifePart4Mixin):
    """Just enough of the provider mixin's host to render the blocks."""

    user_display_name = "Jacob"

    def __init__(
        self,
        attachments: list[dict[str, Any]],
        seen: list[SeenImage],
    ) -> None:
        self._active_turn_attachments = attachments
        self._turn_vision_seen = seen


def _image_ref(name: str = "a.png") -> dict[str, Any]:
    return {
        "id": name, "filename": f"my-{name}", "kind": "image",
        "rel_path": f"Attachments:{name}", "bytes": 10,
    }


class SeenImageBlockTests(unittest.TestCase):
    def test_block_is_silent_when_nothing_was_seen(self) -> None:
        host = _BlockHost([_image_ref()], [])
        self.assertEqual(host._render_seen_image_block(), "")

    def test_block_carries_the_description_and_frames_it_as_looking(
        self,
    ) -> None:
        seen = [SeenImage("sunset.jpg", "Attachments:a.png", "Layered ridges at dusk.", 3.1)]
        block = _BlockHost([_image_ref()], seen)._render_seen_image_block()

        self.assertIn("Layered ridges at dusk.", block)
        self.assertIn("sunset.jpg", block)
        # The framing is the whole point: she is looking now, not later.
        self.assertIn("your own eyes", block)
        self.assertIn("looking at it", block)

    def test_a_verbose_description_is_capped_in_the_prompt(self) -> None:
        seen = [SeenImage("a.jpg", "Attachments:a.png", "word " * 800, 1.0)]
        block = _BlockHost([], seen)._render_seen_image_block()
        self.assertLess(len(block), 1800)
        self.assertIn("…", block)

    def test_multiple_images_read_as_plural(self) -> None:
        seen = [
            SeenImage("a.jpg", "Attachments:a.png", "A cat.", 1.0),
            SeenImage("b.jpg", "Attachments:b.png", "A dog.", 1.0),
        ]
        block = _BlockHost([], seen)._render_seen_image_block()
        self.assertIn("shared images", block)
        self.assertIn("A cat.", block)
        self.assertIn("A dog.", block)

    def test_seen_images_are_not_also_routed_to_a_workflow(self) -> None:
        # Both reacting to the picture and filing a job to look at it
        # reads as her forgetting she had already seen it.
        seen = [SeenImage("a.png", "Attachments:a.png", "A cat.", 1.0)]
        block = _BlockHost([_image_ref()], seen)._render_attachments_block()

        self.assertIn("already looked at this one", block)
        self.assertNotIn("describe_image", block)
        self.assertNotIn("start_workflow", block)

    def test_unseen_image_still_routes_to_the_workflow(self) -> None:
        block = _BlockHost([_image_ref()], [])._render_attachments_block()
        self.assertIn("describe_image", block)
        self.assertIn("start_workflow", block)

    def test_text_attachment_still_routes_while_the_image_is_seen(
        self,
    ) -> None:
        attachments = [
            _image_ref(),
            {"kind": "text", "rel_path": "Attachments:notes.txt",
             "filename": "notes.txt"},
        ]
        seen = [SeenImage("a.png", "Attachments:a.png", "A cat.", 1.0)]
        block = _BlockHost(attachments, seen)._render_attachments_block()

        self.assertIn("read_file", block)
        self.assertNotIn("describe_image", block)


if __name__ == "__main__":
    unittest.main()

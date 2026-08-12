"""Local-vision image description — the one place an image meets a model.

Both callers of the local vision model go through here:

* the ``describe_image`` workflow skill
  (:mod:`app.core.tasks.handlers.vision_describe`), which is the slow,
  asynchronous "read this file for me" path, and
* the **in-turn** show-and-tell pass (H25), which runs while the user is
  waiting so Aiko can react to a shared photo in the same breath rather
  than filing a ticket about it.

Keeping the encode + call in one module matters for more than tidiness.
The image is the single most privacy-sensitive thing the user hands over,
and the rule that protects it — *pixels only ever reach the local Ollama
worker, never the chat route* — is only auditable if there is exactly one
function that touches them. :func:`describe_image_bytes` is that function.
The chat model, which may well be a hosted one, receives Aiko's words
about the picture and never the picture.

## Why the downscale exists

Vision latency is almost entirely prefill, and prefill is driven by image
tokens, which are driven by resolution — not by file size. Measured on a
local ``qwen3.6:27b`` with a 3024x4032 phone photo:

    as shot      4063 prompt tokens    6.7 s
    long edge 1600  1948 tokens        4.3 s
    long edge 1280  1248 tokens        3.2 s
    long edge 1024   816 tokens        3.3 s

Description quality is flat across that range — the 1024px run still
named subject, setting, colours, visible text and mood — so the shrink is
close to free, and it is the difference between a pause the user reads as
"she's looking" and one they read as "it hung". Below ~1024 nothing more
is won, because generation rather than prefill becomes the floor.
"""
from __future__ import annotations

import base64
import io
import logging
import time
from dataclasses import dataclass
from typing import Any

log = logging.getLogger("app.vision.describe")


# Below this, shrinking buys nothing: generation time dominates prefill.
MIN_USEFUL_EDGE = 256


@dataclass(slots=True)
class DescribedImage:
    """One image, as the local model saw it."""

    description: str
    model: str
    source_bytes: int
    sent_bytes: int
    width: int
    height: int
    elapsed_seconds: float


class VisionUnavailable(RuntimeError):
    """The local vision path can't run, with a user-actionable reason.

    Raised rather than returned because every caller has to stop: there
    is no partial answer to "what is in this picture".
    """


def prepare_image(raw: bytes, *, max_edge: int) -> tuple[bytes, int, int]:
    """Downscale ``raw`` so its long edge is at most ``max_edge``.

    Returns ``(encoded, width, height)`` describing what will actually be
    sent. An image already within budget is passed through untouched —
    re-encoding a small JPEG would cost quality for no latency win.

    A decode failure is not fatal here: Ollama may still understand a
    format Pillow refuses, and refusing to look at all is a worse outcome
    than sending the original bytes, so the original is returned with
    unknown dimensions.
    """
    edge = max(MIN_USEFUL_EDGE, int(max_edge))
    try:
        from PIL import Image
    except Exception:  # pragma: no cover - Pillow is a hard dependency
        log.warning("vision: Pillow unavailable, sending image unscaled")
        return raw, 0, 0

    try:
        with Image.open(io.BytesIO(raw)) as img:
            img.load()
            width, height = img.size
            if max(width, height) <= edge:
                return raw, width, height
            scale = edge / float(max(width, height))
            new_size = (
                max(1, int(width * scale)),
                max(1, int(height * scale)),
            )
            # Animated / paletted / alpha sources all have to land on RGB
            # before JPEG will take them.
            shrunk = img.convert("RGB").resize(new_size, Image.LANCZOS)
            buf = io.BytesIO()
            shrunk.save(buf, format="JPEG", quality=85, optimize=True)
    except Exception as exc:  # noqa: BLE001 - see docstring
        log.info("vision: could not downscale image (%r), sending as-is", exc)
        return raw, 0, 0

    out = buf.getvalue()
    # A pathological source can encode larger than it started; keep the
    # smaller of the two rather than paying for the "optimisation".
    if len(out) >= len(raw):
        return raw, width, height
    return out, new_size[0], new_size[1]


def describe_image_bytes(
    raw: bytes,
    *,
    client: Any,
    model: str,
    prompt: str,
    max_edge: int,
) -> DescribedImage:
    """Describe ``raw`` with the local multimodal worker model.

    ``client`` must be an :class:`~app.llm.ollama_client.OllamaClient`.
    That is a deliberate hard requirement rather than a graceful
    degradation: a remote OpenAI-compatible worker needs a different
    image envelope, and quietly sending *no* image would produce a
    confident description of nothing, which is far worse than an error.
    """
    if client is None:
        raise VisionUnavailable("vision is unavailable (no worker model)")
    try:
        from app.llm.ollama_client import OllamaClient
    except Exception:  # pragma: no cover - import guard
        OllamaClient = None  # type: ignore[assignment]
    if OllamaClient is not None and not isinstance(client, OllamaClient):
        raise VisionUnavailable(
            "the current worker client can't accept images; set a local "
            "multimodal Ollama worker model (e.g. qwen3.6:27b) and keep "
            "workers on local Ollama"
        )
    if not raw:
        raise VisionUnavailable("image file is empty")

    sent, width, height = prepare_image(raw, max_edge=max_edge)
    b64 = base64.b64encode(sent).decode("ascii")
    started = time.perf_counter()
    try:
        description = client.chat(
            [{"role": "user", "content": prompt, "images": [b64]}],
            model=model or None,
            think=False,
            surface="vision_describe",
        )
    except Exception as exc:  # noqa: BLE001 - mapped to a friendly reason
        raise VisionUnavailable(friendly_call_error(exc, model)) from exc
    elapsed = time.perf_counter() - started

    description = (description or "").strip()
    if not description:
        raise VisionUnavailable(
            "vision model returned an empty description (is the worker "
            "model multimodal?)"
        )
    log.info(
        "vision: described image: model=%s src=%dB sent=%dB %dx%d "
        "elapsed=%.1fs chars=%d",
        model or "(worker default)",
        len(raw),
        len(sent),
        width,
        height,
        elapsed,
        len(description),
    )
    return DescribedImage(
        description=description,
        model=model or "(worker default)",
        source_bytes=len(raw),
        sent_bytes=len(sent),
        width=width,
        height=height,
        elapsed_seconds=elapsed,
    )


def friendly_call_error(exc: Exception, model: str) -> str:
    """Map a raw chat exception to a short, user-actionable reason."""
    text = str(exc).lower()
    named = model or "the worker model"
    if "not found" in text or "404" in text:
        return f"{named} is not installed locally (ollama pull it first)"
    if "timed out" in text or "timeout" in text:
        return f"{named} timed out looking at the image"
    if "connection" in text or "refused" in text:
        return "could not reach the local Ollama server"
    return f"vision call failed: {exc}"

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class VisionSettings:
    """Resource config for the local-vision ``describe_image`` task.

    The vision task does NOT introduce a second model: it reuses the
    already-loaded worker Ollama client + worker model, so the only
    requirement is that the worker model is multimodal (e.g.
    ``qwen3.5:27b`` / ``qwen3.6:27b``). That's why there's no
    ``base_url`` / ``keep_alive`` / ``num_ctx`` here — those are
    inherited from the worker client so there is genuinely one model
    config to reason about.

    * ``enabled`` — master switch. Off = the ``describe_image`` workflow
      skill is not offered and the handler is not registered.
    * ``model`` — OPTIONAL override. Empty (the default + recommended)
      reuses the effective worker model. A non-empty value points the
      vision call at a different local model, accepting a load/reload.
    * ``max_bytes`` — hard cap on the image file size that will be
      base64-encoded and sent to Ollama.
    * ``max_edge`` — longest edge, in pixels, the image is downscaled to
      before the call. Vision latency is prefill-bound and prefill is
      driven by resolution, so this is the main speed knob; see
      :mod:`app.core.vision.image_describe` for the measurements behind
      the 1024 default.
    * ``timeout_seconds`` — per-call ceiling (vision inference + a
      possible cold model load can be slow).
    * ``allowed_extensions`` — case-insensitive image extension
      allow-list (empty = allow everything).
    * ``default_prompt`` — instruction sent alongside the image when the
      caller doesn't supply a question.

    H25 show-and-tell (the in-turn path):

    * ``in_turn_enabled`` — when the user attaches an image to a chat
      message, look at it *during* that turn instead of filing a
      background workflow, so Aiko reacts in the same breath. Costs a
      few seconds of turn latency, covered by a spoken filler.
    * ``in_turn_max_images`` — how many images from one message get a
      vision pass. Each is a separate call, so this bounds the worst-case
      wait rather than the message.
    * ``in_turn_timeout_seconds`` — total budget for the whole in-turn
      pass. Much tighter than ``timeout_seconds`` because someone is
      sitting there watching: a warm call is 3–5s, but an evicted model
      reloading under VRAM contention has been measured at minutes, and
      a turn must never hang on that. On expiry the pass is abandoned
      and the attachment falls back to the background ``describe_image``
      workflow, which is exactly the pre-H25 behaviour.
    * ``in_turn_prompt`` — the instruction used for the in-turn pass.
      Deliberately different from ``default_prompt``: this description is
      read by Aiko as *her own looking*, so it asks for the things a
      person reacts to rather than a caption.
    """

    enabled: bool = False
    model: str = ""
    max_bytes: int = 8 * 1024 * 1024
    max_edge: int = 1024
    timeout_seconds: int = 180
    allowed_extensions: tuple[str, ...] = (
        ".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp",
    )
    default_prompt: str = (
        "Look at this image and describe what you see in a few natural "
        "sentences. Mention the main subject, setting, notable details, "
        "any visible text, and the overall mood."
    )
    in_turn_enabled: bool = True
    in_turn_max_images: int = 2
    in_turn_timeout_seconds: int = 25
    in_turn_prompt: str = (
        "Describe this image in a few sentences, the way you would to "
        "someone who cannot see it. Cover the main subject, the setting, "
        "any people or animals and what they are doing, colours and "
        "light, anything written in it, and the overall mood. Mention "
        "small specific details that stand out. Do not guess at who "
        "someone is, and say plainly if the image is blurry or unclear."
    )



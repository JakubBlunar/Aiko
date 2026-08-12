"""H25 — remember what was in a picture, not just that one arrived.

Before this, sharing a photo left almost nothing behind. The vision
description lived in ``tasks.result`` until the 30-day cleanup swept it,
and the only durable trace in the memory table was the *act*::

    [event] Jacob sent Aiko an image of her avatar in pajamas to see how
            she looks.

Which is the shape of a delivery receipt. Ask her a week later what the
mountain looked like and there is nothing to retrieve, because nothing
about the mountain was ever written down.

This module closes that. After the turn, the description Aiko already
paid for is distilled into one compact ``event`` memory that says what
was actually in the frame, tagged with the attachment it came from so
the original can still be found on disk.

Design notes:

* **Post-turn, on the speaking window.** Same reasoning as the D3 search
  distill it is modelled on: the user is not waiting on the answer to
  "should this be remembered", so the second LLM call belongs off the
  critical path.
* **Distilled, not dumped.** The raw description is ~700 characters of
  "This image shows…" written for a blind reader. Stored verbatim it
  poisons recall — it is long, it is phrased as a caption rather than a
  memory, and its opening words are identical for every photo, which is
  exactly the wrong thing to hand a semantic index. One line about what
  was in it retrieves far better.
* **``event``, not ``knowledge``.** F9's ``knowledge`` kind is for
  impersonal evergreen facts and its distil prompt actively rejects
  personal material. Someone showing you their dog is the opposite of
  that: it is dated, it is about him, and its value is relational.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Callable

log = logging.getLogger("app.vision.memory")

# Photos are worth keeping but shouldn't crowd out things he said.
IMAGE_MEMORY_SALIENCE = 0.55
# One line. The cap is generous enough for a compound scene and tight
# enough that a rambling model can't smuggle the whole caption through.
MAX_MEMORY_CHARS = 320

_DISTIL_SYSTEM = (
    "You turn a description of a photo somebody shared into a single "
    "memory line, written from the point of view of the person they "
    "showed it to.\n"
    "Rules:\n"
    "- One sentence, under 40 words, past tense.\n"
    "- Lead with what was in the picture, not with the fact a picture "
    "was sent.\n"
    "- Keep the concrete specifics: place, subject, colours, text, "
    "anything countable. Those are the parts worth remembering.\n"
    "- Drop hedging and photography talk (\"the image shows\", "
    "\"appears to be\", \"in the foreground\").\n"
    "- Use the sharer's name if you are given it.\n"
    "- If the description is too vague to be worth remembering, reply "
    "with exactly: SKIP\n"
    "Reply with the sentence only, no quotes, no preamble."
)


def distil_image_memory(
    *,
    description: str,
    user_text: str,
    user_name: str,
    chat: Callable[..., str],
) -> str:
    """Compress a vision description into one memory line, or ``""``.

    ``chat`` is the worker-model callable. Any failure returns ``""``:
    a photo we couldn't summarise is not worth a malformed memory row.
    """
    described = (description or "").strip()
    if not described:
        return ""
    caption = (user_text or "").strip()
    said = (
        f"{user_name} said, sharing it: \"{caption[:300]}\"\n"
        if caption else ""
    )
    prompt = (
        f"{said}"
        f"Description of the photo {user_name} shared:\n{described[:2000]}\n\n"
        "Write the memory line."
    )
    try:
        raw = chat(
            [
                {"role": "system", "content": _DISTIL_SYSTEM},
                {"role": "user", "content": prompt},
            ],
            think=False,
            surface="vision_memory_distil",
        )
    except Exception:
        log.debug("image memory distil call failed", exc_info=True)
        return ""

    line = " ".join((raw or "").strip().split())
    if not line or line.upper().startswith("SKIP"):
        return ""
    # Models like to wrap a single-sentence answer in quotes.
    if len(line) > 1 and line[0] in "\"'" and line[-1] == line[0]:
        line = line[1:-1].strip()
    if len(line) > MAX_MEMORY_CHARS:
        line = line[:MAX_MEMORY_CHARS].rsplit(" ", 1)[0].rstrip(",;: ") + "…"
    return line


def write_image_memory(
    *,
    line: str,
    filename: str,
    rel_path: str,
    source_message_id: int | None,
    now: datetime,
    embedder: Any,
    memory_store: Any,
    notify_memory_added: Callable[[dict[str, Any]], None] | None = None,
) -> int | None:
    """Persist one distilled image memory. Returns its id, or ``None``.

    ``None`` covers both failure and the dedupe case (the store returns
    nothing when the line is too close to an existing memory), which is
    the desired behaviour for someone re-sending the same photo.

    The ``source_attachment`` metadata is the first attachment reference
    on a memory row anywhere in the system. It is what lets a later
    caller get from "she remembers the mountain" back to the actual file.
    """
    text = (line or "").strip()
    if not text:
        return None
    try:
        embedding = embedder.embed(text)
    except Exception:
        log.warning("image memory embed failed", exc_info=True)
        return None
    try:
        memory = memory_store.add(
            content=text,
            kind="event",
            embedding=embedding,
            salience=IMAGE_MEMORY_SALIENCE,
            tier="long_term",
            source_message_id=source_message_id,
            event_time=now.isoformat(),
            metadata={
                "source": "shared_image",
                "source_attachment": rel_path,
                "source_filename": filename,
                "seen_at": now.isoformat(),
            },
        )
    except Exception:
        log.warning("image memory write failed", exc_info=True)
        return None
    if memory is None:
        log.info("image memory deduped against an existing row: %s", filename)
        return None
    if notify_memory_added is not None:
        try:
            notify_memory_added(memory.to_dict())
        except Exception:
            log.debug("image memory notify failed", exc_info=True)
    log.info("image memory written: id=%s %s", memory.id, text[:90])
    return int(memory.id)


__all__ = [
    "IMAGE_MEMORY_SALIENCE",
    "MAX_MEMORY_CHARS",
    "distil_image_memory",
    "write_image_memory",
]

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
* **Both people have to be named in the distil prompt.** The vision pass
  deliberately refuses to guess identities ("Do not guess at who someone
  is" in ``in_turn_prompt``), so the description that arrives here is
  always third-person — "an anime girl with silver-white hair". This is
  therefore the *only* step that sees his caption, and so the only place
  the subject's ownership can be worked out. Get the deixis wrong and she
  files a picture of herself as his: see
  :func:`_build_distil_system`.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Callable

from app.core.infra import timephrase
from app.core.vision.image_evidence import parse_note, source_current

log = logging.getLogger("app.vision.memory")

# Photos are worth keeping but shouldn't crowd out things he said.
IMAGE_MEMORY_SALIENCE = 0.55
# One line. The cap is generous enough for a compound scene and tight
# enough that a rambling model can't smuggle the whole caption through.
MAX_MEMORY_CHARS = 320

def _build_distil_system(user_name: str, assistant_name: str) -> str:
    """Distil prompt, with both people named so pronouns can resolve.

    The "who is who" paragraph is the whole reason this is a factory.
    Naming only the sharer and saying "write from the point of view of
    the person they showed it to" leaves the recipient anonymous, and a
    caption addressed to her then has nothing to bind to: told *"I
    wanted to show you your nice daily outfit"*, the model attached
    "your" to the only named person in the prompt and wrote **"Jacob
    showed me his daily outfit"** over a picture of Aiko. The same run
    of shared avatar images also produced "Jacob's anime character" and
    "an illustration of a white-haired nekomimi" — three for three, she
    could not recognise herself.

    Second-person captions are the norm rather than an edge case here:
    the pictures he shares are very often *of her* (an outfit, an
    expression, a render), so the recipient of the photo is also its
    subject. That is the case the original prompt had no way to express,
    and it is why the anchor is stated as a rule rather than left for
    the model to infer.

    Mirrors the deictic anchor the belief worker puts on its transcript
    lines, and the ``_build_system_prompt(name)`` factory convention the
    memory extractor established.
    """
    sharer = (user_name or "").strip() or "the user"
    me = (assistant_name or "").strip() or "Aiko"
    return (
        "You turn a description of a photo somebody shared into a single "
        f"memory line, written in the first person as {me} — the person "
        "it was shown to.\n"
        f"Who is who: {sharer} shared the photo with you, {me}. In "
        f"anything {sharer} said while sharing it, \"I\" / \"me\" / "
        f"\"my\" means {sharer}, and \"you\" / \"your\" means YOU, {me}. "
        f"So if {sharer} calls it \"your outfit\" it is *yours* — write "
        "\"my outfit\", never \"his\" or \"hers\".\n"
        f"When what {sharer} said makes clear the picture is of you — "
        "\"your outfit\", \"you in the new dress\" — write it as "
        "yourself: \"my new daily outfit, black cropped top and chunky "
        "sneakers\", not \"an anime girl in a black cropped top\". When "
        "nothing identifies the subject, describe it plainly and do NOT "
        f"assume it is you; {sharer} also shares pictures of other "
        "characters, other people and places.\n"
        "Rules:\n"
        "- One sentence, under 40 words, describing the representation.\n"
        "- Lead with what was in the picture, not with the fact a picture "
        "was sent.\n"
        "- Keep the concrete specifics: place, subject, colours, text, "
        "anything countable. Those are the parts worth remembering.\n"
        "- Preserve uncertainty, negation and conditions. A render, imagined "
        "scene or old photo does not prove a physical event happened when shared.\n"
        f"- Refer to the sharer as {sharer}.\n"
        "- If the description is too vague to be worth remembering, reply "
        "with exactly: SKIP\n"
        "Return JSON: {summary:string, subject:user|aiko|relationship|world|unknown, "
        "subject_quote:literal caption span or empty, depicted_domain:real|virtual|unknown, "
        "domain_quote:literal caption span or empty, caption_quote:literal caption span or empty, "
        "visual_quote:literal description span or empty, depicted_time_quote:literal caption "
        "timing span or empty}. Use unknown without support. Subject means who is depicted, "
        "not who uploaded it. Separate caption testimony from visual interpretation. "
        "A caption's date is not independently verified event occurrence.\n"
        + timephrase.STORED_TEXT_TIME_RULE
    )


def distil_image_memory(
    *,
    description: str,
    user_text: str,
    user_name: str,
    chat: Callable[..., str],
    assistant_name: str = "Aiko",
    shared_at: datetime | None = None,
) -> str:
    """Compress a vision description into one memory line, or ``""``.

    ``chat`` is the worker-model callable. Any failure returns ``""``:
    a photo we couldn't summarise is not worth a malformed memory row.

    ``assistant_name`` is not cosmetic — it is what lets a second-person
    caption ("your new outfit") resolve to the right owner. See
    :func:`_build_distil_system`.
    """
    described = (description or "").strip()
    if not described:
        return ""
    caption = (user_text or "").strip()
    shared_at = shared_at or timephrase.utcnow()
    if len(caption) > 3000 or len(described) > 6000:
        return ""
    from types import SimpleNamespace

    transcript = timephrase.format_transcript([
        SimpleNamespace(role="user", content=caption, created_at=shared_at.isoformat()),
        SimpleNamespace(role="assistant", content=described, created_at=shared_at.isoformat()),
    ])
    said = (
        f"{user_name} said to you while sharing it: \"{caption}\"\n"
        if caption else ""
    )
    prompt = (
        f"{timephrase.today_anchor()}\nSharing source recorded at {shared_at.isoformat()}.\n"
        f"{said}Age-tagged caption and visual description (different source roles):\n"
        f"{transcript}\n\n"
        "Write the memory line."
    )
    try:
        raw = chat(
            [
                {
                    "role": "system",
                    "content": _build_distil_system(user_name, assistant_name),
                },
                {"role": "user", "content": prompt},
            ],
            think=False,
            surface="vision_memory_distil",
        )
    except Exception:
        log.debug("image memory distil call failed", exc_info=True)
        return ""

    return parse_note(raw, caption=caption, description=described, shared_at=shared_at,
                      max_chars=MAX_MEMORY_CHARS)


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
    source_session: str | None = None,
    caption_receipt: dict | None = None,
) -> int | None:
    """Persist one distilled image memory. Returns its id, or ``None``.

    ``None`` covers both failure and the dedupe case (the store returns
    nothing when the line is too close to an existing memory), which is
    the desired behaviour for someone re-sending the same photo.

    The ``source_attachment`` metadata is the first attachment reference
    on a memory row anywhere in the system. It is what lets a later
    caller get from "she remembers the mountain" back to the actual file.
    """
    from dataclasses import asdict
    from app.core.memory.memory_origin import MemoryOrigin

    summary = (line or "").strip()
    if not summary or len(summary) > MAX_MEMORY_CHARS or not source_current(
            caption_receipt, memory_store):
        return None
    details = {"version": 1, "subject": "unknown", "depicted_domain": "unknown",
               "precision": "unknown", **dict(getattr(line, "details", {}) or {})}
    details.update(shared_at=now.isoformat(), scope="depicted_content",
                   sources=[caption_receipt] if caption_receipt else [])
    subject = details.get("subject", "unknown")
    domain = "virtual" if details.get("depicted_domain") == "virtual" else "conversation"
    text = f"Image shared at {now.isoformat()}; visual interpretation: {summary}"
    if details.get("caption_quote"):
        text += f'; caption (quoted): "{details["caption_quote"]}"'
    try:
        embedding = embedder.embed(text)
    except Exception:
        log.warning("image memory embed failed", exc_info=True)
        return None
    try:
        if not source_current(caption_receipt, memory_store):
            return None
        memory = memory_store.add(
            content=text,
            kind="event",
            embedding=embedding,
            salience=IMAGE_MEMORY_SALIENCE,
            tier="long_term",
            source_message_id=source_message_id,
            source_session=source_session,
            temporal_type="past_event", event_time=None, provenance="inferred",
            metadata={
                "source": "shared_image",
                "source_attachment": rel_path,
                "source_filename": filename,
                "seen_at": now.isoformat(),
                "image_evidence": details,
                "memory_origin": asdict(MemoryOrigin(
                    subject, "interpretation", "image_memory", domain,
                )),
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

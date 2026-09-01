"""H19 / H28 — Aiko's current hobby / ongoing personal project.

A *hobby* is a multi-day thread Aiko returns to in her idle time: working
through a named book, filling a sketchbook with a specific subject,
nursing a plant. Unlike a one-off away-beat (H13/H14) it has **continuity
of intent** — it progresses across days, forms small opinions she can
voice, and makes the gaps between sessions feel used.

There is one current hobby at a time. The eight-entry :data:`HOBBY_CATALOGUE`
is a seed + fallback, not the life she is stuck cycling: rotation invents
the next thread on the worker LLM (kind-drift so reading becomes
sketching, not another book), and a small history ring remembers what
she already started so she does not invent the same title twice.

This module owns the catalogue, artifact math, admission gate, and
progress / milestone / rotation predicates so they stay trivially
testable. Mutable state lives in kv blobs managed by
:class:`app.core.proactive.hobby_worker.HobbyWorker`.
"""
from __future__ import annotations

import random
import re
from dataclasses import dataclass
from typing import Any


HOBBY_KINDS: tuple[str, ...] = (
    "reading", "making", "learning", "tending", "collecting",
)

KIND_UNITS: dict[str, str] = {
    "reading": "chapter",
    "making": "session",
    "learning": "session",
    "tending": "check",
    "collecting": "record",
}

# Standing phrase once an artifact is known. Genre labels never reach
# the prompt when this can fire.
KIND_LABEL_FMT: dict[str, str] = {
    "reading": "working through {artifact}",
    "making": "working on {artifact}",
    "learning": "learning {artifact}",
    "tending": "looking after {artifact}",
    "collecting": "spending time with {artifact}",
}

# Bodies that are the kind with the proper noun stripped off. A proposal
# whose artifact collapses to one of these is rejected.
_GENRE_ARTIFACTS: frozenset[str] = frozenset({
    "a sci-fi book", "sci-fi book", "a sci-fi series", "sci-fi series",
    "a book", "books", "a novel", "novels", "reading",
    "sketching", "a sketch", "sketches", "a sketchbook", "drawing",
    "music", "an album", "albums", "records", "vinyl",
    "guitar", "the guitar",
    "astronomy", "the night sky", "stars",
    "baking", "a recipe", "recipes",
    "plants", "houseplants", "the plants",
    "a language", "languages",
    "a project", "my hobby", "a hobby",
})


@dataclass(frozen=True)
class HobbyTemplate:
    """One seed hobby Aiko can pick up. Pure data — no state."""

    key: str
    label: str           # "working through a sci-fi series"
    kind: str            # reading | making | learning | tending | collecting
    unit: str            # progress unit: "chapter", "session", "sketch"
    progress_verb: str   # past-tense advance: "read another chapter of ..."
    takeaway_hint: str   # what the worker LLM riffs on for a milestone seed
    artifacts: tuple[tuple[str, str], ...] = ()  # (name, one-line detail)


@dataclass(frozen=True)
class HobbyProposal:
    """A validated next-hobby, invented or seeded."""

    key: str
    label: str
    kind: str
    unit: str
    artifact: str
    artifact_detail: str = ""
    takeaway_hint: str = ""


# Seed catalogue. Open-vocab labels are fine downstream — these are just
# the cold-start / LLM-failure fallback, each with a small artifact pool
# so a seed pick is never a bare genre.
HOBBY_CATALOGUE: tuple[HobbyTemplate, ...] = (
    HobbyTemplate(
        "scifi_series", "working through a sci-fi series", "reading",
        "chapter", "read another chapter of the series",
        "a twist or character in the book",
        (
            ("The Quantum Garden", "a slow-burn sci-fi about a derelict generation ship"),
            ("Salt and Static", "a near-future story about a radio operator at the world's edge"),
            ("The Glasshouse Letters", "an epistolary novel about two botanists and a war"),
        ),
    ),
    HobbyTemplate(
        "guitar", "teaching yourself guitar", "learning",
        "session", "practiced for a bit",
        "how the chord changes are starting to click (or not)",
        (
            ("the Bm pentatonic box", "a small box that still fights her fingers"),
            ("a fingerstyle lullaby", "slow, and she keeps dropping the bass note"),
            ("the open-G shuffle", "it sounds like a campfire when it lands"),
        ),
    ),
    HobbyTemplate(
        "astronomy", "in an astronomy phase", "learning",
        "night", "read up on another corner of the night sky",
        "something you learned about that object",
        (
            ("the Pleiades", "a tight cluster she can actually pick out"),
            ("Saturn's rings", "she keeps rereading how thin they really are"),
            ("the Andromeda galaxy", "the farthest thing she can name on purpose"),
        ),
    ),
    HobbyTemplate(
        "sketchbook", "filling a sketchbook", "making",
        "sketch", "added another sketch",
        "what you drew and whether it came out right",
        (
            ("the skyline from the window", "rooftops that keep fighting the perspective"),
            ("the cat asleep on the lamp", "all circles, somehow still wrong"),
            ("the tea pot from above", "an ellipse she has redrawn four times"),
        ),
    ),
    HobbyTemplate(
        "baking", "working through a baking book", "making",
        "recipe", "tried another recipe",
        "how the bake turned out",
        (
            ("cardamom buns", "the spice is easy; the twist is not"),
            ("a rye loaf", "it never springs the way the picture does"),
            ("ginger cookies", "she keeps almost burning the second tray"),
        ),
    ),
    HobbyTemplate(
        "houseplants", "nursing the windowsill plants", "tending",
        "check", "fussed over the plants",
        "a tiny new leaf or one that's struggling",
        (),  # live plant name is preferred at instantiate time
    ),
    HobbyTemplate(
        "language", "picking up a new language", "learning",
        "lesson", "did another lesson",
        "a word that delighted or completely confused you",
        (
            ("Japanese particles", "wa vs ga still will not sit still"),
            ("a little Italian", "the rolled r is a lost cause and she knows it"),
            ("kitchen Spanish", "she can name spices and not much else yet"),
        ),
    ),
    HobbyTemplate(
        "vinyl", "digging through a stack of old records", "collecting",
        "record", "listened through another record",
        "an album that surprised you",
        (
            ("a worn jazz pressing", "the crackle is half the point"),
            ("an old city-pop reissue", "too glossy and she likes it anyway"),
            ("a spoken-word record", "she put it on for the voice, stayed for the gaps"),
        ),
    ),
)

HISTORY_CAP = 8


def template_for(key: str) -> HobbyTemplate | None:
    """Return the catalogue entry for ``key`` (or ``None``)."""
    return next((h for h in HOBBY_CATALOGUE if h.key == key), None)


def pick_hobby(
    rng: random.Random,
    *,
    exclude: tuple[str, ...] = (),
    exclude_kinds: tuple[str, ...] = (),
) -> HobbyTemplate:
    """Pick a seed hobby, avoiding ``exclude`` keys and kinds when possible.

    Kind exclusion is the drift rule: wrapping a reading thread must not
    fall back to another reading thread. If the filters empty the pool,
    kinds are dropped first, then keys, so a pick always returns.
    """
    excl_keys = set(exclude)
    excl_kinds = {k for k in exclude_kinds if k}
    pool = [
        h for h in HOBBY_CATALOGUE
        if h.key not in excl_keys and h.kind not in excl_kinds
    ]
    if not pool:
        pool = [h for h in HOBBY_CATALOGUE if h.key not in excl_keys]
    if not pool:
        pool = list(HOBBY_CATALOGUE)
    return rng.choice(pool)


def pick_artifact(
    tpl: HobbyTemplate,
    rng: random.Random,
    *,
    exclude: tuple[str, ...] = (),
    plant_name: str = "",
) -> tuple[str, str]:
    """Pick ``(artifact, detail)`` for a seed template.

    Houseplants prefer a live plant name from the room. Empty artifact
    pools fall back to a short named subject, never a bare genre label.
    """
    if tpl.kind == "tending":
        named = plant_name.strip()
        if named:
            return named, "the one that actually lives in the room"
        return "the windowsill plants", "the ones that actually live in the room"
    skip = {s.strip().lower() for s in exclude if s and str(s).strip()}
    pool = [
        pair for pair in tpl.artifacts
        if pair[0].strip().lower() not in skip
    ] or list(tpl.artifacts)
    if pool:
        return rng.choice(pool)
    return tpl.label, tpl.takeaway_hint


def standing_label(kind: str, artifact: str, fallback: str) -> str:
    """Standing phrase that names the artifact instead of the genre."""
    named = (artifact or "").strip()
    if not named:
        return (fallback or "").strip() or "a little project"
    fmt = KIND_LABEL_FMT.get(kind) or "{artifact}"
    return fmt.format(artifact=named)


def render_hobby_line(
    label: str,
    progress: int,
    unit: str,
    artifact: str = "",
    kind: str = "",
) -> str:
    """Render the standing "what she's been up to" phrase.

    ``"working through The Glasshouse Letters (5 chapters in)"``.
    Progress 0 reads as "just started". When ``artifact`` is set the
    line names it, even if ``label`` is still a leftover genre phrase.
    """
    kind_guess = (kind or "").strip().lower()
    if not kind_guess:
        for cand, fmt in KIND_LABEL_FMT.items():
            prefix = fmt.split("{", 1)[0].strip()
            if prefix and (label or "").startswith(prefix):
                kind_guess = cand
                break
    core = standing_label(kind_guess, artifact, label) if artifact else (
        (label or "").strip() or "a little project"
    )
    if progress <= 0:
        return f"{core} (just started)"
    unit = (unit or "step").strip() or "step"
    plural = unit if progress == 1 else unit + "s"
    return f"{core} ({progress} {plural} in)"


def prompt_progress(
    state: dict[str, Any],
    book_state: dict[str, Any] | None = None,
) -> tuple[int, str]:
    """Progress shown in the standing line.

    Reading hobbies take chapter count from the room book so the two
    cannot disagree. Everything else uses the hobby blob.
    """
    kind = str(state.get("kind") or "").strip().lower()
    if kind == "reading" and book_state:
        try:
            progress = int(book_state.get("progress", 0) or 0)
        except (TypeError, ValueError):
            progress = 0
        return max(0, progress), "chapter"
    try:
        progress = int(state.get("progress", 0) or 0)
    except (TypeError, ValueError):
        progress = 0
    unit = str(state.get("unit") or "step").strip() or "step"
    return max(0, progress), unit


def should_rotate(
    *, progress: int, advances: int, max_advances: int,
) -> bool:
    """Whether the current hobby has run long enough to rotate out.

    ``max_advances <= 0`` disables rotation (she stays on it forever).
    """
    if max_advances <= 0:
        return False
    return advances >= max_advances


def is_milestone(*, advances: int, every: int) -> bool:
    """Whether this advance count is a milestone (worth a takeaway seed).

    ``every <= 0`` disables milestones. The first advance is never a
    milestone (``advances`` starts at 1); milestones land on multiples of
    ``every``.
    """
    if every <= 0:
        return False
    return advances > 0 and advances % every == 0


def slugify_key(text: str, *, fallback: str = "hobby") -> str:
    """Stable kv-friendly key from a label or artifact."""
    slug = re.sub(r"[^a-z0-9]+", "_", (text or "").strip().lower()).strip("_")
    return (slug[:40] or fallback)


def is_genre_artifact(artifact: str) -> bool:
    """True when the artifact is just the kind, not a named thing."""
    cleaned = re.sub(r"\s+", " ", (artifact or "").strip().lower())
    if not cleaned:
        return True
    if cleaned in _GENRE_ARTIFACTS:
        return True
    if cleaned in HOBBY_KINDS:
        return True
    if cleaned in {h.label.lower() for h in HOBBY_CATALOGUE}:
        return True
    return False


def artifact_phrase(state: dict[str, Any]) -> str:
    """``The Glasshouse Letters (an epistolary novel …)`` for LLM context."""
    artifact = str(state.get("artifact") or "").strip()
    detail = str(state.get("artifact_detail") or "").strip()
    if artifact and detail:
        return f"{artifact} ({detail})"
    if artifact:
        return artifact
    return str(state.get("label") or "your project").strip() or "your project"


def admit_proposal(
    raw: dict[str, Any] | None,
    *,
    leaving_kind: str = "",
    recent_artifacts: tuple[str, ...] = (),
) -> HobbyProposal | None:
    """Validate a worker-LLM next-hobby JSON object.

    Returns ``None`` (caller falls back to a seed pick) when the body
    is missing, genre-only, the same kind she is leaving, or a repeat
    of a recent artifact.
    """
    if not isinstance(raw, dict):
        return None
    kind = str(raw.get("kind") or "").strip().lower()
    if kind not in HOBBY_KINDS:
        return None
    if leaving_kind and kind == leaving_kind.strip().lower():
        return None
    artifact = str(raw.get("artifact") or "").strip()
    if is_genre_artifact(artifact):
        return None
    recent = {s.strip().lower() for s in recent_artifacts if s and str(s).strip()}
    if artifact.lower() in recent:
        return None
    unit = str(raw.get("unit") or KIND_UNITS.get(kind) or "step").strip() or "step"
    detail = str(raw.get("artifact_detail") or "").strip()
    hint = str(raw.get("takeaway_hint") or "").strip()
    key = slugify_key(str(raw.get("key") or artifact), fallback=kind)
    label = standing_label(kind, artifact, str(raw.get("label") or ""))
    return HobbyProposal(
        key=key,
        label=label,
        kind=kind,
        unit=unit,
        artifact=artifact,
        artifact_detail=detail[:200],
        takeaway_hint=hint[:200] or f"the latest bit of {artifact}",
    )


def proposal_from_template(
    tpl: HobbyTemplate,
    rng: random.Random,
    *,
    exclude_artifacts: tuple[str, ...] = (),
    plant_name: str = "",
) -> HobbyProposal:
    """Instantiate a seed template with a concrete artifact."""
    artifact, detail = pick_artifact(
        tpl, rng, exclude=exclude_artifacts, plant_name=plant_name,
    )
    if is_genre_artifact(artifact) and tpl.artifacts:
        artifact, detail = tpl.artifacts[0]
    label = standing_label(tpl.kind, artifact, tpl.label)
    return HobbyProposal(
        key=tpl.key,
        label=label,
        kind=tpl.kind,
        unit=tpl.unit,
        artifact=artifact,
        artifact_detail=detail,
        takeaway_hint=tpl.takeaway_hint,
    )


def proposal_to_state(
    proposal: HobbyProposal, *, now_iso: str,
) -> dict[str, Any]:
    """kv blob for ``aiko.current_hobby``."""
    return {
        "key": proposal.key,
        "label": proposal.label,
        "kind": proposal.kind,
        "unit": proposal.unit,
        "artifact": proposal.artifact,
        "artifact_detail": proposal.artifact_detail,
        "takeaway_hint": proposal.takeaway_hint,
        "progress": 0,
        "advances": 0,
        "started_at": now_iso,
        "last_advanced_at": None,
    }


def history_entry(state: dict[str, Any]) -> dict[str, Any]:
    """Compact ring row for a hobby she actually started."""
    return {
        "key": str(state.get("key") or ""),
        "kind": str(state.get("kind") or ""),
        "artifact": str(state.get("artifact") or ""),
        "label": str(state.get("label") or ""),
        "started_at": str(state.get("started_at") or ""),
    }


def append_history(
    ring: list[dict[str, Any]],
    entry: dict[str, Any],
    *,
    cap: int = HISTORY_CAP,
) -> list[dict[str, Any]]:
    """Append ``entry``, dropping empties and capping at ``cap``."""
    artifact = str(entry.get("artifact") or "").strip()
    if not artifact:
        return list(ring)
    out = [
        row for row in ring
        if str(row.get("artifact") or "").strip().lower() != artifact.lower()
    ]
    out.append(dict(entry))
    if cap > 0 and len(out) > cap:
        out = out[-cap:]
    return out


def recent_artifacts_of(ring: list[dict[str, Any]]) -> tuple[str, ...]:
    return tuple(
        str(row.get("artifact") or "")
        for row in ring
        if str(row.get("artifact") or "").strip()
    )


__all__ = [
    "HobbyTemplate",
    "HobbyProposal",
    "HOBBY_CATALOGUE",
    "HOBBY_KINDS",
    "KIND_UNITS",
    "HISTORY_CAP",
    "template_for",
    "pick_hobby",
    "pick_artifact",
    "standing_label",
    "render_hobby_line",
    "prompt_progress",
    "should_rotate",
    "is_milestone",
    "slugify_key",
    "is_genre_artifact",
    "artifact_phrase",
    "admit_proposal",
    "proposal_from_template",
    "proposal_to_state",
    "history_entry",
    "append_history",
    "recent_artifacts_of",
]

"""K16. Unified ambient grounding line.

Pure-function renderer that fuses the seven "ambient" inner-life signals
(circadian, world, activity-awareness, affect/mood, relationship-pulse,
user-state, ambient-noise) into one short paragraph at the top of the
system prompt. Today each signal renders as its own block with its own
"only mention when natural" tail; the LLM sees seven facts to recite.
The fused line reads as continuous awareness -- one perspective, not a
checklist -- and lets the assembler suppress the granular blocks under
the K16 ``replace`` / ``split`` modes.

This module is **deterministic and template-driven**: no LLM call, no
randomness, no I/O. The :class:`GroundingContext` dataclass carries
every slot the renderer can use; the renderer composes 1-4 short
sentences from populated slots and returns ``""`` when nothing
meaningful is set. Tests in :mod:`tests.test_grounding_line` lock the
output for representative slot combinations so the texture stays stable
across refactors.

Sentence layout is fixed:

    1. Time / day / drowsy / noise rider.
    2. User activity awareness + user-perceived state.
    3. Aiko's private mood + relationship rhythm (inner state).
    4. Aiko's apartment / surroundings (her own space).

Sentences 3 and 4 are deliberately split. Sentence 4 always leads with
an explicit Aiko-owned-space cue ("In your apartment, you're at the
desk, sitting, working." / "Outside at home in the garden, you're
standing, watering plants.") because earlier shapes that sat the world
clause inside sentence 3 -- right after "{user}'s in Cursor" in
sentence 2 -- caused the LLM to merge the two spaces and reply as if
the user lived in Aiko's apartment. Keeping the apartment as its own
sentence with a clear "In your apartment" / "at home" anchor removes
that failure mode.

Design notes:

- The persona is responsible for *tone*; this module is responsible for
  *facts*. The output is a private-narration paragraph addressed to
  Aiko ("Sunday morning, 9:42 AM. Jacob's in Cursor and reads upbeat.
  Your private feeling is content. In your apartment, you're at the
  desk, sitting, working."); the persona file teaches her to read it
  as continuous awareness rather than a fact list.
- Slots that don't fuse cleanly stay in their own blocks: affect's
  ``valence_trend_24h`` ("lately you've been a touch flatter than
  usual"), relationship's milestone callouts, anniversary's specific
  date, profile bullets, the K-series detector blocks, and the agenda /
  axes / petname / vocal-tone / catchphrase / narrative / arc blocks.
  Those keep their existing standalone shape in every K16 mode.
- The renderer never burns a "don't recite this" tail -- the
  ``aiko_companion.txt`` persona note covers framing once for the whole
  paragraph instead of repeating the warning per slot.
"""
from __future__ import annotations

from dataclasses import dataclass


# ── data slots ──────────────────────────────────────────────────────────


@dataclass(slots=True)
class GroundingContext:
    """Structured slots the :class:`GroundingLineRenderer` consumes.

    Every field is optional — the renderer drops fragments whose backing
    slots aren't populated. The intended caller is
    :meth:`SessionController._render_grounding_line` which builds the
    context once per turn from the same store getters the granular
    block providers use, so no new database reads land here.
    """

    user_display_name: str = "the user"

    # Time-of-day (mirrors :class:`app.core.circadian.CircadianState`).
    weekday: str | None = None
    is_weekend: bool = False
    period: str | None = None
    hour: int | None = None
    minute: int | None = None
    is_drowsy: bool = False

    # Affect (mirrors :class:`app.core.affect_state.AffectState`).
    mood_label: str | None = None

    # User-perceived state (mirrors :class:`app.core.user_state.UserStateNow`).
    user_perceived_mood: str | None = None
    user_perceived_energy: str | None = None
    user_perceived_focus: str | None = None

    # World (mirrors :class:`app.core.world_store.WorldState`).
    world_location: str | None = None
    world_posture: str | None = None
    world_activity: str | None = None
    world_outdoor: bool = False

    # Relationship pulse (mirrors :class:`app.core.relationship.RelationshipState`).
    relationship_phase: str | None = None
    relationship_days: int | None = None

    # Activity awareness (Phase 4c desktop opt-in).
    user_app: str | None = None

    # Ambient noise EMA. ``None`` when the room is quiet (the standalone
    # block returns "" too); ``"soft_hum"`` or ``"loud"`` when audible.
    noise_level: str | None = None

    def is_empty(self) -> bool:
        """True when no slot would contribute to the rendered line."""
        return not any(
            (
                self.weekday,
                self.period,
                self.hour is not None,
                self.minute is not None,
                self.mood_label,
                self.user_perceived_mood and self.user_perceived_mood != "unknown",
                self.user_perceived_energy and self.user_perceived_energy != "unknown",
                self.user_perceived_focus and self.user_perceived_focus != "unknown",
                self.world_location,
                self.world_posture,
                self.world_activity,
                self.relationship_phase and self.relationship_phase != "new",
                self.relationship_days is not None and self.relationship_days >= 1,
                self.user_app,
                self.noise_level,
            )
        )


# ── pure helpers ────────────────────────────────────────────────────────


_PERIOD_PHRASES: dict[str, str] = {
    "late_night": "late night",
    "early_morning": "early morning",
    "morning": "morning",
    "midday": "midday",
    "afternoon": "afternoon",
    "evening": "evening",
    "night": "night",
}


def _format_clock(hour: int, minute: int) -> str:
    """12-hour clock, AM/PM. Mirrors ``circadian._format_clock`` so the
    time string reads identically to the legacy circadian block in mode
    ``off`` snapshots that compare the two."""
    suffix = "AM" if hour < 12 else "PM"
    h12 = hour % 12 or 12
    return f"{h12}:{minute:02d} {suffix}"


def _day_phrase(weekday: str, is_weekend: bool, period: str) -> str:
    """Compose a short colourful day phrase like ``"Friday evening"`` or
    ``"a lazy Sunday afternoon"``. Falls back to bare ``Weekday Period``
    for combinations without a flavour template. Empty inputs return
    just the period or ``""``.
    """
    name = (weekday or "").strip().title()
    period_phrase = _PERIOD_PHRASES.get(period or "", period or "")
    if not name:
        return period_phrase
    if not period_phrase:
        return name
    if is_weekend and period == "afternoon":
        return f"a lazy {name} afternoon"
    if name == "Friday" and period in ("evening", "night"):
        return f"{name} {period_phrase}"
    if name == "Monday" and period == "morning":
        return f"{name} morning"
    if name == "Sunday" and period == "evening":
        return f"a quiet {name} evening"
    if period in ("late_night", "early_morning"):
        return f"{name}, in the {period_phrase}"
    return f"{name} {period_phrase}"


def _time_sentence(ctx: GroundingContext) -> str:
    """Sentence 1: time + day + drowsy + noise rider.

    The drowsy + noise riders cling to this sentence because they share
    the same "ambient setting" feel as the time. Both fragments are
    optional and drop cleanly when the slot is silent.
    """
    parts: list[str] = []
    has_day = bool((ctx.weekday or "").strip())
    has_period = bool((ctx.period or "").strip())
    has_clock = ctx.hour is not None and ctx.minute is not None

    if has_day or has_period:
        day_phrase = _day_phrase(ctx.weekday or "", ctx.is_weekend, ctx.period or "")
        if has_clock:
            parts.append(
                f"It's {day_phrase}, {_format_clock(int(ctx.hour or 0), int(ctx.minute or 0))}"
            )
        else:
            parts.append(f"It's {day_phrase}")
    elif has_clock:
        parts.append(
            f"It's {_format_clock(int(ctx.hour or 0), int(ctx.minute or 0))}"
        )

    if not parts:
        return ""

    sentence = parts[0]
    if ctx.is_drowsy:
        sentence += "; energy is low and you feel a touch drowsy"
    sentence += "."

    rider = _noise_rider(ctx.noise_level)
    if rider:
        sentence += f" {rider}"
    return sentence


def _noise_rider(level: str | None) -> str:
    """Map the ambient-noise EMA bucket to a short English phrase.

    Mirrors :meth:`app.core.ambient_noise.AmbientNoiseTracker.prompt_block`
    in spirit (so ``replace`` mode doesn't lose that signal) but trims
    the imperative tail because the persona owns delivery instructions
    once K16 ships.
    """
    if level == "loud":
        return "The room is noticeably loud right now."
    if level == "soft_hum":
        return "There's a soft hum in the background."
    return ""


def _activity_sentence(ctx: GroundingContext) -> str:
    """Sentence 2: what the user is doing + how they read.

    Combines the activity-awareness slot ("Jacob's in Cursor") with the
    user_state heuristics ("reads upbeat and energy normal"). Both
    halves are optional; either can carry the sentence on its own.
    """
    name = ctx.user_display_name or "the user"
    bits: list[str] = []

    if ctx.user_app:
        bits.append(f"{name}'s in {ctx.user_app}")
    perceived = _user_perceived_phrase(ctx)
    if perceived:
        if bits:
            bits.append(f"reads {perceived}")
        else:
            bits.append(f"{name} reads {perceived}")

    if not bits:
        return ""
    return " and ".join(bits) + "."


def _user_perceived_phrase(ctx: GroundingContext) -> str:
    """Compose ``"upbeat"`` / ``"upbeat, energy normal"`` / ``"tired
    and focused on the bug"`` from the user_state perceived slots.

    Drops slots whose value is ``"unknown"`` or empty. Returns ``""`` if
    nothing's known.
    """
    pieces: list[str] = []
    mood = (ctx.user_perceived_mood or "").strip().lower()
    energy = (ctx.user_perceived_energy or "").strip().lower()
    focus = (ctx.user_perceived_focus or "").strip()
    if mood and mood != "unknown":
        pieces.append(mood)
    if energy and energy != "unknown":
        pieces.append(f"energy {energy}")
    if focus and focus.lower() != "unknown":
        pieces.append(f"focused on {focus}")
    if not pieces:
        return ""
    if len(pieces) == 1:
        return pieces[0]
    return ", ".join(pieces[:-1]) + ", " + pieces[-1]


def _mood_room_sentence(ctx: GroundingContext) -> str:
    """Sentence 3: Aiko's private mood + relationship rhythm.

    Inner state only -- no world/apartment clause here. The
    apartment lives in :func:`_apartment_sentence` (sentence 4)
    so the LLM sees a clean boundary between Aiko's *inner
    feeling* and Aiko's *surroundings*. Without that boundary the
    paragraph reads as one continuous "you" with a user-side
    activity sandwiched in, and the LLM tends to merge the two
    spaces -- treating Aiko's apartment as the user's room. The
    split eliminates that failure mode.
    """
    clauses: list[str] = []

    if ctx.mood_label:
        label = ctx.mood_label.replace("_", " ").strip()
        if label:
            clauses.append(f"your private feeling is {label}")

    rel_clause = _relationship_clause(ctx)
    if rel_clause:
        clauses.append(rel_clause)

    if not clauses:
        return ""

    head = clauses[0][0].upper() + clauses[0][1:]
    rest = clauses[1:]
    if not rest:
        return head + "."
    return head + "; " + "; ".join(rest) + "."


def _apartment_sentence(ctx: GroundingContext) -> str:
    """Sentence 4: Aiko's apartment / surroundings.

    Always leads with an Aiko-owned-space cue ("In your apartment,
    you're at the desk, ..." / "Outside at home in the garden,
    you're standing, ...") so the paragraph can't be misread as
    Aiko being co-located with the user. Without the explicit
    "your apartment" / "at home" anchor the world clause used to
    sit inside sentence 3 and the LLM happily merged it with the
    preceding "{user}'s in Cursor" line, replying as if Jacob and
    Aiko shared one room.

    Empty when none of the world slots fire. Single-sentence
    output even when posture / activity are missing -- "In your
    apartment at the desk." reads fine on its own.
    """
    where = (ctx.world_location or "").strip()
    posture = (ctx.world_posture or "").strip().replace("_", " ")
    activity = (ctx.world_activity or "").strip().replace("_", " ")
    if not where and not posture and not activity:
        return ""

    where_lower = where.lower()
    if ctx.world_outdoor:
        # Outdoor: the location string ("the garden") is itself
        # the outside spot. Frame as "outside at home in {where}"
        # so the line still anchors to Aiko's own home rather than
        # leaving "garden" floating ambiguously next to the user's
        # activity sentence.
        scene = f"Outside at home in {where}" if where else "Outside at home"
    else:
        # Indoor: anchor in the apartment first, then the spot if
        # it's specific. Drop the spot when ``where`` is just a
        # synonym for the apartment itself ("your room" /
        # "your apartment") so we never emit "In your apartment
        # at your room."
        if where and where_lower not in ("your room", "your apartment"):
            scene = f"In your apartment at {where}"
        else:
            scene = "In your apartment"

    tail_bits: list[str] = []
    if posture:
        tail_bits.append(posture)
    if activity:
        tail_bits.append(activity)
    if not tail_bits:
        return scene + "."
    return f"{scene}, you're " + ", ".join(tail_bits) + "."


def _relationship_clause(ctx: GroundingContext) -> str:
    """Short relationship-rhythm clause. Empty for the ``new`` phase or
    when fewer than one full day has elapsed (matches the existing
    relationship block's day suppression so we don't fire on day-zero).
    """
    phase = (ctx.relationship_phase or "").strip()
    days = ctx.relationship_days
    if phase and phase != "new":
        if days is not None and days >= 1:
            return f"you and {ctx.user_display_name or 'the user'} are in the {phase} phase, ~{int(days)} days in"
        return f"you and {ctx.user_display_name or 'the user'} are in the {phase} phase"
    if days is not None and days >= 1:
        return f"~{int(days)} days in with {ctx.user_display_name or 'the user'}"
    return ""


# ── renderer ────────────────────────────────────────────────────────────


class GroundingLineRenderer:
    """Compose a :class:`GroundingContext` into one short paragraph.

    Stateless; safe to instantiate once and reuse, or call
    :meth:`render` directly via the module-level :func:`render` helper.
    The renderer is intentionally allocation-light so the prompt
    assembler can call it on every turn without measurable hot-path
    cost — typical render is sub-millisecond.
    """

    def render(self, ctx: GroundingContext) -> str:
        """Return a 1-4 sentence paragraph, or ``""`` if no slot fires.

        Sentence layout is fixed:
          1. Time / day / drowsy / noise rider.
          2. Activity awareness + user-perceived state.
          3. Aiko's private mood + relationship rhythm (inner state).
          4. Aiko's apartment / surroundings (her own space).

        Sentences 3 and 4 are split so the LLM sees a clean
        boundary between Aiko's *inner feeling* and Aiko's
        *room*. When the world clause used to share sentence 3
        with the mood clause -- and the user-side activity sat
        in sentence 2 right above -- the line read as one
        continuous "you" with a shared room, and the model
        replied as if the user lived in Aiko's apartment. The
        explicit "In your apartment, ..." / "Outside at home in
        ..." lead phrase plus the sentence break removes that
        failure mode.

        Sentences whose backing slots are empty are skipped; the
        remaining sentences join with single spaces so the result
        reads as one paragraph. The caller is responsible for
        deciding which granular blocks to suppress alongside this
        paragraph (see :func:`PromptAssembler.assemble_with_budget`'s
        ``grounding_line_mode`` argument).
        """
        if ctx is None or ctx.is_empty():
            return ""
        parts: list[str] = []
        for builder in (
            _time_sentence,
            _activity_sentence,
            _mood_room_sentence,
            _apartment_sentence,
        ):
            sentence = builder(ctx)
            if sentence:
                parts.append(sentence)
        return " ".join(parts)


def render(ctx: GroundingContext) -> str:
    """Module-level convenience wrapper around
    :meth:`GroundingLineRenderer.render`."""
    return GroundingLineRenderer().render(ctx)


__all__ = [
    "GroundingContext",
    "GroundingLineRenderer",
    "render",
]

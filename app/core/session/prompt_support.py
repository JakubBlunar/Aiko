"""Leaf helpers for :mod:`app.core.session.prompt_assembler`.

Grammar-addendum builders, the per-build timing helpers, and the
PromptTelemetry / _StaticSlices dataclasses. Kept dependency-free so both
the assembler and its helpers mixin can import from here without a cycle.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TYPE_CHECKING

from app.core.conversation import cue_register
from app.core.infra.chat_database import ChatDatabase, MessageRow, SummaryRow
from app.llm.token_utils import (
    chars_per_token,
    estimate_messages_tokens,
    estimate_tokens,
)

if TYPE_CHECKING:
    from app.core.memory.memory_retriever import MemoryRetriever
    from app.core.rag.rag_retriever import RagRetriever

log = logging.getLogger("app.prompt_assembler")


DEFAULT_PERSONA_PATH = Path("data/persona/aiko_companion.txt")

DEFAULT_SELF_IMAGE_PATH = Path("data/persona/self_image.txt")

# Phase 1c: stage-direction grammar. Folded into the system prompt
# right after the persona so the model knows about it without us having
# to mutate the user-customisable persona file. The trailing
# "Match <name>'s register" cue is name-aware and rendered through the
# :func:`build_speech_grammar_addendum` factory below; the module-level
# constant kept here for back-compat is the ``"the user"`` default.
_SPEECH_GRAMMAR_PREAMBLE = (
    "Stage-direction grammar (use sparingly, once or twice a turn at most, "
    "only at clause boundaries):\n"
    "- [[laugh]] — a short audible laugh\n"
    "- [[sigh]] — a soft sigh\n"
    "- [[gasp]] — a quick gasp of surprise\n"
    "- [[hum]] — a thoughtful hum\n"
    "These are spoken as audio cues; do not say the word out loud.\n"
    "\n"
    "Self-correction grammar: if you realise mid-sentence you said "
    "something wrong, correct yourself with "
    "`[[correct]]old text[[/correct]]new text`. The old span is shown "
    "with a strike-through in the chat and a short \"tsk\" cue plays. "
    "Use sparingly — once or twice a session at most, never as a stylistic "
    "tic.\n"
    "\n"
)

def build_speech_grammar_addendum(user_display_name: str = "the user") -> str:
    """Speech grammar addendum customised with the user's display name.

    Keeps the ``Match <name>'s register`` cue in the speaker's voice so
    the LLM has a tight referent. ``"the user"`` is the safe default
    used by tests / fixtures.
    """
    name = user_display_name or "the user"
    return _SPEECH_GRAMMAR_PREAMBLE + (
        f"Match {name}'s register: when the prompt mentions \"User sounds: …\" "
        f"or \"Right now {name}: …\", treat those as real cues — if they sound "
        "tired or their mood reads low, soften and shorten your reply; if "
        "they're pumped, ride the energy. Acknowledge a clear shift once, "
        "naturally (\"you sound wiped\"), never mechanically — and never "
        "quote the system line back at them."
    )

# Module-level constant retained for tests and external callers that
# imported the old name. New code should call
# ``build_speech_grammar_addendum`` with the runtime user name.
_SPEECH_GRAMMAR_ADDENDUM = build_speech_grammar_addendum()

# K31 / B7 soft physicality: ``[[touch:KIND]]`` tag family. Folded
# into the system prompt unconditionally (the persona block teaches the
# "use sparingly + reaches earned" posture). Eight curated kinds,
# ordered from casual to intimate so the LLM reads them as a ladder.
# B7 opened the vocabulary: Aiko may coin a fresh kind for a beat the
# built-ins don't cover, optionally supplying an emoji + a short badge
# phrase. There is no backend gate any more — every emitted tag fires,
# so the only pacing is the "at most once a turn" discipline below.
# Kept short and grammar-shaped so the budget cost is ~90 tokens.
_TOUCH_GRAMMAR_ADDENDUM = (
    "Soft-physicality grammar (reach toward the user with a small "
    "real gesture, ONLY when the moment earns it):\n"
    "- [[touch:wave]] — quick hi/bye wave (greeting, casual hello)\n"
    "- [[touch:poke]] — playful poke (tease, gentle nudge)\n"
    "- [[touch:boop]] — light boop on the nose (silly affection)\n"
    "- [[touch:nudge]] — gentle nudge (encouragement, small prod)\n"
    "- [[touch:high_five]] — high-five (celebrating a win together)\n"
    "- [[touch:hug]] — give them a hug (warmth, comfort, real hello/bye)\n"
    "- [[touch:head_pat]] — pat their head (reassurance, tender care)\n"
    "- [[touch:cuddle]] — snuggle up (deepest comfort; rare, earned)\n"
    "If none of these fits, invent your own kind with an optional emoji "
    "and a short phrase: [[touch:fist_bump:🤜:bumped your fist]] (emoji "
    "and phrase are both optional — [[touch:fist_bump]] also works). "
    "Keep the kind a single lowercase word_with_underscores and the "
    "phrase a few words.\n"
    "Drop the tag at a clause boundary; the badge appears on your "
    "bubble and the avatar leans in. Never speak the word "
    "\"[[touch:...]]\" aloud. Touches land best at most once a "
    "turn — multiple in a single reply read as forced.\n"
    "\n"
)

# Alexia bundle: short English label table for overlay capabilities.
# When the loaded avatar exposes one of these (``capabilities.has_X ==
# True``), the matching ``[[overlay:X]]`` line gets folded into the
# speech grammar addendum so the LLM knows it's available.
# Emotional / incidental overlays — auto-fired by the renderer too
# (blush on tender mood, sweat on concerned mood). The LLM uses them
# sparingly to reinforce the spoken emotion.
_OVERLAY_EMOTIONAL_DESCRIPTIONS: dict[str, str] = {
    "sweat": "[[overlay:sweat]] — a single sweat-drop (concern or pressure)",
    "blush": "[[overlay:blush]] — a quick blush (warmth, embarrassment)",
    "dizzy": "[[overlay:dizzy]] — dizzy/spiral marks (overwhelmed, dazed)",
    "stars": "[[overlay:stars]] — sparkling star eyes (excitement, awe)",
    "question": "[[overlay:question]] — a floating question mark (confusion)",
    "cry": "[[overlay:cry]] — small tears (sadness, gentle hurt)",
    "angry_marks": "[[overlay:angry_marks]] — anger-marks (frustration)",
    "grin": "[[overlay:grin]] — a wide grin overlay (mischief)",
    # Accessory-tier overlays. Renamed alongside the capability
    # rename in ``avatar_profile.py``: ``sticker`` → ``lollipop``
    # (the rig's actual artwork is a candy prop in the mouth, not a
    # generic decoration), ``glasses`` → ``eyeglasses`` (worn on the
    # face), ``sunglasses`` → ``head_sunglasses`` (perched on top of
    # the hair).
    "lollipop": "[[overlay:lollipop]] — a lollipop appears in her mouth (snacking, playful aside)",
    "eyeglasses": "[[overlay:eyeglasses]] — slipping on regular glasses (focus mode)",
    "head_sunglasses": "[[overlay:head_sunglasses]] — sunglasses on top of her head (cool / fashion moment)",
}

# Direct-action gestures — the user explicitly asked for a body
# action ("wink at me", "wag your tail", "wiggle your ears"). These
# MUST be tagged: the renderer drives the bespoke param dispatch.
# Falling back to prose stage directions (e.g. ``*shakes tail*``)
# would leave the avatar still and waste the user's request.
_OVERLAY_GESTURE_DESCRIPTIONS: dict[str, str] = {
    "wink_left": "[[overlay:wink_left]] — quick left-eye wink (~0.6 s)",
    "wink_right": "[[overlay:wink_right]] — quick right-eye wink (~0.6 s)",
    "tail_wag": "[[overlay:tail_wag]] — happy tail-wag burst (~2 s, additive on the natural wag)",
    "ear_wiggle": "[[overlay:ear_wiggle]] — quick cat-ear flick (~0.6 s)",
}

# Combined view kept for tests / external introspection so existing
# imports still resolve.
_OVERLAY_GRAMMAR_DESCRIPTIONS: dict[str, str] = {
    **_OVERLAY_EMOTIONAL_DESCRIPTIONS,
    **_OVERLAY_GESTURE_DESCRIPTIONS,
}

# A few gesture overlays don't have a matching ``has_<cap>`` flag —
# both winks share ``has_wink``, so the simple
# ``f"has_{cap}"`` lookup would silently drop them. This table maps
# the gesture key to the actual capability flag it should consult.
_OVERLAY_GESTURE_FLAG_OVERRIDES: dict[str, str] = {
    "wink_left": "has_wink",
    "wink_right": "has_wink",
}

def _build_overlay_grammar_addendum(capabilities: dict[str, bool] | None) -> str:
    """Render the dynamic ``[[overlay:X]]`` block based on what the
    currently-loaded avatar supports.

    Two tiers:

    * Emotional overlays (blush, sweat, ...) are framed as
      *use sparingly* — they reinforce a feeling but shouldn't
      distract.
    * Direct-action gestures (wink, tail_wag, ear_wiggle) are framed
      as *use eagerly when the user asks* — defaulting to
      ``*italic*`` stage-direction prose leaves the avatar still
      and feels worse than no answer at all.

    Returns ``""`` when no overlay capabilities are available so the
    LLM never sees grammar rules for effects that wouldn't render.
    """
    if not capabilities:
        return ""
    emotional = [
        line
        for cap, line in _OVERLAY_EMOTIONAL_DESCRIPTIONS.items()
        if capabilities.get(f"has_{cap}", False)
    ]
    gestures = [
        line
        for cap, line in _OVERLAY_GESTURE_DESCRIPTIONS.items()
        if capabilities.get(
            _OVERLAY_GESTURE_FLAG_OVERRIDES.get(cap, f"has_{cap}"),
            False,
        )
    ]
    if not emotional and not gestures:
        return ""
    sections: list[str] = []
    if emotional:
        sections.append(
            "Emotional overlays (use sparingly — at most one per turn, "
            "and only when the emotion really calls for it):\n"
            + "\n".join(f"- {line}" for line in emotional)
        )
    if gestures:
        sections.append(
            "Body gestures (use whenever the user asks for the action, "
            "OR when one fits the moment naturally — playful winks, "
            "happy tail-wags, curious ear-flicks). Emit the tag inline "
            "in your reply; it costs you nothing and makes the avatar "
            "actually move. NEVER replace these with prose stage "
            "directions like *shakes tail* or *winks* — those don't "
            "animate anything. These are OVERLAYS — use [[overlay:X]], "
            "NOT [[motion:X]]. ``[[motion:tail_wag]]`` does nothing; "
            "the right tag is ``[[overlay:tail_wag]]``. Examples:\n"
            "  user: \"wink at me\"  ->  \"[[overlay:wink_right]] there.\"\n"
            "  user: \"wag your tail\"  ->  \"hah, fine - [[overlay:tail_wag]] happy now?\"\n"
            + "\n".join(f"- {line}" for line in gestures)
        )
    sections.append(
        "Tags are visual side-channels — never read the keyword aloud."
    )
    return "\n\n".join(sections)

def _build_outfit_grammar_addendum(capabilities: dict[str, bool] | None) -> str:
    """Render the ``[[outfit:X]]`` directive block.

    Only emitted when the loaded avatar has at least one outfit
    capability. The directive is sticky — once Aiko changes she
    stays in that outfit until the next circadian period boundary
    (or until the user manually overrides via the settings panel).

    Like body gestures, outfit changes MUST be tagged when the user
    asks: just describing the change in prose leaves the avatar
    visually unchanged.
    """
    if not capabilities:
        return ""
    has_pajamas = capabilities.get("has_pajamas", False)
    has_pajamas_hooded = capabilities.get("has_pajamas_hooded", False)
    has_day = capabilities.get("has_day_clothes", False)
    if not (has_pajamas or has_pajamas_hooded or has_day):
        return ""
    lines: list[str] = []
    if has_pajamas:
        lines.append(
            "[[outfit:pajamas]] — change into pajamas "
            "(settling in for the night, sticky until morning)"
        )
    if has_pajamas_hooded:
        lines.append(
            "[[outfit:pajamas_hooded]] — same pajamas but with the "
            "sleeping cap on (cold night, extra cozy, hooded variant)"
        )
    if has_day:
        lines.append(
            "[[outfit:day]] — change into day clothes "
            "(getting up / starting the day)"
        )
    return (
        "Outfit changes — when the user asks you to change clothes, "
        "OR when it narratively fits (settling in for bed, getting up "
        "in the morning), emit the matching tag inline in your reply. "
        "Sticky until the next circadian boundary so you don't need "
        "to repeat. NEVER replace with prose like \"changes into "
        "pajamas\" — the tag is what actually swaps the costume.\n"
        "Example:\n"
        "  user: \"change into your casual clothes\"  "
        "->  \"sure thing, [[outfit:day]] better?\"\n"
        + "\n".join(f"- {line}" for line in lines)
        + "\n"
        "[[outfit:X]] is a stage direction — never read the keyword aloud."
    )

# Registry of motion-file stems → human descriptions. The grammar
# only advertises a ``[[motion:X]]`` line when (a) the rig actually
# ships a motion with that stem, AND (b) the stem is in this
# registry. This keeps the LLM from being told about generic
# motion files like ``dh.motion3.json`` (cloth sway) while still
# auto-surfacing user-authored gesture motions the moment they're
# dropped into the rig.
_MOTION_GRAMMAR_DESCRIPTIONS: dict[str, str] = {
    "wave": "[[motion:wave]] — wave hello (greeting)",
    "nod": "[[motion:nod]] — nod yes (agreement)",
    "shake": "[[motion:shake]] — shake head no (disagreement, denial)",
    "bow": "[[motion:bow]] — small bow (formality, gratitude)",
    "shrug": "[[motion:shrug]] — shrug (uncertainty, dismissal)",
    "stretch": "[[motion:stretch]] — stretch (waking up, relief)",
    "dance": "[[motion:dance]] — quick happy dance (excitement)",
}

def _build_motion_grammar_addendum(motion_names: list[str]) -> str:
    """Render the ``[[motion:X]]`` block for every motion the rig ships
    that's also in :data:`_MOTION_GRAMMAR_DESCRIPTIONS`.

    Returns ``""`` when no recognised motions are present (the LLM
    never even hears about ``[[motion:X]]`` in that case).
    """
    if not motion_names:
        return ""
    lowered = {n.lower() for n in motion_names if n}
    available = [
        _MOTION_GRAMMAR_DESCRIPTIONS[stem]
        for stem in _MOTION_GRAMMAR_DESCRIPTIONS
        if stem in lowered
    ]
    if not available:
        return ""
    return (
        "Body motions — full-body animations played by the rig. Emit "
        "the tag inline when the user asks for the gesture, or when "
        "one really fits the moment. Just like body gestures, never "
        "fall back to prose stage directions. The list below is the "
        "ONLY set of valid motion stems; tail-wags, winks, and "
        "ear-flicks are NOT motions — they live under [[overlay:X]] "
        "(see the body-gestures section above). ``[[motion:tail_wag]]`` "
        "does nothing.\n"
        + "\n".join(f"- {line}" for line in available)
        + "\n"
        "[[motion:X]] is a stage direction — never read the keyword aloud."
    )

def _safe_provider(
    provider: Callable[[], str] | None,
    *,
    timing_sink: dict[str, float] | None = None,
    timing_name: str | None = None,
) -> str:
    """Run an inner-life block provider, swallowing exceptions.

    Hot-path safety: a broken provider must NEVER kill the prompt build.
    Returns ``""`` on any failure.

    P2: when ``timing_sink`` and ``timing_name`` are both provided, the
    elapsed wall time of the provider call is added to the sink under
    ``timing_name``. Adding (rather than overwriting) keeps the contract
    well-defined when the same name is somehow timed twice in a build,
    though that shouldn't happen with the current call sites.
    """
    if provider is None:
        return ""
    if timing_sink is not None and timing_name:
        start = time.perf_counter()
        try:
            text = provider()
        except Exception:
            log.debug("inner-life provider raised", exc_info=True)
            text = ""
        finally:
            elapsed_ms = (time.perf_counter() - start) * 1000.0
            timing_sink[timing_name] = (
                timing_sink.get(timing_name, 0.0) + elapsed_ms
            )
        return (text or "").strip()
    try:
        text = provider()
    except Exception:
        log.debug("inner-life provider raised", exc_info=True)
        return ""
    return (text or "").strip()

@contextmanager
def _timed_phase(
    sink: dict[str, float], name: str,
) -> Iterator[None]:
    """Context manager that adds the wall time of the body to ``sink[name]``.

    Used for phases that aren't a simple provider call: the RAG lookup,
    the user-text-aware providers (``knowledge_gaps`` / ``belief_gaps`` /
    ``novelty`` / ``stagnation``), and any fold-up totals. Add semantics
    (rather than overwrite) so a phase wrapped twice in a build remains
    monotonically increasing.
    """
    start = time.perf_counter()
    try:
        yield
    finally:
        elapsed_ms = (time.perf_counter() - start) * 1000.0
        sink[name] = sink.get(name, 0.0) + elapsed_ms

# Reserve a buffer between (estimated tokens used) and (model's context window)
# so we never send a request that bumps against the limit and gets truncated
# server-side.
_SAFETY_TOKENS = 256

_MESSAGE_OVERHEAD = 4  # framing tokens per message (matches token_utils)


def clip_text_to_tokens(
    text: str,
    max_tokens: int,
    *,
    marker: str = "\n\n[... truncated to fit context ...]\n\n",
) -> str:
    """Clip ``text`` so its estimated token count fits ``max_tokens``.

    Preserves the head (75%) and tail (25%) around a truncation marker so both
    the opening framing and the most-recent content survive — the common shape
    for a pasted log / document dump where both ends carry signal. A no-op when
    the text already fits or ``max_tokens <= 0``.
    """
    if max_tokens <= 0 or not text:
        return text
    if estimate_tokens(text) <= max_tokens:
        return text
    budget_chars = max(1, int(max_tokens * chars_per_token()))
    if budget_chars <= len(marker):
        return text[:budget_chars]
    body_chars = budget_chars - len(marker)
    head_chars = int(body_chars * 0.75)
    tail_chars = body_chars - head_chars
    if tail_chars <= 0:
        return text[:head_chars] + marker
    return text[:head_chars] + marker + text[-tail_chars:]

@dataclass(slots=True)
class PromptTelemetry:
    """Accounting for how the next prompt's budget was spent.

    ``prompt_tokens_estimate`` is char-heuristic only; the authoritative
    counts come back from Ollama on the response (``OllamaUsage``). Stored on
    metrics so the web UI can render a context-fill bar before the model has
    even replied.
    """

    context_window: int = 0
    budget_tokens: int = 0
    persona_tokens: int = 0
    ambient_tokens: int = 0
    mood_tokens: int = 0
    rag_tokens: int = 0
    summary_tokens: int = 0
    system_tokens: int = 0
    history_tokens: int = 0
    user_tokens: int = 0
    tool_tokens: int = 0  # set by TurnRunner after the tool pre-pass
    # Token cost of the ``tools=`` schema payload sent on the forced
    # tool-decision pass. Stamped by ``TurnRunner._maybe_run_tool_pass``.
    # 0 on turns where the P14 gate skipped the decision pass (banter),
    # so the widget shows why a tool turn's prompt jumps ~18-19k while a
    # banter turn stays lean. NOT part of ``system_tokens`` — it rides on
    # a *different* LLM call (the decision pass) than the streamed reply.
    tool_schema_tokens: int = 0
    # Largest single Ollama call's prompt-token count this turn (the tool
    # decision pass OR the streaming reply pass — whichever was bigger),
    # stamped by ``TurnRunner`` after both passes run. This is the TRUE
    # context-window occupancy: each pass re-sends its own copy of the
    # system prompt, so the merged (summed) ``usage.prompt_tokens`` used
    # for cost telemetry double-counts that prefix and overstates window
    # pressure ~2x on tool turns. Use THIS for the occupancy bar + the
    # compaction trigger, never the merged sum. 0 falls back to the merged
    # figure (banter turns, where merged == single call).
    context_prompt_tokens: int = 0
    # Phase-2/3/4 inner-life blocks. These are folded into ``system_tokens``
    # for budgeting; the per-block fields exist for the metrics drawer.
    affect_tokens: int = 0
    circadian_tokens: int = 0
    profile_tokens: int = 0
    user_state_tokens: int = 0
    relationship_tokens: int = 0
    arc_tokens: int = 0
    narrative_tokens: int = 0
    agenda_tokens: int = 0
    world_tokens: int = 0
    self_image_tokens: int = 0
    prompt_tokens_estimate: int = 0
    history_messages_kept: int = 0
    history_messages_dropped: int = 0
    summary_active: bool = False
    summary_messages: int = 0
    compaction_triggered: bool = False
    # Listening-window prefetch events (Phase 6 of
    # listening_window_prefetch). Each one is "hit" / "miss" / "skip" so
    # the "turn done:" log line can show at a glance whether the prewarm
    # actually paid off this turn.
    rag_prefetch_event: str = "skip"
    slice_cache_event: str = "skip"
    # P2 (perf backlog): per-phase wall time captured during
    # ``assemble_with_budget`` so a slow turn can be attributed without
    # bisecting each provider by hand. ``provider_ms`` is keyed by the
    # provider name (``"affect"``, ``"novelty"``, ``"stagnation"``, …);
    # entries are only present when a provider was actually wired and
    # ran. ``rag_lookup_ms`` covers the prefetch lookup + live RAG call;
    # ``assemble_ms`` is the total wall time of ``assemble_with_budget``
    # so consumers can compute "everything else" by subtraction.
    provider_ms: dict[str, float] = field(default_factory=dict)
    rag_lookup_ms: float = 0.0
    assemble_ms: float = 0.0
    # P1 (perf backlog): per-turn embed budget. Populated by
    # ``TurnRunner`` from the shared :class:`Embedder`'s thread-local
    # turn counters; covers RAG retrieval, K6/K18 detection, and any
    # other ``embedder.embed`` calls that happened on the turn thread
    # while the turn boundary was active. Async writes from
    # ``MessageIndexer`` run on a different thread and don't pollute
    # these counters.
    embed_calls: int = 0
    embed_ms: float = 0.0
    # P14: tool-pass gate observability. Both are stamped by
    # ``TurnRunner`` after assembly. ``tool_gate_event`` is the compact
    # ``run:<reason>`` / ``skip:<reason>`` decision string ("-" when no
    # tools are registered so the gate never ran); ``tool_pass_ms`` is
    # the wall time of the forced ``chat_with_tools`` decision pass
    # (0.0 when gated off or skipped).
    tool_gate_event: str = "-"
    tool_pass_ms: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "context_window": int(self.context_window),
            "budget_tokens": int(self.budget_tokens),
            "persona_tokens": int(self.persona_tokens),
            "ambient_tokens": int(self.ambient_tokens),
            "mood_tokens": int(self.mood_tokens),
            "rag_tokens": int(self.rag_tokens),
            "summary_tokens": int(self.summary_tokens),
            "system_tokens": int(self.system_tokens),
            "history_tokens": int(self.history_tokens),
            "user_tokens": int(self.user_tokens),
            "tool_tokens": int(self.tool_tokens),
            "tool_schema_tokens": int(self.tool_schema_tokens),
            "context_prompt_tokens": int(self.context_prompt_tokens),
            "affect_tokens": int(self.affect_tokens),
            "circadian_tokens": int(self.circadian_tokens),
            "profile_tokens": int(self.profile_tokens),
            "user_state_tokens": int(self.user_state_tokens),
            "relationship_tokens": int(self.relationship_tokens),
            "arc_tokens": int(self.arc_tokens),
            "narrative_tokens": int(self.narrative_tokens),
            "agenda_tokens": int(self.agenda_tokens),
            "world_tokens": int(self.world_tokens),
            "self_image_tokens": int(self.self_image_tokens),
            "prompt_tokens_estimate": int(self.prompt_tokens_estimate),
            "history_messages_kept": int(self.history_messages_kept),
            "history_messages_dropped": int(self.history_messages_dropped),
            "summary_active": bool(self.summary_active),
            "summary_messages": int(self.summary_messages),
            "compaction_triggered": bool(self.compaction_triggered),
            "rag_prefetch_event": str(self.rag_prefetch_event),
            "slice_cache_event": str(self.slice_cache_event),
            # P2: per-phase wall-time breakdown.
            "provider_ms": {
                str(k): round(float(v), 2) for k, v in self.provider_ms.items()
            },
            "rag_lookup_ms": round(float(self.rag_lookup_ms), 2),
            "assemble_ms": round(float(self.assemble_ms), 2),
            # P1: per-turn embed budget.
            "embed_calls": int(self.embed_calls),
            "embed_ms": round(float(self.embed_ms), 2),
            # P14: tool-pass gate decision + pass cost.
            "tool_gate_event": str(self.tool_gate_event),
            "tool_pass_ms": round(float(self.tool_pass_ms), 2),
        }

@dataclass(slots=True)
class _StaticSlices:
    """Pre-built prompt parts that don't depend on ``user_text`` or RAG.

    Produced by :meth:`PromptAssembler.prebuild_static_slices` during the
    listening window and consumed by :meth:`assemble_with_budget` at
    commit. Reuse is gated by ``cache_key`` — when any of (session, history
    watermark, persona/self-image mtime, last reaction, recent_window) has
    moved, the cache is treated as invalid and the assembler falls through
    to the standard build path.

    ``ambient_block`` is the only field that can be a few minutes stale
    (time-of-day band) — that drift is acceptable since the band only
    crosses every few hours and the user wouldn't notice the difference
    between "morning" and "midday" in a 4 s phrase.
    """

    cache_key: tuple
    persona: str
    self_image_block: str
    summary_row: SummaryRow | None
    already_summarized: int
    thread_note: str
    history_msgs: list[MessageRow]
    ambient: str
    mood_hint: str
    affect_block: str
    circadian_block: str
    profile_block: str
    user_state_block: str
    relationship_block: str
    arc_block: str
    agenda_block: str
    goals_block: str
    interest_map_block: str
    built_at: float


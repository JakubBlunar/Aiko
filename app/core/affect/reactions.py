"""Reaction vocabulary + semantic-neighbour fallback resolver.

Lives outside ``avatar_profile`` so the cadence/affect/text layers can
import the canonical reaction names without pulling in the avatar
loader. Previously co-located with ``persona_manager`` (deleted as
part of the Alexia bundling work).

The canonical 27-name set is what the affect/cadence pipelines emit
via ``[[reaction:X]]`` tags. ``_REACTION_SYNONYMS`` is fuzzy-match
material for personas where we have to *guess* a sensible default
mapping from expression filenames. ``_REACTION_NEIGHBOURS`` is the
fall-back chain used by :func:`resolve_reaction` when the loaded
avatar lacks a direct mapping for the requested reaction.
"""
from __future__ import annotations


# Reactions Aiko can emit. The full 27-name set covers every label
# the affect/cadence pipeline produces; if we shrink this we get
# silent reaction drops. The Phase 5 expression overhaul added five
# entries (``cry``, ``confused``, ``embarrassed``, ``nervous``,
# ``defiant``) on top of the original 22 — see
# ``docs/personality-backlog/shipped.md`` §B4.
REACTIONS: tuple[str, ...] = (
    "neutral",
    "cheerful",
    "excited",
    "enthusiastic",
    "amused",
    "playful",
    "surprised",
    "curious",
    "friendly",
    "warm",
    "tender",
    "thoughtful",
    "wistful",
    "calm",
    "serious",
    "concerned",
    "sad",
    "melancholy",
    # ``cry`` is a more intense form of ``sad`` — used when the user
    # shares deeply moving / distressing news, or when Aiko is overwhelmed
    # in roleplay. Distinct from ``sad`` so the avatar can show a more
    # pronounced cry overlay (Alexia: ``bbt``, Param60) rather than the
    # quieter "tear" overlay (Alexia: ``k``, Param59).
    "cry",
    "tired",
    "gentle",
    "angry",
    "frustrated",
    # ``confused`` covers the dazed / "wait, what?" moment that earlier
    # collapsed into ``curious``. Distinct from curiosity because the
    # avatar visual is a dizzy / spiral-eyes overlay (Alexia: ``y``,
    # Param56 = Dizzy), not the cocked head + raised eyebrow of a
    # curious look. Falls back through ``curious`` → ``thoughtful`` on
    # rigs without a confusion overlay.
    "confused",
    # Phase 5 (expression overhaul): three new shades the audit
    # surfaced that previously collapsed into broader neighbours.
    #
    # ``embarrassed`` = a soft blush + downward-tilted smile (Alexia:
    # ``lh`` shy + ``Param58`` blush overlay). Previously folded
    # into ``warm`` / ``tender``, but the visual difference is
    # real: ``warm`` is open and outward; ``embarrassed`` is
    # hunched inward. Falls back to ``warm`` → ``tender`` →
    # ``cheerful`` on rigs without a blush axis.
    "embarrassed",
    # ``nervous`` = sweat-drop tension + slightly worried mouth
    # (Alexia: ``yfmz`` mouth-anxiety + ``Param44`` sweat overlay).
    # Distinct from ``concerned`` because ``concerned`` is *about
    # the user* (empathetic worry), ``nervous`` is *about herself*
    # (self-conscious tension). Falls back to ``concerned`` →
    # ``serious`` → ``thoughtful``.
    "nervous",
    # ``defiant`` = pouty / refusing frown + slight tilt up
    # (Alexia: ``mj`` pout-meets-stubborn). Previously folded into
    # ``frustrated`` or ``angry`` but it's a softer, more *playful*
    # refusal — the energy is "hmph, no" rather than "I am
    # furious". Falls back to ``frustrated`` → ``angry`` →
    # ``serious``.
    "defiant",
    # K58 (emotion speech weighting): four shades minted so the K57
    # directed-emotion episodes have a register to land in. The
    # taxonomy previously had no way to *show* smugness or a sulk —
    # a correctly-triggered "miffed at you" episode had to borrow
    # ``frustrated`` (too hot) or ``neutral`` (invisible).
    #
    # ``smug`` = the earned "I was right" grin — pleased with
    # herself, pointed at the user, clearly playful. Distinct from
    # ``amused`` (which is about the *situation*); smug is about HER
    # being right. Falls back to ``amused`` → ``playful``.
    "smug",
    # ``pouty`` = the lip-out "hmph" — lighter than ``defiant``
    # (which refuses), warmer than ``frustrated`` (which simmers).
    # The native face of a low-intensity miffed episode. Falls back
    # to ``defiant`` → ``frustrated``.
    "pouty",
    # ``sulky`` = pouty with the energy drained out — withdrawn
    # displeasure, shorter sentences, looking away. The K57 miffed
    # register at higher intensity. Falls back to ``pouty`` →
    # ``defiant``. Deliberately NOT chained into the sad family —
    # a sulk with tear streaks reads as manipulation.
    "sulky",
    # ``mischievous`` = the scheming grin — about to tease, plotting
    # a callback, the K59 ledger-collection face. Sharper than
    # ``playful`` (which is open), more deliberate than ``amused``.
    # Falls back to ``playful`` → ``amused``.
    "mischievous",
)


# Synonyms the auto-default-mapping fuzzy match looks for in expression
# filenames. Used by :class:`AvatarProfile` when a model doesn't ship
# an explicit reaction mapping.
_REACTION_SYNONYMS: dict[str, tuple[str, ...]] = {
    "neutral": ("normal", "default", "neutral", "idle"),
    "cheerful": ("smile", "happy", "joy", "cheer", "cheerful", "grin"),
    "excited": ("excite", "wow", "yay", "shine", "sparkle"),
    "enthusiastic": ("excite", "shine", "sparkle", "yay", "fun"),
    "amused": ("smile", "grin", "laugh", "amused", "smirk"),
    "playful": ("playful", "wink", "tongue", "smirk", "fun"),
    "surprised": ("surprise", "shock", "wow", "gasp"),
    "curious": ("curious", "wonder", "interest", "head"),
    "friendly": ("friendly", "smile", "wink", "warm"),
    "warm": ("warm", "soft", "smile", "blush"),
    "tender": ("tender", "soft", "blush", "warm"),
    "thoughtful": ("thoughtful", "thinking", "ponder", "look"),
    "wistful": ("wistful", "sigh", "soft", "look"),
    "calm": ("calm", "relax", "peace", "soft"),
    "serious": ("serious", "stern", "thinking", "frown"),
    "concerned": ("concern", "worry", "sad", "frown"),
    "sad": ("sad", "cry", "tear", "unhappy", "sob"),
    "melancholy": ("sad", "tear", "down", "blue"),
    "cry": ("cry", "crying", "weep", "wail", "sob", "tears", "bawl"),
    "tired": ("tired", "sleep", "yawn", "weary"),
    "confused": ("confused", "puzzled", "dizzy", "huh", "lost", "spiral"),
    "gentle": ("gentle", "soft", "kind", "warm"),
    "angry": ("angry", "anger", "mad", "rage", "pout"),
    "frustrated": ("frustrated", "annoy", "pout", "angry"),
    # Phase 5 (expression overhaul): three new shades the visual
    # audit surfaced. Synonyms cover the obvious filename stems plus
    # English near-misses the auto-mapper might trip over on a future
    # rig.
    "embarrassed": (
        "embarrassed", "blush", "shy", "lh", "flustered", "bashful",
    ),
    "nervous": (
        "nervous", "sweat", "anxious", "anxiety", "tense", "yfmz",
    ),
    "defiant": (
        "defiant", "pout", "hmph", "stubborn", "sulk", "refuse",
    ),
    # K58: the four directed-emotion shades.
    "smug": ("smug", "smirk", "grin", "proud"),
    "pouty": ("pouty", "pout", "hmph", "puff"),
    "sulky": ("sulky", "sulk", "pout", "moody"),
    "mischievous": (
        "mischievous", "mischief", "smirk", "grin", "scheming", "tease",
    ),
}


# Semantic-neighbour fallbacks. Used when a model's ``reaction_mapping``
# does not have an entry for a reaction the affect/cadence pipeline
# emitted. We walk the candidate list in order and return the first
# one that *is* mapped, so every reaction triggers *some* visual
# change even on minimal-expression models.
#
# Per-model overrides remain authoritative — this only fires when the
# requested reaction is missing from the explicit mapping.
#
# Design rule: NON-sad reactions must NEVER chain through ``concerned``
# / ``sad`` / ``melancholy`` / ``cry``. On rigs where those resolve to
# tear-streak overlays (e.g. Alexia's ``k`` = Param59), a single
# ``[[reaction:thoughtful]]`` emit (or the filler-injector's default
# "thoughtful" carry-over on a fresh-boot turn) would silently flip
# Aiko into "visibly crying" with no narrative justification. Stay
# inside the same emotional family — body-language layers carry the
# subtle texture instead. The sad family (``sad`` / ``melancholy`` /
# ``wistful`` / ``concerned`` / ``tired`` / ``cry``) chains among
# itself; that's where tear-streak overlays belong.
# The frontend mirror lives in
# :file:`web/src/live2d/channels/ExpressionChannel.ts` —
# ``_REACTION_NEIGHBOURS``; keep both in lockstep.
_REACTION_NEIGHBOURS: dict[str, tuple[str, ...]] = {
    "amused":      ("cheerful", "playful", "friendly", "warm", "neutral"),
    "playful":     ("amused", "cheerful", "excited", "friendly", "warm"),
    "enthusiastic": ("excited", "cheerful", "playful", "friendly"),
    "curious":     ("thoughtful", "surprised", "friendly", "neutral"),
    "tender":      ("warm", "gentle", "friendly", "calm", "neutral"),
    "warm":        ("friendly", "gentle", "tender", "cheerful", "neutral"),
    # ``thoughtful`` is a contemplative beat; previously chained
    # through ``concerned`` which on Alexia paints tears. Drop it —
    # body-language carries the thinking texture instead.
    "thoughtful":  ("serious", "calm", "neutral"),
    "wistful":     ("sad", "melancholy", "thoughtful", "calm", "gentle"),
    "concerned":   ("serious", "sad", "thoughtful", "neutral"),
    "melancholy":  ("sad", "wistful", "tired", "calm", "neutral"),
    "tired":       ("calm", "melancholy", "neutral", "sad"),
    # Frustration / anger are anger-leaning, not sadness-leaning.
    # Chain among themselves and serious, never through ``concerned``.
    "frustrated":  ("angry", "serious", "neutral"),
    "gentle":      ("warm", "calm", "friendly", "tender", "neutral"),
    "friendly":    ("warm", "cheerful", "neutral", "calm"),
    "calm":        ("neutral", "thoughtful", "gentle", "warm"),
    # ``serious`` was the second neighbour-chain crybug entrypoint.
    # Same fix: drop ``concerned``.
    "serious":     ("thoughtful", "neutral"),
    "surprised":   ("excited", "curious", "amused", "neutral"),
    "cheerful":    ("amused", "friendly", "warm", "playful", "neutral"),
    "excited":     ("enthusiastic", "cheerful", "playful", "surprised", "neutral"),
    "sad":         ("melancholy", "wistful", "concerned", "neutral"),
    # ``cry`` falls back to ``sad`` first so models without a distinct
    # cry overlay still produce a sad-leaning visual.
    "cry":         ("sad", "melancholy", "wistful", "concerned", "neutral"),
    # ``confused`` walks toward curiosity / pondering first so rigs
    # without a dizzy overlay still produce a "thinking" visual rather
    # than collapsing to neutral.
    "confused":    ("curious", "thoughtful", "surprised", "neutral"),
    "angry":       ("frustrated", "serious", "neutral"),
    # Phase 5 (expression overhaul) neighbour chains. See the module
    # docstrings on the REACTIONS entries for the rationale behind
    # the chosen fall-back order — the goal is that a minimal-rig
    # avatar without the dedicated overlay still produces something
    # visually plausible (e.g. ``embarrassed`` → ``warm`` smile is
    # a much better degrade than ``embarrassed`` → ``neutral``).
    "embarrassed": ("warm", "tender", "cheerful", "friendly", "neutral"),
    "nervous":     ("concerned", "serious", "thoughtful", "neutral"),
    "defiant":     ("frustrated", "angry", "serious", "neutral"),
    # K58 chains. ``smug`` / ``mischievous`` stay in the playful
    # family; ``pouty`` / ``sulky`` stay in the defiant/frustrated
    # family and must NEVER chain through the sad family (a sulk
    # rendering tear streaks reads as a guilt-trip — exactly the
    # tonal failure K57's design forbids).
    "smug":        ("amused", "playful", "cheerful", "neutral"),
    "pouty":       ("defiant", "frustrated", "serious", "neutral"),
    "sulky":       ("pouty", "defiant", "frustrated", "neutral"),
    "mischievous": ("playful", "amused", "cheerful", "excited", "neutral"),
    "neutral":     ("calm", "friendly", "warm"),
}


def resolve_reaction(
    reaction: str | None,
    reaction_mapping: dict[str, str],
) -> str | None:
    """Pick the best expression name for a reaction.

    Returns the model's mapping for ``reaction`` if present, else
    walks ``_REACTION_NEIGHBOURS`` for the closest semantic neighbour
    that *is* mapped, else ``None``.
    """
    if not reaction or not reaction_mapping:
        return None
    key = reaction.strip().lower()
    direct = reaction_mapping.get(key)
    if direct:
        return direct
    for fallback in _REACTION_NEIGHBOURS.get(key, ()):
        mapped = reaction_mapping.get(fallback)
        if mapped:
            return mapped
    return None


def split_reaction_stack(expression: str | None) -> list[str]:
    """Split a stacked reaction / overlay name into ordered components.

    Accepts the verbatim string captured from a
    ``[[reaction:A+B]]`` or ``[[overlay:A+B+C]]`` tag (or a single
    ``[[reaction:A]]`` name with no ``+``). Returns the component
    names lower-cased and de-duplicated while preserving first-seen
    order. Empty / falsy input returns ``[]``.

    Used by the dispatch boundary in :mod:`app.core.session.turn_runner` /
    :mod:`app.core.session.avatar_mixin` to decide how many overlay
    pulses to emit for a single tag, and by
    :func:`resolve_reaction_stack` below for the expression-name
    resolution side.

    Whitespace around component names is silently trimmed so a model
    that emits ``A + B`` instead of ``A+B`` still parses cleanly —
    the regex in :mod:`response_text_service` doesn't allow spaces,
    so this is more of a defensive nicety than a real concern.
    """
    if not expression:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for raw in str(expression).split("+"):
        token = raw.strip().lower()
        if not token or token in seen:
            continue
        seen.add(token)
        out.append(token)
    return out


def resolve_reaction_stack(
    expression: str | None,
    reaction_mapping: dict[str, str],
) -> list[str]:
    """Resolve a stacked reaction expression to a list of expression names.

    Walks :func:`split_reaction_stack` first, then runs each
    component through :func:`resolve_reaction` against the loaded
    rig's mapping. Components that resolve to ``None`` (no direct
    mapping and no neighbour fallback hit) are dropped; the remaining
    expression names are returned in declaration order, deduped.

    On a stack of size 1 this is identical to wrapping
    :func:`resolve_reaction` in a single-element list, so callers
    can use this uniformly regardless of whether the tag was a
    plain or a stacked reaction.

    Designed for the Phase 3 LLM grammar
    (``[[reaction:cheerful+blush]]`` / ``[[overlay:sweat+question]]``)
    where the rig dispatcher wants the resolved expression-name list
    to plumb into the channel layer — see
    :class:`web/src/live2d/channels/ExpressionChannel.ts` for the
    consuming side.
    """
    components = split_reaction_stack(expression)
    if not components:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for component in components:
        resolved = resolve_reaction(component, reaction_mapping)
        if not resolved or resolved in seen:
            continue
        seen.add(resolved)
        out.append(resolved)
    return out


__all__ = [
    "REACTIONS",
    "resolve_reaction",
    "resolve_reaction_stack",
    "split_reaction_stack",
    "_REACTION_SYNONYMS",
    "_REACTION_NEIGHBOURS",
]

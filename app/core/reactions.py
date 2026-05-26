"""Reaction vocabulary + semantic-neighbour fallback resolver.

Lives outside ``avatar_profile`` so the cadence/affect/text layers can
import the canonical reaction names without pulling in the avatar
loader. Previously co-located with ``persona_manager`` (deleted as
part of the Alexia bundling work).

The canonical 22-name set is what the affect/cadence pipelines emit
via ``[[reaction:X]]`` tags. ``_REACTION_SYNONYMS`` is fuzzy-match
material for personas where we have to *guess* a sensible default
mapping from expression filenames. ``_REACTION_NEIGHBOURS`` is the
fall-back chain used by :func:`resolve_reaction` when the loaded
avatar lacks a direct mapping for the requested reaction.
"""
from __future__ import annotations


# Reactions Aiko can emit. The full 22-name set covers every label
# the affect/cadence pipeline produces; if we shrink this we get
# silent reaction drops.
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
    "gentle": ("gentle", "soft", "kind", "warm"),
    "angry": ("angry", "anger", "mad", "rage", "pout"),
    "frustrated": ("frustrated", "annoy", "pout", "angry"),
}


# Semantic-neighbour fallbacks. Used when a model's ``reaction_mapping``
# does not have an entry for a reaction the affect/cadence pipeline
# emitted. We walk the candidate list in order and return the first
# one that *is* mapped, so every reaction triggers *some* visual
# change even on minimal-expression models.
#
# Per-model overrides remain authoritative — this only fires when the
# requested reaction is missing from the explicit mapping.
_REACTION_NEIGHBOURS: dict[str, tuple[str, ...]] = {
    "amused":      ("cheerful", "playful", "friendly", "warm", "neutral"),
    "playful":     ("amused", "cheerful", "excited", "friendly", "warm"),
    "enthusiastic": ("excited", "cheerful", "playful", "friendly"),
    "curious":     ("thoughtful", "surprised", "friendly", "neutral"),
    "tender":      ("warm", "gentle", "friendly", "calm", "neutral"),
    "warm":        ("friendly", "gentle", "tender", "cheerful", "neutral"),
    "thoughtful":  ("serious", "calm", "concerned", "neutral"),
    "wistful":     ("sad", "melancholy", "thoughtful", "calm", "gentle"),
    "concerned":   ("serious", "sad", "thoughtful", "neutral"),
    "melancholy":  ("sad", "wistful", "tired", "calm", "neutral"),
    "tired":       ("calm", "melancholy", "neutral", "sad"),
    "frustrated":  ("angry", "concerned", "serious", "neutral"),
    "gentle":      ("warm", "calm", "friendly", "tender", "neutral"),
    "friendly":    ("warm", "cheerful", "neutral", "calm"),
    "calm":        ("neutral", "thoughtful", "gentle", "warm"),
    "serious":     ("thoughtful", "concerned", "neutral"),
    "surprised":   ("excited", "curious", "amused", "neutral"),
    "cheerful":    ("amused", "friendly", "warm", "playful", "neutral"),
    "excited":     ("enthusiastic", "cheerful", "playful", "surprised", "neutral"),
    "sad":         ("melancholy", "wistful", "concerned", "neutral"),
    # ``cry`` falls back to ``sad`` first so models without a distinct
    # cry overlay still produce a sad-leaning visual.
    "cry":         ("sad", "melancholy", "wistful", "concerned", "neutral"),
    "angry":       ("frustrated", "serious", "concerned", "neutral"),
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


__all__ = [
    "REACTIONS",
    "resolve_reaction",
    "_REACTION_SYNONYMS",
    "_REACTION_NEIGHBOURS",
]

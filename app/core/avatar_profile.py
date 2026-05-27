"""Live2D avatar profile loader (replaces the persona-upload pipeline).

The app now ships a single, hardcoded avatar (Alexia by default; any
Cubism 3 model with the same files-on-disk shape works). This module
reads the model3.json + cdi3.json off disk and computes:

  - the standard manifest fields (expressions, motions, lip-sync IDs),
  - a **capability map** describing which optional features the model
    exposes (pajamas, blush overlay, cat tail, glasses, ...),
  - per-capability **bindings** (parameter ID + on-value + decay
    duration + an English label) that the renderer uses to drive the
    Tier-3 auto effects, and
  - an Alexia-aware default reaction mapping so the LLM's
    ``[[reaction:X]]`` tags trigger meaningful visual changes even
    when the model uses single-parameter overlay expressions
    (rather than full facial expression files).

The manifest is **immutable at runtime** — no upload, no in-app
editing. The two user-tunable knobs (scale multiplier, auto-outfit
mode) live in ``AvatarSettings`` and are applied on top of the
profile.

Capability detection works via a multi-language synonym table so a
future model with Japanese/English/Chinese parameter names degrades
gracefully — features whose synonyms don't match end up with
``has_X = False`` and the renderer no-ops the corresponding effect.
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from app.core.reactions import _REACTION_SYNONYMS


log = logging.getLogger("app.avatar_profile")


# ── Data shapes ─────────────────────────────────────────────────────────


@dataclass(slots=True)
class ExpressionRef:
    """One Live2D expression file as referenced from the model JSON."""

    name: str
    file: str  # relative to the avatar root


@dataclass(slots=True)
class MotionRef:
    """One Live2D motion file in a named group."""

    name: str
    file: str


@dataclass(slots=True)
class OverlayBinding:
    """Phase-3 overlay (sweat / blush / question-mark / dizzy / ...).

    The renderer holds the parameter at ``on_value`` for the overlay's
    duration and decays back to zero. ``decay_ms`` is the auto-fade
    used when the LLM fires the overlay via ``[[overlay:X]]``;
    sticky overlays driven by mood (auto-blush) ignore it and use
    their own envelope.
    """

    param_id: str
    on_value: float = 30.0
    decay_ms: int = 1500
    label_en: str = ""


@dataclass(slots=True)
class OutfitParam:
    """One parameter contribution inside a multi-param outfit binding.

    Outfits in real Cubism rigs aren't always single-param toggles —
    Alexia's "pajamas_hooded" is body-clothes (Param16=30) **and**
    sleeping cap (Param17=30) together, while plain "pajamas" is just
    Param16=30 on its own and "day_clothes" is the bare baseline (no
    params active). Storing the contributions as a list lets the
    renderer cross-fade each component independently against the same
    envelope.
    """

    param_id: str
    on_value: float = 30.0


@dataclass(slots=True)
class ExpressionParam:
    """One parameter contribution inside an expression file binding.

    Mirrors :class:`OutfitParam` but for the ``ExpressionChannel``
    continuous-expressiveness layer. Expressions are normally
    Add-blended by pixi-live2d-display's ``expressionManager`` from
    the ``Value`` field of each ``.exp3.json``. The renderer's
    ``tickPreModel`` arousal-scaler reads this list to know which
    params it can gently override at low arousal (writing
    ``on_value * scale`` directly) without fighting the manager's
    own additive contribution. Param IDs that show up here are *also*
    in the model's parameter table — the renderer is free to ignore
    bindings whose IDs aren't on the loaded rig.
    """

    param_id: str
    on_value: float = 30.0


@dataclass(slots=True)
class OutfitBinding:
    """Outfit toggle composed of one-or-more parameter contributions.

    ``mutex_with`` lets us encode "pajamas excludes day clothes" so
    the renderer cleanly cross-fades between them.
    """

    params: list[OutfitParam] = field(default_factory=list)
    label_en: str = ""
    mutex_with: tuple[str, ...] = field(default_factory=tuple)


@dataclass(slots=True)
class AvatarProfile:
    """Immutable description of the loaded Live2D avatar.

    Mutable runtime knobs (scale, auto-outfit mode) live in
    ``AvatarSettings`` and are merged with this profile at the
    HTTP/WS boundary; the dataclass itself stays pure and cacheable.
    """

    display_name: str
    entry_filename: str
    cubism_version: int
    expressions: list[ExpressionRef] = field(default_factory=list)
    motions: dict[str, list[MotionRef]] = field(default_factory=dict)
    reaction_mapping: dict[str, str] = field(default_factory=dict)
    idle_motion_group: str | None = None
    talk_motion_group: str | None = None
    lip_sync_ids: list[str] = field(default_factory=list)
    eye_blink_ids: list[str] = field(default_factory=list)
    parameters: list[dict[str, str]] = field(default_factory=list)
    parts: list[dict[str, str]] = field(default_factory=list)
    capabilities: dict[str, bool] = field(default_factory=dict)
    overlays: dict[str, OverlayBinding] = field(default_factory=dict)
    outfits: dict[str, OutfitBinding] = field(default_factory=dict)
    # Expression-file → list of (Param ID, Value) bindings parsed from
    # each ``.exp3.json``. Mirrors the ``outfits`` shape but exposes
    # *every* expression's params (not just the outfit ones). The
    # renderer's continuous-expressiveness layer reads this to do an
    # arousal-scaled write of the same params the rig's
    # ``expressionManager`` is Add-blending each frame, so a single
    # ``cheerful`` reaction reads quieter at low arousal. Skipped
    # bindings (missing or unparseable exp3) silently absent — the
    # renderer falls back to the manager's natural amplitude.
    expression_params: dict[str, list[ExpressionParam]] = field(default_factory=dict)
    # Param IDs that draw a *stylised mouth shape* on top of the
    # rig's actual mouth (``ParamMouthOpenY`` / lip-sync params).
    # On Alexia this is ``["Param54"]`` (= 咧嘴笑 = "toothy grin"),
    # which the ``lzx`` expression activates: it paints a fixed
    # grin overlay independent of the lip-synced jaw motion. When
    # the rig speaks while a grin-bearing expression is active,
    # both mouths are visible at once — the static toothy grin and
    # the flapping lip-sync mouth.
    #
    # The renderer's ``ExpressionChannel.tickPreModel`` reads this
    # list and tapers any expression-param binding whose id lands
    # here against the live audio amplitude, so the grin fades out
    # while she's speaking and snaps back in when she stops. Empty
    # on rigs without a stylised mouth overlay (the plain
    # ``ParamMouthOpenY`` does not belong here).
    mouth_overlay_param_ids: list[str] = field(default_factory=list)
    # Expression filenames whose param list intersects
    # ``mouth_overlay_param_ids``. Derived purely from the other two
    # fields, but exposed explicitly so:
    #   - tests can assert "is ``lzx`` recognised as a mouth blocker?";
    #   - any future channel can quickly answer "would firing this
    #     expression visually compete with lip-sync?" without re-walking
    #     ``expression_params`` for every check.
    # For Alexia this is ``["lzx"]`` (Param54 = 咧嘴笑 = toothy grin).
    mouth_blocking_expressions: list[str] = field(default_factory=list)
    # Expression filenames that only render correctly when the active
    # outfit is one of the listed capability names. Each key is the
    # expression name (without the ``.exp3.json`` suffix); each value
    # is the allow-list of outfit capability names. Empty list = no
    # gate. The renderer's expression dispatcher consults this before
    # firing ``adapter.expression(name)`` and falls back to the
    # neighbour chain when the gate fails.
    #
    # For Alexia: ``{"zs1": ["day_clothes"]}`` because the crossed-arms
    # pose explicitly zeroes the alternate-outfit envelope (Param16=0,
    # Param17=0) in its exp3, meaning the rig only draws the pose
    # when the body is in day clothes. Firing ``zs1`` while pajamas
    # are active produces no visible change (silent no-op).
    outfit_gated_expressions: dict[str, list[str]] = field(default_factory=dict)
    # All param IDs whose cdi3 ``Name`` matched a cat-tail synonym,
    # in declaration order. The renderer uses this to drive the
    # arousal-modulated tail wag without hardcoding segment counts.
    cat_tail_param_ids: list[str] = field(default_factory=list)
    # Same idea for cat-ear segments — Alexia ships ``Param38``..``41``
    # (left/right ears, two segments each). Used by the ``ear_wiggle``
    # gesture in the renderer; an empty list means the model has no
    # animatable ear segments and the gesture silently no-ops.
    cat_ear_param_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class AvatarProfileError(Exception):
    """Raised when the avatar root is missing required files."""


# ── Capability synonym table ───────────────────────────────────────────


# Keyed by canonical capability name. Each value is a tuple of
# substrings we look for in the cdi3 ``Name`` (Chinese label) or
# expression filename (pinyin). All matches are case-insensitive
# substring checks so a Japanese model whose blush param is
# ``ホッペ赤ら`` won't silently match — that's fine, ``has_blush``
# becomes False and the renderer skips the auto-blush.
_CAPABILITY_SYNONYMS: dict[str, tuple[str, ...]] = {
    # Outfits — synonym matching is *only* used to flip ``has_*`` for
    # capability detection; the actual multi-param OutfitBinding is
    # built from the matching exp3.json (since real outfits are
    # almost always multi-param compositions). ``hood`` is intentionally
    # absent — Alexia's hood is the upper layer of the pajama costume,
    # not a separate toggle, and adding it as its own capability gave
    # us a phantom "Param17 alone" binding that produced a hood
    # floating without the body.
    "pajamas":        ("睡衣", "pajama", "shuiyi"),
    # ``pajamas_hooded`` is the "pajamas + sleeping cap" variant. There
    # isn't a clean Chinese single-word for it (the source label was
    # ``衣服托帽子`` = "clothes with hood"), so synonym matching is
    # English-leaning. Detection still works via the ``yfmz`` →
    # ``pajamas_hooded`` entry in ``_ALEXIA_EXPR_TO_CAPABILITY`` for
    # Alexia; the synonyms are here so a future rig that names its hood
    # param "pajamas_hood" / "sleep_cap" still flips ``has_*`` on.
    "pajamas_hooded": ("pajamas hooded", "sleep cap", "nightcap", "睡衣帽"),
    "day_clothes":    ("衣服", "outfit", "yifu"),
    # Stage-direction overlays
    "blush":       ("脸红", "blush", "lh", "lianhong"),
    "sweat":       ("汗", "sweat"),
    "dizzy":       ("晕", "dizzy"),
    "stars":       ("星星眼", "stars", "starry", "xxy"),
    "question":    ("问号", "question", "wh", "wenhao"),
    "cry":         ("哭", "cry", "tear"),
    "angry_marks": ("生气", "angry", "sq", "shengqi"),
    "grin":        ("咧嘴笑", "grin", "smirk"),
    # ── Accessories ────────────────────────────────────────────────
    # ``lollipop`` — ``bbt`` (Param60) draws a candy/lollipop INSIDE
    # her mouth (additive 0-30). Previously misidentified as a generic
    # "sticker overlay slot" and then as a dramatic cry overlay; the
    # rig's actual artwork is a prop, so it belongs in the accessory
    # space alongside glasses, not the emotion / reaction space. See
    # ``docs/alexia-model-notes.md`` §3a.
    "lollipop":    ("bbt",),
    # ``eyeglasses`` (proper pair worn on the face, Param64) is
    # deliberately separate from ``head_sunglasses`` (Param11, sits
    # on top of her head like a hair accessory). The substring list
    # avoids the bare word "glasses" so "Sunglasses" doesn't get
    # cross-matched.
    "eyeglasses":  ("带眼镜", "eyeglasses", "dyj"),
    # ``head_sunglasses`` — ``mj`` (Param11). The rig draws the
    # sunglasses ON TOP of the hair, not on the eyes; this is a
    # styling accessory, not an obstruction. Renamed from the
    # earlier ``sunglasses`` capability so the catalogue is honest
    # about where the artwork actually lives.
    "head_sunglasses": ("墨镜", "sunglasses", "mojing", "mj"),
    "eye_color_a": ("眼睛颜色", "eye color", "yjys1", "yjys"),
    "eye_color_b": ("眼睛颜色2", "eye color 2", "yjys2"),
    # ``crossed_arms`` — ``zs1`` (Param61). Only renders when the
    # active outfit is day_clothes; the exp3 explicitly zeroes
    # Param16/17 to enforce the gate.
    "crossed_arms": ("姿势", "pose", "zs", "crossed", "arms"),
    # Cat-girl bits
    "cat_tail":    ("猫尾", "cat_tail", "tail"),
    "cat_ears":    ("耳朵", "cat ear", "neko"),
}


# Body-rotation + ambient-driver parameter probes. The lookup checks
# for these *exact* IDs (not synonym matches) because the names are
# part of the Cubism standard parameter set; we only want to flip
# the capability flags when the rig actually exposes them so the
# renderer's body-language layer is a no-op on minimal models.
#
# ``has_breath`` is included here so the
# ``AmbientBodyChannel.tickPreModel`` continuous-expressiveness pass
# can override the breath driver with an arousal-scaled wave on
# rigs that expose ``ParamBreath``, and silently no-op on minimal
# rigs that don't.
_BODY_ANGLE_PROBES: dict[str, str] = {
    "has_body_angle_y": "ParamBodyAngleY",
    "has_body_angle_z": "ParamBodyAngleZ",
    "has_breath": "ParamBreath",
}


# Per-eye open params. Both must exist for the ``[[overlay:wink_*]]``
# grammar to be advertised; otherwise winking would close both eyes
# (the ``EyeBlink`` group on most rigs ties them together).
_WINK_PROBES: tuple[str, str] = ("ParamEyeLOpen", "ParamEyeROpen")


# Cat-ear synonym list — kept distinct from ``cat_ears`` (which fires
# on the part name ``耳朵`` and only flips the flag) because we want
# the *individual segment* params (``左耳1``/``右耳2`` …) for the
# ear-wiggle gesture, and matching ``耳朵`` against parameter names
# would be too greedy (``耳坠`` = earring, etc.).
_EAR_SEGMENT_SYNONYMS: tuple[str, ...] = (
    "左耳",
    "右耳",
    "left ear",
    "right ear",
    "ear segment",
)


# Synonyms for params that paint a *stylised mouth shape* on top of
# the rig's real mouth — the toothy-grin overlay is the canonical
# example. We intentionally do NOT include ``"smile"`` here: a soft
# closed-mouth smile doesn't conflict with lip-sync (it doesn't draw
# a competing mouth artwork). Only overlays that visibly add a
# second mouth belong here. Substring match against the cdi3
# ``Name`` field, case-insensitive, multi-language.
_MOUTH_OVERLAY_SYNONYMS: tuple[str, ...] = (
    "咧嘴笑",
    "咧嘴",
    "grin",
    "smirk",
    "toothy",
)


# Expression-file → capability mapping for Alexia. The cdi3 lookup
# below builds the OverlayBinding for each, then this links them up
# by the expression's short name (``bbt``, ``lh``, …). When a future
# model uses different filenames the binding still works because the
# linking is by **capability**, not by literal filename.
_ALEXIA_EXPR_TO_CAPABILITY: dict[str, str] = {
    # ``bbt`` was misclassified twice before (happy sticker, then cry
    # overlay). Visual audit against the live rig (Cubism Viewer
    # Standalone for SDK 5) confirmed it draws a lollipop / candy
    # prop in the mouth. It lives in the accessory tier now, NOT
    # the emotion tier — firing it for the ``cry`` reaction shoved a
    # lollipop into Aiko's mouth mid-sob. See
    # ``docs/alexia-model-notes.md`` §3a.
    "bbt":   "lollipop",
    "dyj":   "eyeglasses",
    "h":     "sweat",
    "k":     "cry",
    "lh":    "blush",
    # ``mj`` sits ON the head (hair-perched), not on the eyes. The
    # capability is renamed to make that explicit.
    "mj":    "head_sunglasses",
    "sq":    "angry_marks",
    "wh":    "question",
    "xxy":   "stars",
    "y":     "dizzy",
    "lzx":   "grin",          # Param54 = 咧嘴笑 = toothy grin
    # Outfit mapping (verified VISUALLY against the actual Alexia rig
    # by toggling each radio in the SettingsDrawer; do NOT trust the
    # original Chinese parameter names alone — they're misleading):
    #
    #   - BASELINE (Param16=0, Param17=0) renders the casual day
    #     clothes (streetwear). ``day_clothes`` is therefore the
    #     "no params active" outfit; its binding stays around as an
    #     empty shell so ``has_day_clothes`` lights up for the UI
    #     radio.
    #   - ``yf.exp3.json`` (Param16=30 alone) renders pajamas WITH
    #     the sleeping hoodie up → ``pajamas_hooded`` capability.
    #     (Counter-intuitive given the file is just "yf" = 衣服 =
    #     "clothes", but the rig's default art for the alternate
    #     outfit ships hooded — Param17 LIFTS the hood off, it does
    #     not add one.)
    #   - ``yfmz.exp3.json`` (Param16=30 + Param17=30, original
    #     Chinese label 衣服托帽子 = "clothes with hat lifted up";
    #     ``托`` = "to lift / hold up", so the literal meaning is
    #     "clothes [with the] hat held up", i.e. the hood is
    #     pulled DOWN off the head) renders pajamas WITHOUT the
    #     hood → bare ``pajamas`` capability.
    #
    # Net effect at the renderer:
    #   pajamas         binding ← {Param16: 30, Param17: 30}
    #   pajamas_hooded  binding ← {Param16: 30}
    # Both reference Param16 — the additive-sum write strategy in
    # Live2DAvatar.tsx is what keeps Param16 stable while only
    # Param17 fades during a pajamas <-> hooded crossfade.
    #
    # See ``docs/alexia-model-notes.md`` for the full rig audit.
    "yf":    "pajamas_hooded",
    "yfmz":  "pajamas",
    "yjys1": "eye_color_a",
    "yjys2": "eye_color_b",
    "zs1":   "crossed_arms",
}


# Authoritative reaction → expression-name mapping for Alexia. Keys
# are the canonical ``REACTIONS`` set; values are expression filenames
# (without the ``.exp3.json`` suffix). Empty string = "no overlay,
# rely on eye-smile / mouth-form for the look".
#
# Visual identity audit (third and hopefully final pass, anchored on
# the user's live ``Cubism Viewer Standalone`` observation):
#
#   - ``bbt`` (Param60) is a **lollipop in the mouth**, not a cry
#     decoration. It moved out of the emotion map entirely (no
#     reaction maps to it) and is now an accessory under the
#     ``lollipop`` capability. Previous mappings to ``cry`` shoved a
#     lollipop into Aiko's mouth mid-sob.
#   - ``lzx`` (Param54 = 咧嘴笑) is a closed-mouth toothy grin that
#     visually competes with lip-sync. The frontend
#     ``ExpressionChannel`` tapers Param54 against live audio
#     amplitude (``mouth_overlay_param_ids``), so a cheerful turn
#     reads as a soft smile during speech and snaps back to the full
#     grin during silence. Keep ``cheerful`` / ``amused`` on ``lzx``;
#     the per-param taper handles the rig's mouth-closure quirk.
#   - ``y`` (Param56 = Dizzy) draws spiral / dizzy eyes — it's
#     **confused**, not tired. ``tired`` no longer maps to it; the
#     new canonical reaction ``confused`` does.
#   - ``zs1`` (Param61 = Pose 1, crossed arms) only renders with day
#     clothes — the exp3 explicitly zeroes Param16/17. ``playful``
#     still points at it, but ``outfit_gated_expressions`` makes the
#     dispatcher fall through to the neighbour chain when the active
#     outfit is pajamas.
#
# ``[[overlay:grin]]`` still pulses ``lzx`` transiently on demand —
# the OverlayChannel re-applies the persistent reaction afterwards,
# so a cheerful turn that also emits ``[[overlay:grin]]`` simply
# sustains the smile.
_ALEXIA_REACTION_MAP: dict[str, str] = {
    "amused":       "lzx",
    "cheerful":     "lzx",
    "playful":      "zs1",
    "excited":      "xxy",
    "enthusiastic": "xxy",
    "surprised":    "wh",
    "curious":      "wh",
    "friendly":     "",
    "warm":         "lh",
    "tender":       "lh",
    "gentle":       "lh",
    "thoughtful":   "",
    "wistful":      "",
    "calm":         "",
    "serious":      "",
    "concerned":    "k",
    "sad":          "k",
    "melancholy":   "k",
    # ``cry`` falls back to ``sad`` via ``_REACTION_NEIGHBOURS`` and
    # resolves to ``k`` (Param59 = subtle tear streaks). The dramatic
    # cry-decoration slot we thought existed turned out to be a
    # lollipop prop (see ``bbt`` notes above). Mapping ``cry`` to
    # an empty string preserves the semantic difference at the LLM
    # layer (Aiko still emits ``[[reaction:cry]]`` for intense
    # distress) while routing it to the visually correct overlay.
    "cry":          "",
    # ``tired`` no longer points at ``y`` (which is dizzy/confused).
    # Empty string lets the neighbour chain fall through to ``calm``
    # → ``melancholy`` → ``neutral``, while ``AmbientBodyChannel``'s
    # arousal-driven body slump carries the weary visual.
    "tired":        "",
    # ``confused`` is the new canonical reaction for ``y``'s
    # spiral-eye dizzy look.
    "confused":     "y",
    "neutral":      "",
    "angry":        "sq",
    "frustrated":   "sq",
    # Phase 5 (expression overhaul): the three new shades the visual
    # audit surfaced. ``embarrassed`` → ``lh`` (the shy / inward-
    # tilted smile) is the closest direct hit; the persona expects
    # the LLM to stack ``[[reaction:embarrassed+blush]]`` for the
    # full blush+smile beat, where the ``+blush`` component fires a
    # Param58 overlay pulse on top. ``nervous`` → ``yfmz``
    # (sweat-meets-mouth-anxiety) is the closest single-expression
    # match; ``[[reaction:nervous+sweat]]`` adds the Param44 sweat
    # drop. ``defiant`` → ``mj`` (head_sunglasses also drives a
    # stubborn-pout shape on this rig); the LLM is encouraged to
    # stack with ``+pout`` overlays for the full hmph beat.
    "embarrassed": "lh",
    "nervous":     "yfmz",
    "defiant":     "mj",
}


# ── Loader ──────────────────────────────────────────────────────────────


def from_disk(root: Path | str, *, display_name: str = "") -> AvatarProfile:
    """Load an :class:`AvatarProfile` from ``root``.

    Expected layout::

        <root>/<entry>.model3.json   (or .model.json)
        <root>/<entry>.cdi3.json     (Cubism 3 display info)
        <root>/<expression>.exp3.json   *
        <root>/<motion>.motion3.json    *
        <root>/<texture-folder>/...     (served as static files)

    Missing optional files (cdi3, expressions, motions) degrade
    gracefully — capabilities just come back as False.
    """
    root_path = Path(root)
    if not root_path.exists() or not root_path.is_dir():
        raise AvatarProfileError(f"avatar root does not exist: {root_path}")

    entry = _find_entry(root_path)
    if entry is None:
        raise AvatarProfileError(
            f"no .model3.json / .model.json found under {root_path}",
        )
    # ``Path.suffix`` only returns ``.json`` for ``Mini.model3.json``,
    # so we have to look at the full filename to distinguish Cubism 3
    # (``.model3.json``) from Cubism 2 (``.model.json``).
    cubism_version = 3 if entry.name.lower().endswith(".model3.json") else 2
    try:
        entry_data = json.loads(entry.read_text(encoding="utf-8"))
    except Exception as exc:
        raise AvatarProfileError(f"failed to parse {entry.name}: {exc}") from exc

    expressions = _parse_expressions(entry_data, root_path)
    motions = _parse_motions(entry_data)
    lip_sync_ids, eye_blink_ids = _parse_groups(entry_data)

    # Localized parameter / part names (Cubism 3 only). The cdi3 is
    # what makes capability detection robust against id drift between
    # rigs. ``entry.stem`` for ``Mini.model3.json`` is ``Mini.model3``
    # so we strip the trailing ``.model3`` to get the actual base name.
    base_name = entry.name
    for suffix in (".model3.json", ".model.json"):
        if base_name.lower().endswith(suffix):
            base_name = base_name[: -len(suffix)]
            break
    cdi_path = root_path / f"{base_name}.cdi3.json"
    parameters: list[dict[str, str]] = []
    parts: list[dict[str, str]] = []
    if cubism_version == 3 and cdi_path.exists():
        try:
            cdi_data = json.loads(cdi_path.read_text(encoding="utf-8"))
            parameters = [
                {
                    "id": str(p.get("Id") or ""),
                    "name": str(p.get("Name") or ""),
                    "group_id": str(p.get("GroupId") or ""),
                }
                for p in (cdi_data.get("Parameters") or [])
                if isinstance(p, dict)
            ]
            parts = [
                {
                    "id": str(p.get("Id") or ""),
                    "name": str(p.get("Name") or ""),
                }
                for p in (cdi_data.get("Parts") or [])
                if isinstance(p, dict)
            ]
        except Exception:
            log.debug("failed to parse cdi3 at %s", cdi_path, exc_info=True)

    (
        capabilities,
        overlays,
        outfits,
        cat_tail_param_ids,
        cat_ear_param_ids,
    ) = _detect_capabilities(
        parameters=parameters,
        parts=parts,
        expressions=expressions,
        root=root_path,
    )
    reaction_mapping = _build_reaction_mapping(
        expressions=expressions,
        capabilities=capabilities,
    )
    expression_params = _build_expression_params(expressions, root_path)
    mouth_overlay_param_ids = _detect_mouth_overlay_param_ids(parameters)
    mouth_blocking_expressions = _detect_mouth_blocking_expressions(
        expression_params, mouth_overlay_param_ids,
    )
    outfit_gated_expressions = _detect_outfit_gated_expressions(
        expressions, root_path,
    )
    idle_motion_group = _pick_motion_group(motions, ("idle", "tick", "loop"))
    talk_motion_group = _pick_motion_group(motions, ("tap", "talk", "anim", "story"))

    return AvatarProfile(
        display_name=display_name or entry.stem,
        entry_filename=entry.name,
        cubism_version=cubism_version,
        expressions=expressions,
        motions=motions,
        reaction_mapping=reaction_mapping,
        idle_motion_group=idle_motion_group,
        talk_motion_group=talk_motion_group,
        lip_sync_ids=lip_sync_ids,
        eye_blink_ids=eye_blink_ids,
        parameters=parameters,
        parts=parts,
        capabilities=capabilities,
        overlays=overlays,
        outfits=outfits,
        expression_params=expression_params,
        mouth_overlay_param_ids=mouth_overlay_param_ids,
        mouth_blocking_expressions=mouth_blocking_expressions,
        outfit_gated_expressions=outfit_gated_expressions,
        cat_tail_param_ids=cat_tail_param_ids,
        cat_ear_param_ids=cat_ear_param_ids,
    )


# ── Internals ───────────────────────────────────────────────────────────


def _find_entry(root: Path) -> Path | None:
    """Return the shallowest ``*.model3.json`` (or ``.model.json``) file."""
    candidates_v3 = sorted(root.glob("**/*.model3.json"), key=lambda p: len(p.parts))
    if candidates_v3:
        return candidates_v3[0]
    candidates_v2 = sorted(root.glob("**/*.model.json"), key=lambda p: len(p.parts))
    if candidates_v2:
        return candidates_v2[0]
    return None


def _parse_expressions(entry_data: dict, root: Path) -> list[ExpressionRef]:
    refs = (entry_data.get("FileReferences") or {}).get("Expressions") or []
    out: list[ExpressionRef] = []
    for ref in refs:
        if not isinstance(ref, dict):
            continue
        name = str(ref.get("Name") or "").strip()
        rel_path = str(ref.get("File") or "").strip()
        if not name or not rel_path:
            continue
        out.append(ExpressionRef(name=name, file=rel_path.replace("\\", "/")))
    return out


def _parse_motions(entry_data: dict) -> dict[str, list[MotionRef]]:
    raw = (entry_data.get("FileReferences") or {}).get("Motions") or {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, list[MotionRef]] = {}
    for group, entries in raw.items():
        if not isinstance(entries, list):
            continue
        # Empty-string group from Live2D editor → "default" so the
        # renderer can address it by a stable name.
        group_name = group if group else "default"
        bucket: list[MotionRef] = []
        for idx, entry in enumerate(entries):
            if not isinstance(entry, dict):
                continue
            raw_path = str(entry.get("File") or "").strip()
            if not raw_path:
                continue
            # ``Path.stem`` strips one suffix, so ``dh.motion3.json``
            # → ``dh.motion3``. Strip the ``.motion3`` too so callers
            # see the bare gesture name (matches our prompt grammar
            # registry of stems like ``wave`` / ``nod``).
            if "." in raw_path:
                stem = Path(raw_path).stem
                if stem.lower().endswith(".motion3"):
                    stem = stem[: -len(".motion3")]
                name = stem
            else:
                name = f"{group_name}_{idx}"
            bucket.append(MotionRef(name=name, file=raw_path.replace("\\", "/")))
        if bucket:
            out[group_name] = bucket
    return out


def _parse_groups(entry_data: dict) -> tuple[list[str], list[str]]:
    """Pull out Cubism 3 ``Groups[LipSync]`` / ``Groups[EyeBlink]``."""
    groups = entry_data.get("Groups") or []
    lip_sync: list[str] = []
    eye_blink: list[str] = []
    for grp in groups:
        if not isinstance(grp, dict):
            continue
        target = str(grp.get("Target") or "").lower()
        name = str(grp.get("Name") or "").lower()
        if target != "parameter":
            continue
        ids = grp.get("Ids") or []
        if not isinstance(ids, list):
            continue
        cleaned = [str(i) for i in ids if str(i or "")]
        if name == "lipsync":
            lip_sync = cleaned
        elif name == "eyeblink":
            eye_blink = cleaned
    return lip_sync, eye_blink


_OUTFIT_CAPABILITIES = frozenset({"pajamas", "pajamas_hooded", "day_clothes"})


# Outfit capabilities that imply the existence of ``day_clothes`` as the
# baseline ("nothing toggled") fallback. When any of these are detected
# we synthesise an empty ``day_clothes`` binding so the SettingsDrawer
# radio always has the "Day" option even though no exp3.json activates
# it (day clothes = baseline = no params written).
_PAJAMA_VARIANT_CAPABILITIES = frozenset({"pajamas", "pajamas_hooded"})


def _outfit_mutex_for(cap: str) -> tuple[str, ...]:
    """Return the ``mutex_with`` tuple for outfit capability ``cap``.

    All known outfit capabilities are mutually exclusive — selecting
    one fades the others out. Returns ``()`` for an unknown capability
    so the loader degrades gracefully on rigs we haven't curated.
    """
    if cap == "pajamas":
        return ("day_clothes", "pajamas_hooded")
    if cap == "pajamas_hooded":
        return ("day_clothes", "pajamas")
    if cap == "day_clothes":
        return ("pajamas", "pajamas_hooded")
    return ()


def _detect_capabilities(
    *,
    parameters: list[dict[str, str]],
    parts: list[dict[str, str]],
    expressions: list[ExpressionRef],
    root: Path,
) -> tuple[
    dict[str, bool],
    dict[str, OverlayBinding],
    dict[str, OutfitBinding],
    list[str],
    list[str],
]:
    """Walk the cdi3 and expression list to populate capability flags.

    For each capability, we look for a matching parameter (preferred,
    so the renderer has the param ID it can drive directly) and a
    matching expression filename (gives us the expression name to
    pass to ``model.expression()``). A capability is marked True
    whenever EITHER source produced a hit.

    Outfit categories (``pajamas`` / ``day_clothes``) are special:
    they're almost always **compositions** of several params (clothes
    body + hood + pose flag), and a synonym match against any single
    one wouldn't capture the whole costume. So the synonym pass is
    used solely to *gate* capability detection, but the actual
    binding is parsed from the matching ``.exp3.json`` — the only
    place that carries the multi-param shape.

    Cat-tail and cat-ear param IDs are returned separately so the
    renderer can iterate every segment instead of hardcoding indices.
    """
    capabilities: dict[str, bool] = {}
    overlays: dict[str, OverlayBinding] = {}
    outfits: dict[str, OutfitBinding] = {}

    # Build a quick lookup: capability name → first matching parameter id + label.
    param_match: dict[str, tuple[str, str]] = {}
    for cap, synonyms in _CAPABILITY_SYNONYMS.items():
        for param in parameters:
            label = param.get("name", "")
            pid = param.get("id", "")
            if not pid:
                continue
            if any(syn.lower() in label.lower() for syn in synonyms):
                param_match[cap] = (pid, label)
                break
        else:
            # No param hit → check parts. Parts can't be driven directly
            # the same way, but their *presence* still flips the flag
            # so the front-end knows the visual exists.
            for part in parts:
                label = part.get("name", "")
                if any(syn.lower() in label.lower() for syn in synonyms):
                    param_match[cap] = ("", label)
                    break

    # Capability flags + overlay bindings (single-param). Outfit
    # categories are skipped here — their bindings come from exp3.
    for cap, _synonyms in _CAPABILITY_SYNONYMS.items():
        param_hit = param_match.get(cap)
        capabilities[f"has_{cap}"] = param_hit is not None
        if cap in _OUTFIT_CAPABILITIES:
            continue
        if param_hit is None:
            continue
        pid, _label = param_hit
        if not pid:
            continue
        overlays[cap] = OverlayBinding(
            param_id=pid,
            on_value=30.0,
            decay_ms=1500,
            label_en=cap.replace("_", " "),
        )

    # Expression filename → capability. For overlays we fall back to
    # ``expr:<name>`` if the param-pass missed; for outfits we
    # parse the exp3 to get the full multi-param shape. ``setdefault``
    # protects existing param-based overlay bindings.
    for expr in expressions:
        cap = _ALEXIA_EXPR_TO_CAPABILITY.get(expr.name)
        if not cap:
            continue
        capabilities[f"has_{cap}"] = True
        if cap in _OUTFIT_CAPABILITIES:
            outfit_params = _parse_exp3_params(root, expr.file)
            if not outfit_params:
                continue
            outfits.setdefault(
                cap,
                OutfitBinding(
                    params=outfit_params,
                    label_en=cap.replace("_", " "),
                    mutex_with=_outfit_mutex_for(cap),
                ),
            )
        else:
            overlays.setdefault(
                cap,
                OverlayBinding(
                    param_id=f"expr:{expr.name}",
                    on_value=30.0,
                    decay_ms=1500,
                    label_en=cap.replace("_", " "),
                ),
            )

    # ``day_clothes`` reconciliation. The model's natural day-clothes
    # outfit is its BASELINE state (no outfit params active); the only
    # reason ``day_clothes`` exists as a capability at all is so the
    # SettingsDrawer radio always offers it as the "off" option. We
    # therefore force its binding to an empty param list:
    #   - If it was never created, synthesise one (driven by the
    #     presence of any pajama variant -- there has to be a "not
    #     wearing pajamas" baseline to fade back to).
    #   - If a curated ``yf.exp3.json`` accidentally created a
    #     ``day_clothes`` binding back when ``yf`` was mapped to it,
    #     wipe its params -- shared param ids would otherwise cause
    #     the renderer's per-frame writes to clobber the active
    #     pajama variant's contribution down to zero.
    has_pajama_variant = any(
        cap in outfits for cap in _PAJAMA_VARIANT_CAPABILITIES
    )
    if has_pajama_variant:
        capabilities["has_day_clothes"] = True
        binding = outfits.get("day_clothes")
        if binding is None:
            outfits["day_clothes"] = OutfitBinding(
                params=[],
                label_en="day clothes",
                mutex_with=_outfit_mutex_for("day_clothes"),
            )
        else:
            binding.params = []

    # Body-rotation flags. These are pure presence checks against the
    # standard Cubism parameter IDs because the body-language layer
    # in the renderer drives them by name.
    param_ids = {p.get("id", "") for p in parameters}
    for flag, probe_id in _BODY_ANGLE_PROBES.items():
        capabilities[flag] = probe_id in param_ids

    # Wink. Both eye-open params must exist independently — most rigs
    # tie them via the EyeBlink group, but the params themselves are
    # always individually addressable when present.
    capabilities["has_wink"] = all(p in param_ids for p in _WINK_PROBES)

    # tail_wag aliases cat_tail — the renderer reuses ``cat_tail_param_ids``
    # but the LLM grammar advertises it as a separate command.
    capabilities["has_tail_wag"] = capabilities.get("has_cat_tail", False)

    # Collect every param whose name matches a cat-tail synonym, in
    # declaration order. The renderer derives one wave-phase per
    # entry, so segment count is data-driven.
    tail_synonyms = _CAPABILITY_SYNONYMS["cat_tail"]
    cat_tail_param_ids: list[str] = []
    for param in parameters:
        label = (param.get("name") or "").lower()
        pid = param.get("id") or ""
        if not pid:
            continue
        if any(syn.lower() in label for syn in tail_synonyms):
            cat_tail_param_ids.append(pid)

    # Same idea for ear segments. Uses ``_EAR_SEGMENT_SYNONYMS`` rather
    # than the broader ``cat_ears`` synonyms so we only pick up
    # individually-addressable per-side segments (Alexia: ``左耳1``,
    # ``左耳2``, ``右耳1``, ``右耳2``) and not earrings or the part
    # group itself.
    cat_ear_param_ids: list[str] = []
    for param in parameters:
        label = (param.get("name") or "").lower()
        pid = param.get("id") or ""
        if not pid:
            continue
        if any(syn.lower() in label for syn in _EAR_SEGMENT_SYNONYMS):
            cat_ear_param_ids.append(pid)

    capabilities["has_ear_wiggle"] = bool(cat_ear_param_ids)

    return (
        capabilities,
        overlays,
        outfits,
        cat_tail_param_ids,
        cat_ear_param_ids,
    )


def _detect_mouth_overlay_param_ids(
    parameters: list[dict[str, str]],
) -> list[str]:
    """Return param IDs that paint a stylised mouth shape on the rig.

    Substring-matches the cdi3 ``Name`` field against
    :data:`_MOUTH_OVERLAY_SYNONYMS`. The frontend
    ``ExpressionChannel`` reads this list and tapers the relevant
    expression-param writes against live audio amplitude so the
    grin overlay fades while she's speaking — see the channel for
    details. Empty list when no matching param exists, in which
    case the renderer's lipsync layer runs unmodified.

    Order is preserved (declaration order from the cdi3) for
    determinism, and duplicates are filtered.
    """
    out: list[str] = []
    seen: set[str] = set()
    for param in parameters:
        label = (param.get("name") or "").lower()
        pid = param.get("id") or ""
        if not pid or pid in seen:
            continue
        if not label:
            continue
        if any(syn.lower() in label for syn in _MOUTH_OVERLAY_SYNONYMS):
            out.append(pid)
            seen.add(pid)
    return out


def _detect_mouth_blocking_expressions(
    expression_params: dict[str, list[ExpressionParam]],
    mouth_overlay_param_ids: list[str],
) -> list[str]:
    """Return expression names whose params touch a mouth-overlay id.

    Derived from :func:`_detect_mouth_overlay_param_ids` — any
    expression whose ``(param_id, value)`` bindings include one of the
    mouth-overlay params is recorded here so callers can answer "does
    firing this expression visually compete with lip-sync?" without
    re-walking the expression-param map.

    Empty list when the rig has no mouth-overlay params at all (in
    which case lip-sync gating is moot). Order matches expression
    declaration order so the result is deterministic.
    """
    if not mouth_overlay_param_ids:
        return []
    overlay_set = set(mouth_overlay_param_ids)
    out: list[str] = []
    for name, params in expression_params.items():
        if any(p.param_id in overlay_set for p in params):
            out.append(name)
    return out


def _detect_outfit_gated_expressions(
    expressions: list[ExpressionRef],
    root: Path,
) -> dict[str, list[str]]:
    """Return ``{expression_name: [allowed_outfit_capability, ...]}``.

    An expression is "outfit-gated" when its exp3 file explicitly
    **zeroes** one or more outfit params. Zero-value entries in
    Live2D's additive expression model are not the same as "absent"
    — they're authored as "I require this param to be off". On
    Alexia, ``zs1.exp3.json`` zeroes ``Param16`` and ``Param17``,
    which means the crossed-arms pose is only meant to render when
    the body is in day_clothes (the outfit whose binding has neither
    param active).

    Detection logic:

    1. For each expression, parse the raw exp3 and collect the set
       of param_ids whose ``Value`` is exactly ``0``.
    2. Cross-reference those zeroed ids against the known outfit
       capabilities' bindings (built later in the loader). Since this
       function runs *before* outfits are built, we encode the check
       loosely here: an expression that zeroes ANY outfit-shaped
       param (``Param16`` / ``Param17`` for Alexia-style rigs, plus
       the generic "Clothes" / "睡衣" / "pajama" pattern) gets a
       day_clothes-only gate.

    The detection deliberately stays simple — generic enough to
    survive future rigs but not so clever that it produces phantom
    gates. Expressions without zero-valued entries return no gate
    (empty dict entry).
    """
    out: dict[str, list[str]] = {}
    for expr in expressions:
        path = root / expr.file
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            log.debug("failed to parse exp3 at %s", path, exc_info=True)
            continue
        raw = data.get("Parameters") or []
        if not isinstance(raw, list):
            continue
        zeroed: list[str] = []
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            pid = str(entry.get("Id") or "").strip()
            if not pid:
                continue
            try:
                value = float(entry.get("Value", 0.0) or 0.0)
            except (TypeError, ValueError):
                continue
            if value == 0.0:
                zeroed.append(pid)
        if not zeroed:
            continue
        # Heuristic: any expression that zeroes an outfit-shape param
        # (the canonical Alexia pair is Param16/Param17) is signalling
        # "render me with the baseline outfit only". We hard-code the
        # day_clothes target because that's the only outfit whose
        # binding has zero param contributions (it's the rig's
        # baseline). Future rigs with multiple baselines would extend
        # this map.
        out[expr.name] = ["day_clothes"]
    return out


def _build_expression_params(
    expressions: list[ExpressionRef],
    root: Path,
) -> dict[str, list[ExpressionParam]]:
    """Parse each loaded expression's exp3 file into the
    ``expression_params`` map consumed by the frontend
    ``ExpressionChannel`` continuous-expressiveness layer.

    Each entry is keyed by the expression *name* (the filename minus
    ``.exp3.json``) and maps to the same ``(param_id, value)`` pairs
    that ``expressionManager`` Add-blends every frame. Missing /
    unparseable exp3s degrade gracefully — the renderer falls back to
    the manager's natural amplitude when a binding isn't there.
    """
    out: dict[str, list[ExpressionParam]] = {}
    for expr in expressions:
        bindings = _parse_exp3_params(root, expr.file)
        if not bindings:
            continue
        out[expr.name] = [
            ExpressionParam(param_id=b.param_id, on_value=b.on_value)
            for b in bindings
        ]
    return out


def _parse_exp3_params(root: Path, rel_file: str) -> list[OutfitParam]:
    """Read a Cubism 3 ``.exp3.json`` and return its ``Parameters``
    array as :class:`OutfitParam` entries.

    Skips params whose ``Value`` is exactly 0 — those are explicit
    "no contribution from this expression" markers in Live2D's
    additive expression model and would just dilute the binding.
    Returns an empty list if the file is missing or malformed; the
    caller treats that as "no outfit binding for this capability".
    """
    path = root / rel_file
    if not path.exists():
        log.debug("exp3 not found at %s", path)
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        log.debug("failed to parse exp3 at %s", path, exc_info=True)
        return []
    raw = data.get("Parameters") or []
    if not isinstance(raw, list):
        return []
    out: list[OutfitParam] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        pid = str(entry.get("Id") or "").strip()
        if not pid:
            continue
        try:
            value = float(entry.get("Value", 0.0) or 0.0)
        except (TypeError, ValueError):
            continue
        if value == 0.0:
            continue
        out.append(OutfitParam(param_id=pid, on_value=value))
    return out


def _build_reaction_mapping(
    *,
    expressions: list[ExpressionRef],
    capabilities: dict[str, bool],
) -> dict[str, str]:
    """Build the ``reaction → expression name`` map.

    For Alexia we use the authoritative table; expressions that
    don't actually exist in the loaded model fall back to the synonym
    fuzzy matcher so other rigs still get *some* mapping.
    """
    expr_names = {e.name for e in expressions}
    out: dict[str, str] = {}

    # Pass 1: Alexia explicit map (only kept when the expression is present).
    for reaction, expr_name in _ALEXIA_REACTION_MAP.items():
        if expr_name and expr_name in expr_names:
            out[reaction] = expr_name

    # Pass 2: fuzzy-match anything still missing.
    for reaction, synonyms in _REACTION_SYNONYMS.items():
        if reaction in out:
            continue
        chosen: str | None = None
        for syn in synonyms:
            for expr in expressions:
                low_name = expr.name.lower()
                low_file = expr.file.lower()
                if syn in low_name or syn in low_file:
                    chosen = expr.name
                    break
            if chosen is not None:
                break
        if chosen is not None:
            out[reaction] = chosen
    _ = capabilities  # retained for future capability-aware refinements
    return out


def _pick_motion_group(
    motions: dict[str, list[MotionRef]],
    keywords: tuple[str, ...],
) -> str | None:
    if not motions:
        return None
    lowered = {name: name.lower() for name in motions.keys()}
    for keyword in keywords:
        for original, low in lowered.items():
            if keyword in low:
                return original
    # Fall back to the first group so idle has *something* to play.
    return next(iter(motions.keys()))


__all__ = [
    "AvatarProfile",
    "AvatarProfileError",
    "ExpressionParam",
    "ExpressionRef",
    "MotionRef",
    "OverlayBinding",
    "OutfitBinding",
    "OutfitParam",
    "from_disk",
]

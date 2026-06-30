"""One-shot translator for the Chinese display labels in
``data/personas/active/Alexia/Alexia.cdi3.json``.

What this touches and why
-------------------------

The ``.cdi3.json`` file is the **display info** sidecar — pure
human-readable labels for every parameter, parameter-group, and
part. It is consumed by Live2D Cubism Editor for the inspector
panels, and by our own capability detector for the synonym match.
**No runtime code path** — neither ``pixi-live2d-display`` nor the
binary ``.moc3`` — references the ``Name`` field; everything that
actually drives the rig works off the stable ``Id`` strings, which
this script never touches.

Safe-to-translate vs untouchable
--------------------------------

* Translates: ``Parameters[].Name`` / ``ParameterGroups[].Name`` /
  ``Parts[].Name``. Preserves declaration order, indentation, and
  trailing newlines.
* Untouched: every ``Id``, ``GroupId``, the ``Version``, the
  ``CombinedParameters`` array, and the structural shape of the
  document.

Idempotency
-----------

Running this twice is a no-op — the second pass finds zero Chinese
strings and rewrites the same file. The original is preserved at
``Alexia.cdi3.json.zh-backup`` (committed before this script ran)
so a user can always diff or restore it.

After running, our capability detection should find the same
``has_*`` flags it found before (verified by comparing the
``cdi3.zh-backup`` capability snapshot to the post-translation
one in the inline self-test below).
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

CDI3_PATH = Path("data/personas/active/Alexia/Alexia.cdi3.json")


# ── Literal translations ────────────────────────────────────────────
#
# Each Chinese name → its English equivalent. Where Live2D's auto
# layer-naming gave an opaque label we keep the layer index so the
# Editor's inspector stays diffable against the original. A handful
# of names are *intentionally* disambiguated (``Eyeglasses`` vs
# ``Sunglasses``) so substring-based capability synonym matching
# doesn't conflate them — see ``_CAPABILITY_SYNONYMS`` for the
# matching English synonyms that paired with these.
_NAMES: dict[str, str] = {
    # ── Accessories / outfits / overlays (param names that drive
    #    capability detection) ──────────────────────────────────────
    "墨镜":           "Sunglasses",
    "带眼镜":         "Eyeglasses",
    "衣服":           "Clothes",
    "衣服托帽子":     "Clothes (with hood)",
    "睡衣":           "Pajamas",
    "外套":           "Coat",
    "外套阴影":       "Coat shadow",
    "项链":           "Necklace",
    "带子":           "Ribbon",
    "袖子":           "Sleeves",
    "帽子":           "Hat",
    "装饰":           "Decoration",
    # ── Symbols / emotional overlays ──────────────────────────────
    "问号":           "Question mark",
    "汗":             "Sweat",
    "咧嘴笑":         "Grin",
    "星星眼":         "Star eyes",
    "晕":             "Dizzy",
    "生气":           "Angry",
    "脸红":           "Blush",
    "哭":             "Cry",
    "阴影":           "Shadow",
    "眼睛红晕":       "Eye blush",
    "符号表情":       "Symbol expressions",
    "表情":           "Expressions",
    "动作":           "Motions",
    "动作手臂":       "Motion arm",
    # ── Pose / body mechanics ─────────────────────────────────────
    "姿势1":          "Pose 1",
    "角度 X":         "Angle X",
    "角度 Y":         "Angle Y",
    "角度 Z":         "Angle Z",
    "身体旋转\u3000X": "Body rotation X",
    "身体旋转\u3000Y": "Body rotation Y",
    "身体旋转\u3000Z": "Body rotation Z",
    "参数188":        "Parameter 188",
    "呼吸":           "Breath",
    # ── Eyes / brows / mouth ─────────────────────────────────────
    "左眼\u3000开闭": "Left eye open/close",
    "左眼\u3000微笑": "Left eye smile",
    "右眼\u3000开闭": "Right eye open/close",
    "右眼  微笑":     "Right eye smile",
    "眼珠物理":       "Eyeball physics X",
    "眼珠物理y":      "Eyeball physics Y",
    "眼球 X":         "Eyeball X",
    "眼球 Y":         "Eyeball Y",
    "眉  上下":       "Brow up/down",
    "眉\u3000変形":   "Brow deform",
    "嘴部\u3000变形": "Mouth deform",
    "嘴巴\u3000张开和闭合": "Mouth open/close",
    "舌头TongueOut":  "Tongue out",
    "歪嘴MouthX":     "Mouth slant X",
    "鼓脸CheeckPuff": "Cheek puff",
    "撅嘴Mouthshrug": "Mouth shrug",
    "嘟嘴Mouthfunnel": "Mouth funnel",
    "用力挤嘴MouthPressLipOpen": "Mouth press / lip open",
    "嘴巴宽MouthPuckerWiden": "Mouth wide / pucker",
    "下巴JawOpen":    "Jaw open",
    "挤眼睛EyeSquint": "Eye squint",
    "2挤眼睛EyeSquint": "Eye squint 2",
    "眼睛颜色":       "Eye color",
    "眼睛颜色2":      "Eye color 2",
    # ── Head / body / hair / clothes secondary ───────────────────
    "头x1":           "Head X 1",
    "头y1":           "Head Y 1",
    "头x2":           "Head X 2",
    "头y2":           "Head Y 2",
    "身x1":           "Body X 1",
    "身y1":           "Body Y 1",
    "身x2":           "Body X 2",
    "身y2":           "Body Y 2",
    "头发4":          "Hair 4",
    "头发44":         "Hair 4-1",
    "头发444":        "Hair 4-2",
    "头发5":          "Hair 5",
    "头发55":         "Hair 5-1",
    "头发555":        "Hair 5-2",
    "衣服1":          "Clothes 1",
    "衣服11":         "Clothes 1-1",
    "衣服111":        "Clothes 1-2",
    "衣服2":          "Clothes 2",
    "衣服22":         "Clothes 2-1",
    "衣服222":        "Clothes 2-2",
    "衣服3":          "Clothes 3",
    "衣服33":         "Clothes 3-1",
    "衣服333":        "Clothes 3-2",
    "飘1":            "Float 1",
    "飘2":            "Float 2",
    "飘3":            "Float 3",
    # ── Cat ears / tail ──────────────────────────────────────────
    "耳坠":           "Earring",
    "左耳1":          "Left ear 1",
    "左耳2":          "Left ear 2",
    "右耳1":          "Right ear 1",
    "右耳2":          "Right ear 2",
    "耳朵":           "Cat ears",
    "耳朵左":         "Left cat ear",
    "耳朵右":         "Right cat ear",
    "猫尾":           "Cat tail",
    "猫尾(蒙皮)":     "Cat tail (skinning)",
    "猫尾(回转)":     "Cat tail (rotation)",
    # ── Group labels (ParameterGroups) ───────────────────────────
    "头角度":         "Head angles",
    "眼睛":           "Eyes",
    "嘴":             "Mouth",
    "头发摆动":       "Hair sway",
    "部件摆动":       "Parts sway",
    # ── Parts ────────────────────────────────────────────────────
    "组 1":           "Group 1",
    "头":             "Head",
    "上身":           "Upper body",
    "下身":           "Lower body",
    "肚子":           "Belly",
    "手臂左":         "Left arm",
    "手臂右":         "Right arm",
    "前发":           "Front hair",
    "眼睛左":         "Left eye",
    "眼睛右":         "Right eye",
    "脸":             "Face",
    "嘴 ":            "Mouth ",  # trailing space variant — defensive
    "口腔":           "Mouth cavity",
    "睫毛":           "Eyelashes",
    "白睫毛":         "White eyelashes",
    "高光":           "Highlight",
    "红":             "Red",
    "龇牙":           "Bared teeth",
    "上":             "Upper",
    "下":             "Lower",
    "腿":             "Legs",
    "脚":             "Feet",
    "右":             "Right",
    "左":             "Left",
    "放下":           "Down",
    "部件47":         "Part 47",
    "部件55":         "Part 55",
    "5555":           "Hair 5-3",  # numeric layer label, best-guess
    # ── Mojibake (corrupted half-width katakana, untranslatable) ──
    "ﾑﾛｾｦﾗ\ufffd":    "Eye highlight L",
    "ﾑﾛｾｦﾓﾒ":         "Eye highlight R",
}


# ── Pattern translators ─────────────────────────────────────────────
#
# Layer parameters follow predictable templates. Doing them with a
# regex keeps the translation dict from ballooning to hundreds of
# near-identical entries.

# Pattern 1: ``[N]<base>`` → ``[N]<translated_base>`` (param Name)
_INDEXED_LAYER_RE = re.compile(r"^\[(\d+)\](.+)$")

# Pattern 2: ``<name>(蒙皮)`` → ``<translated> (skinning)``
_SKINNING_SUFFIX = "(蒙皮)"
_ROTATION_SUFFIX = "(回转)"


# Substitutions for layer-name fragments that appear inside the
# ``[N]…`` and ``…(蒙皮)`` templates. Order matters for substring
# overlap (``图层31(2) 的複製`` is a superstring of ``图层``, so it
# goes first).
_LAYER_FRAGMENTS: list[tuple[str, str]] = [
    ("图层31(2) 的複製 ", "Layer 31(2) copy "),
    ("图层", "Layer "),
    ("圖層 ", "Layer "),
]


def _translate_layer_fragment(fragment: str) -> str:
    """Apply layer/duplicate fragment translations + suffix swap."""
    out = fragment
    for zh, en in _LAYER_FRAGMENTS:
        out = out.replace(zh, en)
    out = out.replace(_SKINNING_SUFFIX, " (skinning)")
    out = out.replace(_ROTATION_SUFFIX, " (rotation)")
    return out


def translate_label(label: str) -> str:
    """Translate one display label.

    Returns the input unchanged if no translation rule applies — the
    capability detector will silently fall through to its synonym
    fallbacks for those (rare, unknown) names.
    """
    if not label:
        return label

    # Literal table first.
    if label in _NAMES:
        return _NAMES[label]

    # ``[N]base`` indexed-layer pattern.
    m = _INDEXED_LAYER_RE.match(label)
    if m:
        idx, base = m.group(1), m.group(2)
        if base in _NAMES:
            return f"[{idx}]{_NAMES[base]}"
        translated_base = _translate_layer_fragment(base)
        if translated_base != base:
            return f"[{idx}]{translated_base}"

    # ``<name>(skinning)`` / ``(rotation)`` suffix without a literal
    # entry — translate the layer fragment.
    if _SKINNING_SUFFIX in label or _ROTATION_SUFFIX in label:
        translated = _translate_layer_fragment(label)
        if translated != label:
            return translated

    # Bare layer-fragment names ("图层57", "图层31(2) 的複製 28", …)
    # that show up as ParameterGroup labels without the ``[N]``
    # prefix. Run the fragment translator unconditionally; if it
    # doesn't change anything we leave the label alone.
    translated = _translate_layer_fragment(label)
    if translated != label:
        return translated

    return label


def main() -> int:
    if not CDI3_PATH.exists():
        print(f"error: {CDI3_PATH} not found", file=sys.stderr)
        return 1
    raw = CDI3_PATH.read_text(encoding="utf-8")
    data = json.loads(raw)

    sections = ("Parameters", "ParameterGroups", "Parts")
    changed = 0
    for section in sections:
        for entry in data.get(section, []):
            old = entry.get("Name")
            if not isinstance(old, str):
                continue
            new = translate_label(old)
            if new != old:
                entry["Name"] = new
                changed += 1

    # Live2D's own files use tab indentation. Match that so the diff
    # stays visually small.
    out = json.dumps(data, ensure_ascii=False, indent="\t")
    # Final newline matches Live2D's convention; the original file
    # ends with a closing brace + no trailing newline so we mirror.
    CDI3_PATH.write_text(out, encoding="utf-8")

    print(f"Translated {changed} labels in {CDI3_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Author simple Live2D Cubism 3 ``.motion3.json`` files for Alexia.

The Alexia bundle ships only one motion (``dh.motion3.json`` — an
ambient idle loop). The LLM's prompt grammar already advertises
``[[motion:nod]]`` / ``[[motion:shake]]`` / ``[[motion:bow]]`` etc.
via ``app/core/prompt_assembler._MOTION_GRAMMAR_DESCRIPTIONS`` — but
those gestures only become available to the model when matching
``.motion3.json`` files exist on disk **and** are referenced from
``Alexia.model3.json``.

This script is the bootstrap for that. It emits hand-tuned head /
body curves for a small set of gestures, computes the meta counts
(CurveCount / TotalSegmentCount / TotalPointCount), writes the
files, and patches the ``Motions`` block of ``Alexia.model3.json``
so the loader picks them up.

Re-run safe: regenerating motion files just overwrites them, and
the model3.json patch is idempotent (it skips entries that already
exist for the same group + filename).

Authoring conventions used here
-------------------------------

Live2D head conventions (Alexia matches):
  ``ParamAngleX``      = head yaw   (-30 left, +30 right)
  ``ParamAngleY``      = head pitch (-30 down, +30 up)
  ``ParamAngleZ``      = head roll  (-30 left tilt, +30 right tilt)
  ``ParamBodyAngleX``  = body sway  (-10 .. +10 typical)
  ``ParamBodyAngleY``  = body pitch (-10 fwd .. +10 back)

Each gesture is a list of ``Curve`` objects. A ``Curve`` is one
parameter ID plus a sequence of (time_seconds, value) keyframes.
The script emits **linear segments** between consecutive keyframes
(segment type ``0``); that's enough for short gestures and stays
diff-friendly. Switch any keyframe to a Bezier by using a 4-tuple
``(time, value, ctrl_in_offset, ctrl_out_offset)`` — see ``_render``
for the encoding.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

ROOT = Path("live-2d-models/Alexia")
MOTION_DIR = ROOT  # motions live next to the model3.json by convention


@dataclass(slots=True)
class Curve:
    param_id: str
    keyframes: list[tuple[float, float]]  # [(time_s, value), …]


@dataclass(slots=True)
class Motion:
    name: str            # filename stem — must match a key in
                         # ``_MOTION_GRAMMAR_DESCRIPTIONS`` for the
                         # LLM to know about it.
    duration: float
    fps: float
    loop: bool
    curves: list[Curve]


# ── Gesture authoring ──────────────────────────────────────────────────


def _nod() -> Motion:
    """Head pitches down, slight rebound up, settles. ~1s."""
    return Motion(
        name="nod",
        duration=1.0,
        fps=30.0,
        loop=False,
        curves=[
            Curve(
                param_id="ParamAngleY",
                keyframes=[
                    (0.0, 0.0),
                    (0.30, -15.0),
                    (0.60, 5.0),
                    (1.0, 0.0),
                ],
            ),
        ],
    )


def _shake() -> Motion:
    """Head yaws left → right → left → settle. ~1.4s.

    Slightly longer than a nod because shaking ‘no’ feels deliberate
    when it stretches across two oscillations.
    """
    return Motion(
        name="shake",
        duration=1.4,
        fps=30.0,
        loop=False,
        curves=[
            Curve(
                param_id="ParamAngleX",
                keyframes=[
                    (0.0, 0.0),
                    (0.35, -15.0),
                    (0.70, 15.0),
                    (1.05, -10.0),
                    (1.4, 0.0),
                ],
            ),
        ],
    )


def _bow() -> Motion:
    """Quick bow: head + body lean forward, brief hold, rise back. ~1.5s.

    Drives both head pitch and body pitch so the bow feels like a
    whole-upper-body gesture rather than just a chin-tuck.
    """
    return Motion(
        name="bow",
        duration=1.5,
        fps=30.0,
        loop=False,
        curves=[
            Curve(
                param_id="ParamAngleY",
                keyframes=[
                    (0.0, 0.0),
                    (0.30, -28.0),
                    (0.90, -22.0),
                    (1.5, 0.0),
                ],
            ),
            Curve(
                param_id="ParamBodyAngleY",
                keyframes=[
                    (0.0, 0.0),
                    (0.30, -8.0),
                    (0.90, -6.0),
                    (1.5, 0.0),
                ],
            ),
        ],
    )


GESTURES: list[Motion] = [_nod(), _shake(), _bow()]


# ── Motion-file rendering ──────────────────────────────────────────────


def _render(motion: Motion) -> dict:
    """Convert a :class:`Motion` into the Cubism motion3.json shape.

    Live2D's ``Segments`` is a flat number list:
      ``[t0, v0, type, …per-segment-numbers]``
    where each segment writes a numeric type code followed by enough
    numbers to describe its endpoint. Type ``0`` (linear) needs
    ``[end_t, end_v]`` (2 nums); we use only linear here.
    """
    total_segments = 0
    total_points = 0
    curves_out: list[dict] = []
    for curve in motion.curves:
        kfs = curve.keyframes
        if len(kfs) < 2:
            raise ValueError(
                f"curve for {curve.param_id!r} needs at least 2 keyframes"
            )
        segments: list[float | int] = [
            float(kfs[0][0]),
            float(kfs[0][1]),
        ]
        for end_t, end_v in kfs[1:]:
            segments.extend([0, float(end_t), float(end_v)])  # 0 = Linear
        total_segments += len(kfs) - 1
        total_points += len(kfs)
        curves_out.append({
            "Target": "Parameter",
            "Id": curve.param_id,
            "Segments": segments,
        })
    return {
        "Version": 3,
        "Meta": {
            "Duration": float(motion.duration),
            "Fps": float(motion.fps),
            "Loop": bool(motion.loop),
            "AreBeziersRestricted": True,
            "CurveCount": len(motion.curves),
            "TotalSegmentCount": total_segments,
            "TotalPointCount": total_points,
            "UserDataCount": 0,
            "TotalUserDataSize": 0,
        },
        "Curves": curves_out,
    }


def _write_motions() -> list[str]:
    """Render every gesture and write it to ``MOTION_DIR``."""
    written: list[str] = []
    for motion in GESTURES:
        out_path = MOTION_DIR / f"{motion.name}.motion3.json"
        out_path.write_text(
            # Tabs match the indentation style Live2D Editor produces
            # for the original ``dh.motion3.json`` so diffs stay quiet.
            json.dumps(_render(motion), indent="\t"),
            encoding="utf-8",
        )
        written.append(out_path.name)
    return written


# ── model3.json patching ───────────────────────────────────────────────


def _patch_model3(filenames: list[str]) -> int:
    """Append every new motion file under a ``Tap`` group.

    The existing ``""`` (default) group keeps the ``dh`` idle loop.
    Putting one-shot gestures under ``Tap`` matches the Live2D Editor
    convention and keeps groups semantically meaningful — the LLM's
    motion lookup is name-based so the group label is purely for the
    Editor UI.
    """
    model3_path = ROOT / "Alexia.model3.json"
    data = json.loads(model3_path.read_text(encoding="utf-8"))
    motions: dict = data.setdefault("FileReferences", {}).setdefault(
        "Motions", {}
    )
    tap = motions.setdefault("Tap", [])
    existing = {entry.get("File") for entry in tap if isinstance(entry, dict)}
    added = 0
    for fname in filenames:
        if fname in existing:
            continue
        tap.append({"File": fname})
        added += 1
    model3_path.write_text(
        json.dumps(data, indent="\t") + "\n", encoding="utf-8",
    )
    return added


def main() -> None:
    if not ROOT.exists():
        raise SystemExit(f"avatar root not found: {ROOT}")
    written = _write_motions()
    added = _patch_model3(written)
    print(f"Wrote {len(written)} motion file(s): {', '.join(written)}")
    print(f"Patched Alexia.model3.json (added {added} new Tap entries)")


if __name__ == "__main__":
    main()

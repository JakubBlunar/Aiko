"""Run the same phrases through every engine and build a listening page.

What this measures, and why those things
----------------------------------------
Quality is the thing you can only judge by ear, so this does not try to
score it. It measures the four things that can disqualify an engine
before taste enters the room, all of them properties of *our* pipeline
rather than of the engine in the abstract:

* **first-audio latency.** ``SessionController`` streams ~50 ms PCM
  chunks and she is expected to start speaking promptly. Note what this
  number means for an engine whose ``streams_generation`` is False --
  including the incumbent, which paces a *finished* clip out in small
  chunks: first audio waits for the last token, so the figure here is
  the whole generation time. That is the honest number rather than a
  missing one, and it is why the long phrase costs over two seconds
  before she makes a sound. Production hides it by feeding text a
  sentence at a time; a single long utterance has nowhere to hide.
* **realtime factor**, on this box, on these cores. Published RTF
  numbers come from other people's machines and are the single most
  optimistic figure in any model card.
* **load time**, because the service warms up on startup and a 30-second
  model load is felt every time the app restarts.
* **control surface**, from :class:`Caps` -- printed beside the audio so
  a pretty engine with nothing for the cadence layer to say cannot win
  on sound alone.

The listening page defaults to blind
------------------------------------
Labels are hidden and clip order is shuffled per phrase, because the
stated goal is "keep Aiko's current voice" and that makes the incumbent
impossible to judge fairly once you know which one it is -- the
familiar clip sounds correct by definition. Vote first, reveal after.
Votes are tallied in ``localStorage`` so closing the tab does not lose
them.

Usage::

    python -m tools.tts_lab.bench                       # every engine
    python -m tools.tts_lab.bench --engines pocket-tts
    python -m tools.tts_lab.bench --voice-ref voices/reference/aiko_reference.wav
    python -m tools.tts_lab.bench --open                # launch the page
"""

from __future__ import annotations

import argparse
import html
import json
import random
import sys
import webbrowser
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from tools.tts_lab import adapters
from tools.tts_lab.adapters import REPO_ROOT, Caps, assess, write_wav

OUT_DIR = REPO_ROOT / "voices" / "audition"
DEFAULT_REF = REPO_ROOT / "voices" / "reference" / "aiko_reference.wav"

#: Chosen to exercise the failure modes rather than to sound nice. Each
#: one is here because some engine somewhere gets it wrong.
PHRASES: tuple[tuple[str, str], ...] = (
    ("plain", "I finished the thing I was working on, finally."),
    ("question", "Did you actually sleep, or did you just lie there?"),
    ("excited", "Oh, that is so much better than I expected!"),
    # Long sentences are where clip-at-a-time engines blow the latency
    # budget and where clones start drifting off the voice.
    ("long", (
        "I was reading about it earlier and it turns out the whole "
        "problem was a single missing character, which is either very "
        "funny or very annoying depending on how your day went."
    )),
    # Numbers and times are the classic normaliser bug: "1:30" read as
    # "one colon thirty", "9950X3D" read letter by letter.
    ("numbers", "It arrived at 4:15, which is 20 minutes earlier than they said."),
    # Trailing-comma and ellipsis handling separates engines that model
    # prosody from engines that model characters.
    ("soft", "I do not know... maybe it does not matter that much."),
)


@dataclass
class Row:
    engine: str
    voice: str
    phrase_id: str
    text: str
    wav: str
    duration_s: float
    first_chunk_ms: float
    total_ms: float
    rtf: float
    sample_rate: int
    warnings: list[str] = field(default_factory=list)


@dataclass
class Result:
    rows: list[Row] = field(default_factory=list)
    caps: dict[str, dict] = field(default_factory=dict)
    coverage: dict[str, dict[str, str]] = field(default_factory=dict)
    load_ms: dict[str, float] = field(default_factory=dict)
    unavailable: dict[str, str] = field(default_factory=dict)


def run(
    engine_names: list[str],
    *,
    voice_ref: Path | None,
    voice_id: str | None,
    out_dir: Path,
    phrases: tuple[tuple[str, str], ...] = PHRASES,
) -> Result:
    out = Result()
    out_dir.mkdir(parents=True, exist_ok=True)

    for name in engine_names:
        engine = adapters.build(name)
        try:
            engine.load()
        except Exception as exc:
            # An engine that will not install is a finding, not a crash.
            # It stays in the report so the next reader does not spend an
            # afternoon rediscovering it.
            print(f"{name}: unavailable -- {exc}")
            out.unavailable[name] = str(exc)
            continue

        out.caps[name] = asdict(engine.caps)
        out.coverage[name] = engine.caps.prosody_coverage()
        out.load_ms[name] = round(engine.load_ms, 1)
        print(f"{name}: up in {engine.load_ms:.0f}ms")

        voices: list[tuple[str, object]] = []
        if voice_id:
            try:
                voices.append((f"{voice_id}", engine.voice_from_id(voice_id)))
            except Exception as exc:
                print(f"  named voice {voice_id} unusable: {exc}")
        if voice_ref is not None and voice_ref.exists():
            try:
                voices.append(("cloned", engine.voice_from_reference(voice_ref)))
            except Exception as exc:
                print(f"  clone from {voice_ref.name} failed: {exc}")
        if not voices:
            out.unavailable[name] = "no usable voice"
            continue

        for voice_label, voice in voices:
            for phrase_id, text in phrases:
                try:
                    synth = engine.synth(text, voice)
                except Exception as exc:
                    print(f"  {phrase_id}/{voice_label}: FAILED {exc!r}")
                    continue
                quality = assess(synth.audio, synth.sample_rate)
                stem = f"{name}__{voice_label}__{phrase_id}".replace(
                    ".", "_"
                ).replace(" ", "-")
                path = write_wav(
                    out_dir / f"{stem}.wav", synth.audio, synth.sample_rate
                )
                out.rows.append(
                    Row(
                        engine=name,
                        voice=voice_label,
                        phrase_id=phrase_id,
                        text=text,
                        wav=path.name,
                        duration_s=round(synth.duration_s, 3),
                        first_chunk_ms=round(synth.first_chunk_ms, 1),
                        total_ms=round(synth.total_ms, 1),
                        rtf=round(synth.rtf, 3),
                        sample_rate=synth.sample_rate,
                        warnings=list(quality.warnings),
                    )
                )
                flag = "" if quality.ok else "  !! " + ", ".join(quality.warnings)
                print(
                    f"  {phrase_id:9} {voice_label:14} "
                    f"{synth.duration_s:5.2f}s  first {synth.first_chunk_ms:6.0f}ms  "
                    f"rtf {synth.rtf:5.2f}{flag}"
                )
    return out


# ── report ───────────────────────────────────────────────────────────

_CSS = """
:root { color-scheme: dark; }
body { font: 15px/1.55 ui-sans-serif, system-ui, sans-serif;
       margin: 0; padding: 32px; background: #14161a; color: #e6e8ec; }
h1 { font-size: 21px; margin: 0 0 4px; }
h2 { font-size: 16px; margin: 34px 0 10px; color: #cfd3da;
     border-bottom: 1px solid #2a2e36; padding-bottom: 6px; }
.sub { color: #8b919c; margin: 0 0 22px; font-size: 13px; }
.bar { display: flex; gap: 10px; align-items: center; flex-wrap: wrap;
       background: #1b1e24; border: 1px solid #2a2e36; border-radius: 8px;
       padding: 12px 14px; margin-bottom: 8px; position: sticky; top: 0;
       z-index: 5; }
button { font: inherit; background: #2a2f38; color: #e6e8ec; border: 0;
         border-radius: 6px; padding: 7px 13px; cursor: pointer; }
button:hover { background: #353b46; }
button.on { background: #4a6fa5; }
.phrase { background: #1b1e24; border: 1px solid #2a2e36; border-radius: 8px;
          padding: 14px 16px; margin-bottom: 14px; }
.ptext { color: #b9c0cb; font-style: italic; margin-bottom: 12px; }
.clip { display: grid; grid-template-columns: 34px 1fr 300px 190px;
        gap: 12px; align-items: center; padding: 7px 0;
        border-top: 1px solid #23272e; }
.clip:first-of-type { border-top: 0; }
.who { font-variant-numeric: tabular-nums; color: #8b919c; }
.lab { font-weight: 600; }
.lab.hid { color: #6b7280; font-weight: 400; }
.met { color: #8b919c; font-size: 12px; font-variant-numeric: tabular-nums; }
audio { width: 300px; height: 32px; }
.vote { background: #23272e; }
.vote.on { background: #3d7a4e; }
table { border-collapse: collapse; width: 100%; font-size: 13px; }
th, td { text-align: left; padding: 6px 10px; border-bottom: 1px solid #23272e;
         vertical-align: top; }
th { color: #8b919c; font-weight: 600; }
code { background: #23272e; padding: 1px 5px; border-radius: 4px;
       font-size: 12px; }
.warn { color: #e0a458; }
.tally { color: #9fd0aa; }
"""

_JS = """
const KEY = 'ttslab.votes.v1';
let votes = {};
try { votes = JSON.parse(localStorage.getItem(KEY) || '{}'); } catch (e) {}
let revealed = false;

function paint() {
  document.querySelectorAll('.lab').forEach(el => {
    el.textContent = revealed ? el.dataset.real : el.dataset.blind;
    el.classList.toggle('hid', !revealed);
  });
  document.querySelectorAll('.met').forEach(el => {
    el.style.visibility = revealed ? 'visible' : 'hidden';
  });
  document.querySelectorAll('.vote').forEach(el => {
    el.classList.toggle('on', votes[el.dataset.phrase] === el.dataset.key);
  });
  const counts = {};
  Object.values(votes).forEach(v => { counts[v] = (counts[v] || 0) + 1; });
  const parts = Object.entries(counts).sort((a, b) => b[1] - a[1])
    .map(([k, n]) => k + ': ' + n);
  document.getElementById('tally').textContent = revealed
    ? (parts.length ? 'votes -- ' + parts.join(', ') : 'no votes yet')
    : Object.keys(votes).length + ' of ' + TOTAL + ' phrases voted';
}

document.addEventListener('click', ev => {
  const b = ev.target.closest('.vote');
  if (b) {
    const p = b.dataset.phrase;
    votes[p] = votes[p] === b.dataset.key ? undefined : b.dataset.key;
    if (!votes[p]) delete votes[p];
    localStorage.setItem(KEY, JSON.stringify(votes));
    paint();
    return;
  }
  if (ev.target.id === 'reveal') {
    revealed = !revealed;
    ev.target.classList.toggle('on', revealed);
    ev.target.textContent = revealed ? 'hide labels' : 'reveal labels';
    paint();
  }
  if (ev.target.id === 'clear') {
    votes = {}; localStorage.removeItem(KEY); paint();
  }
});
paint();
"""


def _caps_table(result: Result) -> str:
    if not result.caps:
        return "<p class='sub'>No engine loaded.</p>"
    fields = [
        ("params", "params"),
        ("sample_rate", "Hz"),
        ("torch_free", "torch-free"),
        ("streams_generation", "streams generation"),
        ("clone_seconds", "clone ref (s)"),
        ("native_rate", "native rate"),
        ("numeric_expressiveness", "numeric expr"),
        ("nl_instruct", "NL instruct"),
        ("license", "license"),
        ("verified", "verified"),
    ]
    names = sorted(result.caps)
    head = "".join(f"<th>{html.escape(n)}</th>" for n in names)
    body = []
    for key, label in fields:
        cells = []
        for n in names:
            raw = result.caps[n].get(key)
            if isinstance(raw, bool):
                text = "yes" if raw else "no"
            elif raw in (None, "", 0, 0.0):
                text = "-"
            else:
                text = str(raw)
            cells.append(f"<td>{html.escape(text)}</td>")
        body.append(f"<tr><th>{label}</th>{''.join(cells)}</tr>")

    for n in names:
        tags = result.caps[n].get("inline_tags") or []
        result.caps[n]["_tags"] = ", ".join(tags) if tags else "-"
    body.append(
        "<tr><th>inline tags</th>"
        + "".join(
            f"<td>{html.escape(str(result.caps[n]['_tags']))}</td>"
            for n in names
        )
        + "</tr>"
    )
    body.append(
        "<tr><th>load</th>"
        + "".join(
            f"<td>{result.load_ms.get(n, 0):.0f} ms</td>" for n in names
        )
        + "</tr>"
    )
    body.append(
        "<tr><th>notes</th>"
        + "".join(
            f"<td>{html.escape(str(result.caps[n].get('notes') or '-'))}</td>"
            for n in names
        )
        + "</tr>"
    )
    return (
        f"<table><tr><th></th>{head}</tr>{''.join(body)}</table>"
    )


def _coverage_table(result: Result) -> str:
    if not result.coverage:
        return ""
    names = sorted(result.coverage)
    channels = ["speed_hint", "reaction", "prefix_reaction", "gain_db", "pause_ms"]
    head = "".join(f"<th>{html.escape(n)}</th>" for n in names)
    rows = []
    for ch in channels:
        cells = "".join(
            f"<td>{html.escape(result.coverage[n].get(ch, '-'))}</td>"
            for n in names
        )
        rows.append(f"<tr><th><code>{ch}</code></th>{cells}</tr>")
    return f"<table><tr><th></th>{head}</tr>{''.join(rows)}</table>"


def write_report(result: Result, out_dir: Path, *, seed: int = 7) -> Path:
    rng = random.Random(seed)
    by_phrase: dict[str, list[Row]] = {}
    for row in result.rows:
        by_phrase.setdefault(row.phrase_id, []).append(row)

    blocks = []
    for phrase_id, _text in PHRASES:
        rows = by_phrase.get(phrase_id) or []
        if not rows:
            continue
        # Shuffled per phrase so position cannot be used as a label, and
        # seeded so a reload does not reshuffle under a half-finished vote.
        order = list(rows)
        rng.shuffle(order)
        clips = []
        for index, row in enumerate(order):
            key = f"{row.engine}/{row.voice}"
            warn = (
                f" <span class='warn'>{html.escape(', '.join(row.warnings))}</span>"
                if row.warnings else ""
            )
            clips.append(
                "<div class='clip'>"
                f"<div class='who'>{chr(65 + index)}</div>"
                f"<div class='lab hid' data-blind='clip {chr(65 + index)}' "
                f"data-real='{html.escape(key)}'>clip {chr(65 + index)}</div>"
                f"<audio controls preload='none' src='{html.escape(row.wav)}'></audio>"
                f"<div><button class='vote' data-phrase='{html.escape(phrase_id)}' "
                f"data-key='{html.escape(key)}'>sounds most like her</button>"
                f"<div class='met'>first {row.first_chunk_ms:.0f} ms &middot; "
                f"rtf {row.rtf:.2f} &middot; {row.duration_s:.2f} s &middot; "
                f"{row.sample_rate} Hz{warn}</div></div>"
                "</div>"
            )
        blocks.append(
            f"<div class='phrase'><h2>{html.escape(phrase_id)}</h2>"
            f"<div class='ptext'>{html.escape(rows[0].text)}</div>"
            f"{''.join(clips)}</div>"
        )

    unavailable = ""
    if result.unavailable:
        items = "".join(
            f"<li><code>{html.escape(k)}</code> &mdash; {html.escape(v)}</li>"
            for k, v in sorted(result.unavailable.items())
        )
        unavailable = f"<h2>Would not run</h2><ul class='sub'>{items}</ul>"

    total = len([p for p, _ in PHRASES if by_phrase.get(p)])
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    doc = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>Aiko TTS audition</title><style>{_CSS}</style></head>
<body>
<h1>Aiko TTS audition</h1>
<p class="sub">Generated {stamp}. Labels are hidden and clip order is
shuffled: pick the one that sounds most like her <em>before</em>
revealing, because the familiar voice sounds correct by definition once
you know which one it is.</p>
<div class="bar">
  <button id="reveal">reveal labels</button>
  <button id="clear">clear votes</button>
  <span class="tally" id="tally"></span>
</div>
{''.join(blocks)}
{unavailable}
<h2>Control surface &mdash; what each engine can be told</h2>
<p class="sub">The selection criterion from
<code>docs/tts-engine-options.md</code>: not which engine sounds best,
but which one <code>ProsodyParams</code> can be mapped onto. Gain and
pauses are post-processing and survive any swap; the top three rows are
the ones that need the engine's cooperation.</p>
{_coverage_table(result)}
<h2>Declared capabilities</h2>
<p class="sub">From upstream docs at the time the adapter was written.
<em>verified</em> means someone confirmed it here &mdash; published RTF
and CPU claims are the most optimistic numbers in any model card.</p>
{_caps_table(result)}
<script>const TOTAL = {total};{_JS}</script>
</body></html>
"""
    path = out_dir / "index.html"
    path.write_text(doc, encoding="utf-8")
    (out_dir / "report.json").write_text(
        json.dumps(
            {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "load_ms": result.load_ms,
                "unavailable": result.unavailable,
                "caps": result.caps,
                "coverage": result.coverage,
                "rows": [asdict(r) for r in result.rows],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return path


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--engines", nargs="+", default=None,
        help=f"default: every registered engine ({', '.join(adapters.available())})",
    )
    p.add_argument(
        "--voice-ref", type=Path, default=DEFAULT_REF,
        help="reference clip to clone from (build it with tools.tts_lab.voicebank)",
    )
    p.add_argument(
        "--voice-id", default="",
        help=(
            "also render a named/pre-existing voice, e.g. "
            "aiko1_refined.safetensors -- the incumbent baseline"
        ),
    )
    p.add_argument("--out", type=Path, default=OUT_DIR)
    p.add_argument(
        "--open", action="store_true", help="open the page when done"
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    names = args.engines or adapters.available()

    voice_id = args.voice_id
    if not voice_id:
        from app.core.infra.settings import load_settings

        voice_id = load_settings().tts.pocket_tts_voice

    ref = args.voice_ref if args.voice_ref and args.voice_ref.exists() else None
    if ref is None:
        print(
            f"no reference clip at {args.voice_ref} -- "
            "run 'python -m tools.tts_lab.voicebank' first to build one"
        )

    result = run(
        names,
        voice_ref=ref,
        voice_id=voice_id,
        out_dir=args.out,
    )
    if not result.rows:
        print("nothing synthesised; no report written")
        return 1
    path = write_report(result, args.out)
    print("")
    print(f"{len(result.rows)} clips -> {path.relative_to(REPO_ROOT)}")
    if args.open:
        webbrowser.open(path.resolve().as_uri())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

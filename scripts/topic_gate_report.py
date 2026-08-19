"""What the topic gate's two arms would each decide, and at what threshold.

H43. Five cue providers share one predicate for "is his message about this
cue's subject", and it is the largest single reason Aiko stays quiet
(``topic_miss``, 94.5% of eligible cue declines). This report is the
evidence behind replacing it, and the thing to re-run before touching
``topic_match.DEFAULT_MIN_COSINE``.

It answers three questions, in order of how much they matter:

1. **Is a cosine threshold meaningful for this embedder at all?** Measured
   against a null of random message-against-cue pairs. If the null's p99
   sits above the threshold under consideration, the threshold is noise
   and nothing else in the report means anything.
2. **Does the change cost reach?** The stoplist makes the lexical arm
   stricter, so pairs that used to match on ``and`` now do not. The cosine
   arm has to more than pay for that. Reported as a straight before/after
   on the same pair population, plus the two disagreement classes.
3. **Is what we lose worth losing?** The cosine of the pairs that only the
   old gate accepted. If those sit at the null, they were coincidences.

Usage::

    python -m scripts.topic_gate_report
    python -m scripts.topic_gate_report --messages 400 --null-pairs 4000

Note on what this cannot tell you. Cue availability is not recorded per
turn -- ``state``, ``not_before`` and ``surfaced_count`` are last-value
only -- so this measures the *predicate* over a realistic population of
(subject, message) pairs, not the reach the live pool would have produced.
Reach itself has to come from ``scripts/cue_reach_report.py`` after the
change has run for a few days.
"""
from __future__ import annotations

import argparse
import json
import random
import re
import sqlite3
import statistics as st
import sys
from collections import Counter
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.infra.settings import load_settings  # noqa: E402
from app.core.proactive.topic_match import (  # noqa: E402
    DEFAULT_MIN_COSINE,
    content_words,
    lexical_overlap,
)
from app.llm.embedder import build_embedder  # noqa: E402

# The field each provider hands to the gate, so the lexical arm is measured
# on the same strings production feeds it.
LABEL_KEYS: dict[str, tuple[str, ...]] = {
    "concept_hypothesis": ("label",),
    "interest_drift": ("topic",),
    "knowledge_gap_notice": ("topic",),
    "curiosity_gradient": ("dense_topic", "thin_topic"),
    "associative_wander": ("topic_a", "topic_b"),
}

_OLD_WORD_RE = re.compile(r"[a-z0-9]+")


def old_gate(topic: str, user_text: str) -> bool:
    """The predicate as it stood: one shared token of 3+ characters."""
    tw = {w for w in _OLD_WORD_RE.findall((topic or "").lower()) if len(w) >= 3}
    if not tw:
        return False
    uw = {
        w for w in _OLD_WORD_RE.findall((user_text or "").lower())
        if len(w) >= 3
    }
    return bool(tw & uw)


def _unpack(blob: Any) -> Any:
    import numpy as np

    if blob is None:
        return None
    arr = np.frombuffer(blob, dtype=np.float32)
    n = float(np.linalg.norm(arr))
    return arr / n if n else None


def _label_for(cue_type: str, payload: dict[str, Any], subject: str) -> str:
    parts = [
        str(payload.get(k) or "") for k in LABEL_KEYS.get(cue_type, ())
    ]
    joined = " ".join(p for p in parts if p).strip()
    return joined or subject


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default="data/chat_sessions.db")
    ap.add_argument("--messages", type=int, default=400)
    ap.add_argument("--null-pairs", type=int, default=4000)
    ap.add_argument("--seed", type=int, default=11)
    args = ap.parse_args()

    import numpy as np

    random.seed(args.seed)
    con = sqlite3.connect(Path(args.db))
    con.row_factory = sqlite3.Row

    settings = load_settings()
    names = [
        str(getattr(getattr(settings, "assistant", None), "name", "") or ""),
        str(getattr(getattr(settings, "assistant", None), "user_name", "") or ""),
    ]
    extra_stop = [n.lower() for n in names if n]
    print(f"names treated as stopwords: {extra_stop or '(none found)'}")

    # ── the population: real cue subjects x real user messages ──
    cues: list[tuple[str, str, Any]] = []
    for r in con.execute(
        "SELECT cue_type, subject, payload, embedding FROM cue_pool "
        "WHERE cue_type IN ({}) AND embedding IS NOT NULL".format(
            ",".join("?" * len(LABEL_KEYS))
        ),
        tuple(LABEL_KEYS),
    ):
        try:
            payload = json.loads(r["payload"] or "{}")
        except Exception:
            payload = {}
        vec = _unpack(r["embedding"])
        if vec is None:
            continue
        label = _label_for(r["cue_type"], payload, str(r["subject"] or ""))
        if label.strip():
            cues.append((r["cue_type"], label, vec))

    msgs = [
        str(r["content"] or "") for r in con.execute(
            "SELECT content FROM messages WHERE role='user' "
            "AND length(content) BETWEEN 12 AND 900 "
            "ORDER BY id DESC LIMIT ?", (int(args.messages),),
        )
    ]
    print(f"cues with embeddings: {len(cues)}   user messages: {len(msgs)}")
    if not cues or not msgs:
        print("not enough data")
        return 1

    emb = build_embedder(settings.llm)
    print("embedding messages...")
    mv: dict[str, Any] = {}
    for i, m in enumerate(msgs):
        try:
            mv[m] = emb.embed(m.strip()[:2000])
        except Exception as exc:  # pragma: no cover - operational
            print(f"  embed failed: {exc}")
        if (i + 1) % 100 == 0:
            print(f"  {i+1}/{len(msgs)}")
    msgs = [m for m in msgs if m in mv]

    # ── 1. is a threshold meaningful for this embedder? ──
    null = []
    for _ in range(int(args.null_pairs)):
        m = random.choice(msgs)
        _ct, _lbl, cv = random.choice(cues)
        null.append(float(np.dot(mv[m], cv)))
    null.sort()

    def pct(vals: list[float], p: float) -> float:
        return vals[min(len(vals) - 1, max(0, int(p * len(vals)) - 1))]

    print("\n1. NULL -- random message x random cue subject")
    print(f"   n={len(null)} median={st.median(null):.3f} "
          f"p90={pct(null,.90):.3f} p95={pct(null,.95):.3f} "
          f"p99={pct(null,.99):.3f} max={null[-1]:.3f}")
    print("   share of unrelated pairs clearing each candidate threshold:")
    for thr in (0.45, 0.50, 0.55, 0.60, 0.65):
        n = sum(1 for c in null if c >= thr)
        flag = "  <-- default" if abs(thr - DEFAULT_MIN_COSINE) < 1e-9 else ""
        print(f"     >={thr:.2f}: {100*n/len(null):5.2f}%{flag}")

    # ── 2/3. arm-by-arm over the full pair population ──
    print("\n2. THE TWO ARMS over every (subject, message) pair")
    header = (
        f"   {'threshold':>9s} {'old':>7s} {'lex':>7s} {'cos':>7s} "
        f"{'new':>7s} {'lost':>7s} {'gained':>7s}"
    )
    lost_cos: list[float] = []
    for thr in (0.45, 0.50, 0.55, 0.60, 0.65):
        pairs = old_hit = lex_hit = cos_hit = new_hit = lost = gained = 0
        for _ct, label, cv in cues:
            lw = content_words(label, extra_stop=extra_stop)
            for m in msgs:
                pairs += 1
                o = old_gate(label, m)
                lx = bool(lw and lexical_overlap(
                    label, m, extra_stop=extra_stop,
                ))
                c = float(np.dot(mv[m], cv)) >= thr
                n = lx or c
                old_hit += o
                lex_hit += lx
                cos_hit += c
                new_hit += n
                if o and not n:
                    lost += 1
                    if abs(thr - DEFAULT_MIN_COSINE) < 1e-9:
                        lost_cos.append(float(np.dot(mv[m], cv)))
                if n and not o:
                    gained += 1
        if thr == 0.45:
            print(header)
        print(
            f"   {thr:9.2f} {100*old_hit/pairs:6.1f}% {100*lex_hit/pairs:6.1f}% "
            f"{100*cos_hit/pairs:6.1f}% {100*new_hit/pairs:6.1f}% "
            f"{100*lost/pairs:6.1f}% {100*gained/pairs:6.1f}%"
        )
    print("   old=gate as shipped  lex=stoplisted arm  cos=semantic arm")
    print("   new=lex OR cos   lost=old accepted, new rejects")
    print("   gained=new accepts, old rejected")

    # ── 3. was what we lose worth keeping? ──
    if lost_cos:
        lost_cos.sort()
        print(f"\n3. WHAT THE STOPLIST DROPS at {DEFAULT_MIN_COSINE:.2f} "
              f"(n={len(lost_cos)})")
        print(f"   cosine median={st.median(lost_cos):.3f} "
              f"p90={pct(lost_cos,.90):.3f}")
        print(f"   null   median={st.median(null):.3f}   <- compare these two")
        above = sum(1 for c in lost_cos if c >= pct(null, .95))
        print(f"   of the dropped pairs, {100*above/len(lost_cos):.1f}% sit "
              f"above the null's p95")

    # ── which words the stoplist actually removed ──
    killed: Counter[str] = Counter()
    for _ct, label, _cv in cues:
        for m in msgs[:120]:
            old_shared = (
                {w for w in _OLD_WORD_RE.findall(label.lower()) if len(w) >= 3}
                & {w for w in _OLD_WORD_RE.findall(m.lower()) if len(w) >= 3}
            )
            new_shared = lexical_overlap(label, m, extra_stop=extra_stop)
            killed.update(old_shared - new_shared)
    if killed:
        print("\n   top tokens the stoplist stopped matching on:")
        for w, n in killed.most_common(12):
            print(f"     {n:7d}  {w}")

    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Read a P44 prompt-cache session back: where did the prefix break?

Turn on ``logging.prompt_cache_log_enabled``, have a normal conversation,
then run this. It answers the question the raw records cannot at a
glance: which block ends the cacheable prefix, how much that costs, and
whether the provider's own cache figures agree with our prediction.

Run: ``python scripts/prefix_break_report.py [path]``
     (defaults to ``data/prompt-cache.jsonl``)

Reading the output:

* **Divergence by block** — a single block dominating means one cheap
  fix is available. A flat spread means the prompt is churning
  everywhere and re-ordering will not save it.
* **Ladder discipline** — the break tells you what this turn cost, but
  it also hides everything behind it: while a T0 block churns you cannot
  see whether the lower tiers behave. This section counts every block
  that moved, so the tier contract (T0 rarest, rising to T6) is a number
  you can read rather than a claim in a comment. An inverted pair is
  flagged: it means a block is filed in a tier more stable than it
  actually is, and moving it down the ladder is the fix.
* **cached_tokens vs our prediction** — if the break point is stable
  while the provider's cache hit rate swings wildly, the misses are the
  provider's routing or TTL and no prompt restructuring will help. If
  they move together, the break is ours to fix.
* **History** — ``slid`` is a window shift (expected; it leaves a stable
  tail). ``churn`` means retained messages were rewritten in place,
  which is what relative-age prefixes do and is far more damaging.
* **Estimator error** — the per-block breakdown the UI renders is only
  as good as this number.
"""
from __future__ import annotations

import json
import pathlib
import statistics
import sys
from collections import Counter


DEFAULT_PATH = pathlib.Path("data/prompt-cache.jsonl")


def load(path: pathlib.Path) -> list[dict]:
    records: list[dict] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                # A torn final line is normal if the app is still running.
                continue
            if isinstance(payload, dict):
                records.append(payload)
    return records


def _mean(values: list[float]) -> float:
    return statistics.fmean(values) if values else 0.0


def _histogram(title: str, counts: Counter, total: int) -> None:
    print(f"\n{title}")
    if not counts:
        print("  (nothing recorded)")
        return
    width = max(len(str(name)) for name, _ in counts.most_common())
    for name, count in counts.most_common():
        pct = 100.0 * count / total if total else 0.0
        bar = "#" * int(round(pct / 2.5))
        print(f"  {str(name):<{width}}  {count:>4}  {pct:5.1f}%  {bar}")


def _ladder() -> tuple[list[str], dict[str, str], list[str]]:
    """``(block order, block -> tier, tier order)`` from the live assembler.

    Imported lazily and best-effort: the report stays runnable against a
    JSONL file copied off a machine that has no checkout.
    """
    try:
        from app.core.session.prompt_assembler import _PROMPT_BLOCK_TIERS
    except Exception:
        return [], {}, []
    order: list[str] = []
    tier_of: dict[str, str] = {}
    for tier, names in _PROMPT_BLOCK_TIERS.items():
        for name in names:
            order.append(name)
            tier_of[name] = tier
    return order, tier_of, list(_PROMPT_BLOCK_TIERS)


def _ladder_discipline(turns: list[dict]) -> None:
    """Per-block change frequency, grouped by tier, plus the ordering verdict."""
    recorded = [r for r in turns if "changed_blocks" in r]
    if not recorded:
        print("\nLadder discipline")
        print(
            "  (not recorded in this file -- predates the per-block churn "
            "instrumentation; only the earliest break was captured)",
        )
        return

    order, tier_of, tier_order = _ladder()
    per_block: Counter = Counter()
    for record in recorded:
        for name in record.get("changed_blocks") or ():
            per_block[str(name)] += 1
    # A tier counts as changed on a turn if any of its blocks moved. The
    # assembler already grouped this for us.
    per_tier: Counter = Counter()
    for record in recorded:
        for tier, count in (record.get("changed_by_tier") or {}).items():
            if count:
                per_tier[str(tier)] += 1

    total = len(recorded)
    print(f"\nLadder discipline -- how often each block changed ({total} turns)")
    print("  the contract: T0 rarest, rising monotonically to T6")

    tiers = tier_order or sorted(
        {tier_of.get(n, "unknown") for n in per_block} | set(per_tier),
    )
    for tier in tiers:
        rate = 100.0 * per_tier.get(tier, 0) / total
        bar = "#" * int(round(rate / 2.5))
        print(f"\n  {tier:<16} any block moved  {rate:5.1f}%  {bar}")
        names = [n for n in order if tier_of.get(n) == tier] or sorted(
            n for n in per_block if tier_of.get(n, "unknown") == tier
        )
        movers = [(n, per_block.get(n, 0)) for n in names if per_block.get(n)]
        if not movers:
            print("      (every block held still)")
            continue
        for name, count in sorted(movers, key=lambda kv: -kv[1]):
            print(f"      {name:<28} {count:>4}  {100.0 * count / total:5.1f}%")

    # The verdict. Only tiers that actually moved are ranked against each
    # other: a silent tier is perfectly stable, so "T5 churns more than a
    # T6 that never fired" is not a filing error and must not be reported
    # as one. Each active tier is compared to the next *active* one down.
    ranked = [
        (t, 100.0 * per_tier.get(t, 0) / total)
        for t in tiers
        if per_tier.get(t, 0)
    ]
    inversions = [
        (a, ra, b, rb)
        for (a, ra), (b, rb) in zip(ranked, ranked[1:], strict=False)
        if ra > rb + 1e-9
    ]
    print("\n  verdict:")
    if not inversions:
        print("      ladder holds -- every tier is at least as stable as the next")
        return
    for a, ra, b, rb in inversions:
        print(f"      INVERTED  {a} ({ra:.1f}%) churns more than {b} ({rb:.1f}%)")
    worst = max(inversions, key=lambda row: row[1] - row[3])
    culprits = [
        (n, c) for n, c in per_block.items()
        if tier_of.get(n) == worst[0]
    ]
    if culprits:
        name, count = max(culprits, key=lambda kv: kv[1])
        print(
            f"      biggest offender: {name} moved on {100.0 * count / total:.1f}% "
            f"of turns while filed under {worst[0]}",
        )


def report(records: list[dict]) -> None:
    # First turns carry no divergence signal -- there was nothing to
    # compare against -- so they would drag every mean toward zero.
    turns = [r for r in records if not r.get("first_turn")]
    print(f"records: {len(records)}  comparable turns: {len(turns)}")
    if not turns:
        print("\nNothing to report yet. Have a longer conversation.")
        return

    models = Counter(str(r.get("model") or "?") for r in turns)
    print("models: " + ", ".join(f"{m} x{c}" for m, c in models.most_common()))

    identical = sum(1 for r in turns if not r.get("diverged"))
    print(
        f"byte-identical system prompts: {identical} "
        f"({100.0 * identical / len(turns):.1f}%)",
    )

    _histogram(
        "Divergence by block (earliest change, the one that matters)",
        Counter(str(r.get("diverged") or "(none)") for r in turns),
        len(turns),
    )
    _histogram(
        "Divergence by tier",
        Counter(str(r.get("tier") or "(none)") for r in turns),
        len(turns),
    )

    _ladder_discipline(turns)

    broken = [r for r in turns if r.get("diverged")]
    if broken:
        print("\nCost of the break")
        print(
            f"  lost_chars   mean {_mean([float(r.get('lost_chars', 0)) for r in broken]):>9,.0f}"
            f"   max {max(int(r.get('lost_chars', 0)) for r in broken):>9,}",
        )
        print(
            f"  lost_pct     mean {_mean([float(r.get('lost_pct', 0.0)) for r in broken]):>9.1f}%",
        )
        print(
            f"  blocks moved mean {_mean([float(r.get('changed', 0)) for r in broken]):>9.1f}",
        )

    print("\nHistory")
    slid = [r for r in turns if int(r.get("history_slid", 0)) > 0]
    churn = [r for r in turns if int(r.get("history_slid", 0)) < 0]
    stable = len(turns) - len(slid) - len(churn)
    print(f"  prefix intact  {stable:>4}")
    print(f"  window slid    {len(slid):>4}  (expected; leaves a stable tail)")
    print(
        f"  rewritten      {len(churn):>4}  "
        "(messages changed in place -- the age-prefix fingerprint)",
    )

    print("\nProvider cache (the ground truth our prediction is checked against)")
    cached = [float(r.get("cached_pct", 0.0)) for r in turns]
    cached_tokens = [int(r.get("cached_tokens", 0)) for r in turns]
    print(f"  cached_pct   mean {_mean(cached):>9.1f}%   max {max(cached):>7.1f}%")
    print(
        f"  cached_tokens mean {_mean([float(t) for t in cached_tokens]):>8,.0f}"
        f"   max {max(cached_tokens):>9,}",
    )
    distinct = Counter(cached_tokens)
    if len(distinct) <= 6:
        # A handful of distinct values (rather than a smooth spread) is
        # itself the finding: it means the cache is hitting or missing
        # wholesale rather than tracking our prompt edits.
        print(
            "  distinct values: "
            + ", ".join(f"{v:,} x{c}" for v, c in distinct.most_common()),
        )

    print("\nEstimator accuracy (drives the per-block breakdown in the UI)")
    errors = [float(r.get("est_error_pct", 0.0)) for r in turns]
    print(f"  est_error_pct  mean {_mean(errors):>7.1f}%")
    print(
        f"  worst over-estimate {max(errors):>6.1f}%   "
        f"worst under-estimate {min(errors):>6.1f}%",
    )
    ratios = [
        float(r["chars_per_token"]) for r in turns
        if r.get("chars_per_token") is not None
    ]
    if ratios:
        print(
            f"  chars_per_token  first {ratios[0]:.3f} -> last {ratios[-1]:.3f} "
            "(resets to 3.5 on restart)",
        )

    estimated = sum(1 for r in turns if r.get("eval_estimated"))
    if estimated:
        print(
            f"\ntok/s derived from wall clock on {estimated}/{len(turns)} turns "
            "(provider reported no generation timer)",
        )


def main(argv: list[str]) -> int:
    path = pathlib.Path(argv[1]) if len(argv) > 1 else DEFAULT_PATH
    if not path.exists():
        print(f"no such file: {path}")
        print(
            "Enable it with logging.prompt_cache_log_enabled = true, then "
            "have a conversation.",
        )
        return 1
    report(load(path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

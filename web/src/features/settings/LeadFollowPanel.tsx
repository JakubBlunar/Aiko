import { useCallback, useEffect, useState } from "react";
import { api } from "../../api";
import type { LeadFollowCohort, LeadFollowSnapshot } from "../../types";

/** Blocks this family is meant to move, in the order worth reading. */
const LEAD_BLOCKS = [
  "initiative_block",
  "wants_block",
  "thread_ownership_block",
  "taste_lean_block",
  "pursuit_lean_block",
  "topic_appetite_block",
  "style_pattern_block",
  "curiosity_seeds_block",
  "away_activities_block",
  "turning_over_block",
];

function pct(value: number | undefined): string {
  if (value === undefined || Number.isNaN(value)) return "–";
  return `${Math.round(value * 100)}%`;
}

function windowLabel(days: number | null): string {
  return days === null ? "all time" : `${days}d`;
}

export function LeadFollowPanel() {
  const [data, setData] = useState<LeadFollowSnapshot | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setData(await api.getLeadFollow());
    } catch (err) {
      setError(String(err));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const cohorts = data?.cohorts ?? [];
  const newest = cohorts[0];

  return (
    <div className="mt-4 space-y-2 rounded-md border border-white/5 bg-white/[0.02] p-3">
      <div className="flex items-center justify-between gap-2 text-[11px]">
        <span
          className="font-medium text-ink-100/70"
          title="K90 — is she leading the conversation or following it? Computed from the message log every time you open this, so there is no snapshot to go stale. The text metrics are retroactive; the block firing rates below are not."
        >
          Lead / follow
          {newest ? (
            <span className="ml-2 text-ink-100/50">
              {pct(newest.anaphoric_opener_rate)} anaphoric
            </span>
          ) : null}
        </span>
        <button
          type="button"
          onClick={refresh}
          disabled={loading}
          className="rounded border border-white/10 px-2 py-0.5 hover:border-ink-400 disabled:opacity-40"
        >
          {loading ? "reading…" : "refresh"}
        </button>
      </div>

      {error ? (
        <div className="rounded border border-rose-400/40 bg-rose-500/10 px-2 py-1 text-[11px] text-rose-200">
          {error}
        </div>
      ) : null}

      {data?.error ? (
        <p className="text-[11px] text-ink-100/40">
          Unavailable: <code className="text-ink-100/60">{data.error}</code>
        </p>
      ) : cohorts.length === 0 ? (
        <p className="text-[11px] text-ink-100/40">
          {loading ? "Reading the message log…" : "No turns to measure yet."}
        </p>
      ) : (
        <>
          <table className="w-full text-[11px] tabular-nums">
            <thead>
              <tr className="text-[9px] uppercase tracking-wide text-ink-100/30">
                <th className="py-0.5 text-left font-normal">window</th>
                <th className="py-0.5 text-right font-normal">turns</th>
                <th className="py-0.5 text-right font-normal" title="Share of replies that closed on a question. High means she is interviewing him.">
                  ends-Q
                </th>
                <th className="py-0.5 text-right font-normal" title="Median words per reply.">
                  words
                </th>
                <th className="py-0.5 text-right font-normal" title="Her first sentence needed his to stand up — 'Then…', 'Exactly.', 'That makes sense'. The following tell.">
                  anaph
                </th>
                <th className="py-0.5 text-right font-normal" title="Share of her opening content words taken straight from his message.">
                  echo
                </th>
                <th className="py-0.5 text-right font-normal" title="Share of her content words that were hers rather than recycled from his turn or the recent history. The one number here you want going up.">
                  own
                </th>
              </tr>
            </thead>
            <tbody>
              {cohorts.map((cohort) => (
                <CohortRow key={windowLabel(cohort.window_days)} cohort={cohort} />
              ))}
            </tbody>
          </table>

          <p className="text-[10px] leading-snug text-ink-100/35">
            <span className="text-ink-100/50">anaph</span> and{" "}
            <span className="text-ink-100/50">echo</span> want to go down,{" "}
            <span className="text-ink-100/50">own</span> up.{" "}
            <span className="text-ink-100/50">own</span> is lexical only —
            elaborating on his subject scores there too, so read it as "did she
            bring anything", not "did she change the subject".
          </p>

          {newest ? <BlockSection cohort={newest} /> : null}
        </>
      )}
    </div>
  );
}

function CohortRow({ cohort }: { cohort: LeadFollowCohort }) {
  return (
    <tr className="border-t border-white/5 text-ink-100/70">
      <td className="py-0.5 text-left text-ink-100/50">
        {windowLabel(cohort.window_days)}
      </td>
      <td className="py-0.5 text-right">{cohort.turns}</td>
      <td className="py-0.5 text-right">{pct(cohort.question_end_rate)}</td>
      <td className="py-0.5 text-right">{Math.round(cohort.median_words)}</td>
      <td className="py-0.5 text-right">{pct(cohort.anaphoric_opener_rate)}</td>
      <td className="py-0.5 text-right">{pct(cohort.mean_opener_echo)}</td>
      <td className="py-0.5 text-right">{pct(cohort.mean_own_material)}</td>
    </tr>
  );
}

function BlockSection({ cohort }: { cohort: LeadFollowCohort }) {
  const blocks = cohort.blocks;
  if (!blocks?.available) {
    // Deliberately a sentence rather than a table of zeroes: a block
    // that has never been recorded and a block that never fires are the
    // same zero, and rendering it would read as the latter.
    return (
      <div className="rounded border border-white/5 bg-white/[0.02] px-2 py-1.5 text-[10px] leading-snug text-ink-100/40">
        <span className="text-ink-100/60">Lead-cue firing</span> not available
        yet — {blocks?.reason || "no data"}. Unlike the metrics above, this is
        recorded per turn as it happens, so it only accrues from here on.
      </div>
    );
  }
  const byName = new Map(blocks.blocks.map((b) => [b.block, b]));
  return (
    <div className="space-y-0.5">
      <div className="text-[9px] uppercase tracking-wide text-ink-100/30">
        lead-cue firing · per 100 turns · {blocks.turns} recorded
      </div>
      {LEAD_BLOCKS.map((name) => {
        const row = byName.get(name);
        return (
          <div
            key={name}
            className="flex items-baseline justify-between gap-2 text-[10px]"
          >
            <span className="truncate text-ink-100/50">{name}</span>
            <span
              className={
                row ? "shrink-0 tabular-nums text-ink-100/70" : "shrink-0 text-ink-100/25"
              }
            >
              {row ? row.per_hundred_turns.toFixed(1) : "never"}
            </span>
          </div>
        );
      })}
    </div>
  );
}

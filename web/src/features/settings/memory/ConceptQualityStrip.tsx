import { useCallback, useState } from "react";
import { api } from "../../../api";
import type { ConceptQualityReport } from "../../../types";
import { useAsyncResource } from "@/hooks/useAsyncResource";
import { RefreshButton } from "@/components/RefreshButton";

/**
 * L22 quality scoreboard — a compact health readout above the concept
 * list. Where the list answers "what does Aiko believe?", this answers
 * "is the layer that formed those beliefs working?".
 *
 * Everything here is advisory. Nothing on this panel demotes, retires or
 * deletes a concept; the L3 lifecycle worker remains the single writer.
 */
export function ConceptQualityStrip() {
  const loader = useCallback(() => api.getConceptQuality(), []);
  const { data, loading, error, refresh } =
    useAsyncResource<ConceptQualityReport | null>(loader, null);
  const [open, setOpen] = useState(false);

  if (error) {
    return (
      <div className="rounded border border-white/10 bg-white/[0.02] p-2 text-[10px] text-ink-100/40">
        Quality report unavailable: {error}
      </div>
    );
  }
  if (!data || !data.enabled) return null;

  const { flow, pruning, evidence, duplicates } = data;
  const promotionRate = flow.promotion_rate_pct ?? 0;
  const demotions = flow.demotion_events ?? 0;
  const unreinforced = pruning.unreinforced_pct ?? 0;
  const belowBar = evidence.active_below_bar ?? 0;

  return (
    <div className="rounded border border-white/10 bg-white/[0.02] p-2">
      <div className="flex items-center justify-between gap-2">
        <button
          type="button"
          onClick={() => setOpen((v) => !v)}
          aria-expanded={open}
          className="flex flex-1 flex-wrap items-center gap-x-3 gap-y-1 text-left text-[10px]"
        >
          <span className="uppercase tracking-wide text-ink-100/40">
            layer health
          </span>
          <Stat
            label="promoted"
            value={`${promotionRate.toFixed(0)}%`}
            // A promotion rate near 100 means the gate is waving
            // everything through, not that the proposer is brilliant.
            warn={promotionRate >= 80}
            title="Share of proposed concepts that became active beliefs. Near 100% means the promotion gate is not discriminating."
          />
          <Stat
            label="stalled"
            value={`${unreinforced.toFixed(0)}%`}
            warn={unreinforced >= 50}
            title="Active concepts never reinforced since promotion. High means beliefs are minted once and never earn their place again."
          />
          <Stat
            label="demotions"
            value={String(demotions)}
            warn={demotions === 0 && promotionRate > 0}
            title="Total dormant + retired events. Zero alongside active promotion means the graph only ever grows."
          />
          <Stat
            label="dupes"
            value={String(duplicates.pair_count)}
            warn={duplicates.pair_count > 0}
            title="Paraphrase twins sitting just under the creation-time dedupe bar, so they landed as separate rows."
          />
          {belowBar > 0 ? (
            <Stat
              label="under-bar"
              value={String(belowBar)}
              warn
              title="Active concepts holding less evidence than the promotion bar they passed."
            />
          ) : null}
          {flow.concepts_per_day != null ? (
            <span className="text-ink-100/35">
              {flow.concepts_per_day.toFixed(1)}/day
            </span>
          ) : null}
          <span className="text-ink-100/25">{open ? "hide" : "details"}</span>
        </button>
        <RefreshButton onClick={() => void refresh()} loading={loading} />
      </div>

      {open ? <QualityDetail report={data} /> : null}
    </div>
  );
}

function Stat({
  label,
  value,
  warn,
  title,
}: {
  label: string;
  value: string;
  warn?: boolean;
  title: string;
}) {
  return (
    <span title={title} className="whitespace-nowrap">
      <span className="text-ink-100/35">{label} </span>
      <span className={warn ? "text-amber-200/80" : "text-ink-100/70"}>
        {value}
      </span>
    </span>
  );
}

function QualityDetail({ report }: { report: ConceptQualityReport }) {
  const registerRows = Object.entries(report.register);
  const horizon = report.pruning.median_engaged_days_to_dormant;
  const windowDays = report.pruning.recent_window_days;
  const promotedRecent = report.pruning.promoted_recent;
  const stalledRecent = report.pruning.unreinforced_recent ?? 0;
  const stalledRecentPct = report.pruning.unreinforced_recent_pct ?? 0;

  return (
    <div className="mt-2 space-y-2 border-t border-white/5 pt-2 text-[10px]">
      <div className="text-ink-100/45">
        {report.pruning.unreinforced_since_promotion ?? 0} of{" "}
        {report.pruning.active ?? 0} active concepts have not been reinforced
        since promotion
        {horizon != null ? (
          <>
            {" "}
            — at the current half-life, decay would need a median of{" "}
            <span className="text-amber-200/80">{horizon}</span> engaged days
            (roughly conversation hours) to demote them
          </>
        ) : null}
        .
      </div>

      {/* The stock figure above is slow by construction, so it cannot show
          whether a threshold change worked. This is the flow that can. */}
      {windowDays != null && promotedRecent != null ? (
        <div
          className="text-ink-100/45"
          title="Recently promoted concepts that have already gone quiet. Reads high in absolute terms — a concept promoted yesterday has barely had a chance to be reinforced — so compare it against the same figure measured before a threshold change, not against zero."
        >
          Last {windowDays} days: {promotedRecent} promoted
          {promotedRecent > 0 ? (
            <>
              ,{" "}
              <span
                className={
                  stalledRecentPct >= 60
                    ? "text-amber-200/80"
                    : "text-ink-100/70"
                }
              >
                {stalledRecent} ({stalledRecentPct.toFixed(0)}%)
              </span>{" "}
              already unreinforced
            </>
          ) : null}
          .
        </div>
      ) : null}

      {evidenceNotes(report).map((note) => (
        <div key={note} className="text-ink-100/45">
          {note}
        </div>
      ))}

      {registerRows.length > 0 ? (
        <div>
          <div className="mb-1 uppercase tracking-wide text-ink-100/35">
            label register (per kind — never compare across kinds)
          </div>
          <ul className="space-y-0.5">
            {registerRows.map(([key, entry]) => (
              <li key={key} className="flex flex-wrap items-baseline gap-x-2">
                <span className="text-ink-100/70">{key}</span>
                <span className="text-ink-100/30">n={entry.n}</span>
                <span
                  className={
                    entry.frame_pct >= 50
                      ? "text-amber-200/80"
                      : "text-ink-100/45"
                  }
                  title="Labels imposing an interpretive frame ('treats X as Y'). Expected to be non-zero for interpretive kinds like value or tension."
                >
                  frame {entry.frame_pct}%
                </span>
                <span
                  className={
                    entry.jargon_pct >= 30
                      ? "text-amber-200/80"
                      : "text-ink-100/45"
                  }
                  title="Labels importing vocabulary from an unrelated (engineering) domain to describe ordinary behaviour."
                >
                  jargon {entry.jargon_pct}%
                </span>
                <span
                  className="text-ink-100/30"
                  title="Most common opening words and how many labels share them."
                >
                  “{entry.top_lead_ngram}” {entry.top_lead_pct}%
                </span>
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      {report.duplicates.pairs.length > 0 ? (
        <div>
          <div className="mb-1 uppercase tracking-wide text-ink-100/35">
            near-duplicate pairs ({report.duplicates.pair_count})
          </div>
          <ul className="space-y-1">
            {report.duplicates.pairs.slice(0, 8).map((pair) => (
              <li
                key={`${pair.a.id}-${pair.b.id}`}
                className="rounded border border-white/5 bg-white/[0.02] p-1.5"
              >
                <div className="text-ink-100/30">
                  {pair.kind}/{pair.subject} · cos {pair.cosine.toFixed(3)}
                </div>
                <div className="break-words text-ink-100/60">
                  #{pair.a.id} {pair.a.label}
                </div>
                <div className="break-words text-ink-100/60">
                  #{pair.b.id} {pair.b.label}
                </div>
              </li>
            ))}
          </ul>
        </div>
      ) : null}
    </div>
  );
}

/** Only surface the spurious-concept signals that actually fired, so the
 *  detail panel stays a list of problems rather than a wall of zeroes. */
function evidenceNotes(report: ConceptQualityReport): string[] {
  const notes: string[] = [];
  const { evidence } = report;
  const zero = evidence.active_zero_source ?? 0;
  const singleCluster = evidence.single_cluster_active ?? 0;
  const weak = evidence.weak_memory_active ?? 0;

  if (zero > 0) {
    notes.push(
      `${zero} active concept${zero === 1 ? "" : "s"} rest on no evidence ` +
        `at all — their edges were reconciled away without the status ` +
        `being re-gated.`,
    );
  }
  if (singleCluster > 0) {
    notes.push(
      `${singleCluster} active concept${singleCluster === 1 ? "" : "s"} ` +
        `draw all their evidence from a single topic cluster, which makes ` +
        `them a topic rather than a cross-cutting concept.`,
    );
  }
  if (weak > 0) {
    notes.push(
      `${weak} active concept${weak === 1 ? "" : "s"} rest on memories ` +
        `that are themselves low-confidence.`,
    );
  }
  return notes;
}

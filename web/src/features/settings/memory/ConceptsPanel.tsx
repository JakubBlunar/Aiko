import { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "../../../api";
import type { ConceptRow, ConceptsSnapshot } from "../../../types";
import { formatRelative } from "../SettingsSection";
import { useAsyncResource } from "@/hooks/useAsyncResource";
import { Panel } from "@/components/Panel";
import { RefreshButton } from "@/components/RefreshButton";
import { ErrorBanner } from "@/components/ErrorBanner";
import { EmptyState } from "@/components/EmptyState";
import { ConceptQualityStrip } from "./ConceptQualityStrip";

const SUBJECT_ALL = "all";
const STATUS_ALL = "all";

/** Matches the memories list. Concept rows are much heavier than memory
 *  rows — the snapshot resolves every evidence edge to its full source
 *  text — so this is the difference between a ~90 KB page and a graph
 *  dump that ran to 1.5 MB and hundreds of cards in one commit. */
const CONCEPT_PAGE_SIZE = 50;

export function ConceptsPanel() {
  const [statusFilter, setStatusFilter] = useState<string>(STATUS_ALL);
  const [subjectFilter, setSubjectFilter] = useState<string>(SUBJECT_ALL);
  const [page, setPage] = useState(0);

  // Filtering is server-side so paging walks the filtered set rather than
  // hiding rows out of a page that was already fetched whole.
  const loader = useCallback(
    () =>
      api.getConcepts({
        limit: CONCEPT_PAGE_SIZE,
        offset: page * CONCEPT_PAGE_SIZE,
        status: statusFilter === STATUS_ALL ? undefined : statusFilter,
        subject: subjectFilter === SUBJECT_ALL ? undefined : subjectFilter,
      }),
    [page, statusFilter, subjectFilter],
  );
  const { data, loading, error, setError, refresh } =
    useAsyncResource<ConceptsSnapshot | null>(loader, null);

  const [expanded, setExpanded] = useState<Set<number>>(new Set());
  const [running, setRunning] = useState(false);
  const [runResult, setRunResult] = useState<string | null>(null);

  const toggle = useCallback((id: number) => {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }, []);

  const handleRun = useCallback(async () => {
    setRunning(true);
    setRunResult(null);
    setError(null);
    try {
      const { result } = await api.runConceptSynthesis();
      const added = Number(result?.added ?? 0);
      const reinforced = Number(result?.reinforced ?? 0);
      setRunResult(`added ${added} · reinforced ${reinforced}`);
      await refresh();
    } catch (err) {
      setError(String(err));
    } finally {
      setRunning(false);
    }
  }, [refresh, setError]);

  const handleDelete = useCallback(
    async (concept: ConceptRow) => {
      if (
        !window.confirm(
          `Delete concept "${concept.label}"?\n\nThis removes only the ` +
            `concept and its evidence links. The underlying memories are ` +
            `left untouched.`,
        )
      ) {
        return;
      }
      try {
        await api.deleteConcept(concept.id);
        await refresh();
      } catch (err) {
        setError(String(err));
      }
    },
    [refresh, setError],
  );

  const enabled = data?.enabled ?? true;
  const visible = data?.concepts ?? [];
  const byStatus = data?.counts.by_status ?? {};
  const bySubject = data?.counts.by_subject ?? {};
  // ``counts`` describes the whole store on every page, so the pills stay
  // put instead of shrinking to whatever this page happens to hold.
  const matched = data?.matched ?? visible.length;
  const pageCount = Math.max(1, Math.ceil(matched / CONCEPT_PAGE_SIZE));

  const statusOptions = useMemo(
    () => [STATUS_ALL, ...Object.keys(byStatus).sort()],
    [byStatus],
  );
  const subjectOptions = useMemo(
    () => [SUBJECT_ALL, ...Object.keys(bySubject).sort()],
    [bySubject],
  );

  // Deleting the last row of the final page (or landing on a stale page
  // after a synthesis run) would otherwise strand the user on an empty
  // list with no way back but Prev.
  useEffect(() => {
    if (page > 0 && page >= pageCount) setPage(pageCount - 1);
  }, [page, pageCount]);

  const changeFilter = useCallback((apply: () => void) => {
    apply();
    setPage(0);
  }, []);

  if (data && !enabled) {
    return (
      <Panel className="text-[11px] text-ink-100/40">
        The concept layer is disabled. Enable
        <code className="mx-1">concepts_enabled</code>
        in agent settings to let Aiko synthesise higher-order concepts, then
        run synthesis here to populate candidates.
      </Panel>
    );
  }

  const statusSummary = Object.entries(byStatus)
    .map(([k, n]) => `${k} ${n}`)
    .join(" · ");

  return (
    <Panel>
      <div className="flex items-center justify-between gap-2 text-[11px]">
        <span
          className="font-medium text-ink-100/70"
          title="Higher-order concepts Aiko has synthesised from her topic clusters (user identity) and her own self/reflection/diary memories (aiko identity). Candidates are proposed by the L2 worker; promotion to active is L3's job."
        >
          Concepts
          {data ? (
            <span className="ml-2 text-ink-100/40">
              ({matched < data.total ? `${matched} of ${data.total}` : data.total}
              {statusSummary ? ` — ${statusSummary}` : ""})
            </span>
          ) : null}
        </span>
        <div className="flex items-center gap-1">
          {runResult ? (
            <span className="text-[10px] text-emerald-200/70">{runResult}</span>
          ) : null}
          <RefreshButton
            onClick={() => void handleRun()}
            loading={running}
            label="run synthesis"
            title="Force one concept-synthesis pass now instead of waiting for the idle worker."
          />
          <RefreshButton onClick={() => void refresh()} loading={loading} />
        </div>
      </div>

      <ConceptQualityStrip />

      <div className="flex flex-wrap items-center gap-1 text-[10px] uppercase tracking-wide text-ink-100/40">
        <span>subject:</span>
        {subjectOptions.map((s) => (
          <FilterPill
            key={s}
            active={subjectFilter === s}
            onClick={() => changeFilter(() => setSubjectFilter(s))}
            label={s}
          />
        ))}
        <span className="ml-2">status:</span>
        {statusOptions.map((s) => (
          <FilterPill
            key={s}
            active={statusFilter === s}
            onClick={() => changeFilter(() => setStatusFilter(s))}
            label={s}
          />
        ))}
      </div>

      {error ? <ErrorBanner compact>{error}</ErrorBanner> : null}

      {visible.length === 0 ? (
        <EmptyState>
          No concepts in this view. Aiko's L2 worker mines candidate concepts
          from topic clusters and her self-memories; use "run synthesis" to
          trigger a pass.
        </EmptyState>
      ) : (
        <ul className="space-y-1">
          {visible.map((c) => (
            <ConceptCard
              key={c.id}
              concept={c}
              expanded={expanded.has(c.id)}
              onToggle={() => toggle(c.id)}
              onDelete={() => void handleDelete(c)}
            />
          ))}
        </ul>
      )}

      {pageCount > 1 ? (
        <div className="flex items-center justify-center gap-3 pt-1 text-[11px] text-ink-100/60">
          <button
            type="button"
            onClick={() => setPage(page - 1)}
            disabled={loading || page <= 0}
            className="rounded border border-white/10 px-2 py-0.5 text-[11px] hover:border-ink-400 disabled:cursor-not-allowed disabled:opacity-40"
          >
            Prev
          </button>
          <span className="font-mono text-ink-100/40">
            page {page + 1} of {pageCount}
          </span>
          <button
            type="button"
            onClick={() => setPage(page + 1)}
            disabled={loading || page + 1 >= pageCount}
            className="rounded border border-white/10 px-2 py-0.5 text-[11px] hover:border-ink-400 disabled:cursor-not-allowed disabled:opacity-40"
          >
            Next
          </button>
        </div>
      ) : null}
    </Panel>
  );
}

function FilterPill({
  active,
  onClick,
  label,
}: {
  active: boolean;
  onClick: () => void;
  label: string;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={
        "rounded border px-1.5 py-0.5 " +
        (active
          ? "border-ink-400 bg-ink-400/10 text-ink-100"
          : "border-white/10 text-ink-100/60 hover:border-ink-400/60")
      }
    >
      {label}
    </button>
  );
}

const STATUS_TONE: Record<string, string> = {
  candidate: "border-amber-400/30 bg-amber-500/5",
  active: "border-emerald-400/30 bg-emerald-500/5",
  dormant: "border-white/10 bg-white/[0.02] opacity-70",
  retired: "border-white/10 bg-white/[0.02] opacity-60",
  contradicted: "border-rose-400/30 bg-rose-500/5",
};

interface ConceptCardProps {
  concept: ConceptRow;
  expanded: boolean;
  onToggle: () => void;
  onDelete: () => void;
}

function ConceptCard({
  concept,
  expanded,
  onToggle,
  onDelete,
}: ConceptCardProps) {
  const tone =
    STATUS_TONE[concept.status] ?? "border-white/10 bg-white/[0.02]";
  // L9: a compact "what this belief rests on" line, from the first few
  // resolved evidence labels already in the payload (never truncated in
  // the expanded view -- this collapsed summary just trims for density).
  const supportingSummary = concept.evidence
    .map((e) => e.label.trim())
    .filter((l) => l.length > 0)
    .slice(0, 3)
    .map((l) => (l.length > 40 ? `${l.slice(0, 39)}\u2026` : l))
    .join(" · ");
  return (
    <li className={`rounded border p-2 text-[11px] ${tone}`}>
      <div className="flex items-start justify-between gap-2">
        <button
          type="button"
          onClick={onToggle}
          className="flex-1 text-left"
          aria-expanded={expanded}
        >
          <div className="mb-1 flex flex-wrap items-center gap-1 text-[10px] uppercase tracking-wide text-ink-100/60">
            <span>{concept.subject}</span>
            <span>·</span>
            <span>{concept.kind}</span>
            <span>·</span>
            {concept.status === "contradicted" ? (
              <span
                className="rounded bg-rose-500/20 px-1 text-rose-100"
                title="Actively disproven by counter-evidence (L9). Revivable if it re-reinforces."
              >
                contradicted
              </span>
            ) : (
              <span>{concept.status}</span>
            )}
            <span>·</span>
            <span>conf {concept.confidence.toFixed(2)}</span>
            <span>·</span>
            <span title="Plasticity: how readily this belief moves. Low = sticky (resists decay and disproof).">
              plast {concept.plasticity.toFixed(2)}
            </span>
            {concept.importance !== undefined && (
              <>
                <span>·</span>
                <span
                  title={
                    `Importance: how much this belief matters, separate from how likely it is to be true. ` +
                    `${concept.importance_prior?.toFixed(2) ?? "?"} from the ${concept.kind} kind` +
                    (concept.importance_charge
                      ? `, lifted by ${concept.importance_charge.toFixed(2)} of emotional charge on its topics.`
                      : " (no affect data on its topics).")
                  }
                >
                  imp {concept.importance.toFixed(2)}
                </span>
              </>
            )}
            <span>·</span>
            <span>
              {concept.evidence_count} evidence
              {concept.distinct_source_count !== concept.evidence_count
                ? ` (${concept.distinct_source_count} distinct)`
                : ""}
            </span>
            {concept.last_reinforced_at ? (
              <>
                <span>·</span>
                <span>reinforced {formatRelative(concept.last_reinforced_at)}</span>
              </>
            ) : null}
          </div>
          {/* Full label, wrapped -- never truncated. */}
          <div className="whitespace-pre-wrap break-words font-medium text-ink-100/85">
            {concept.label}
          </div>
          {supportingSummary ? (
            <div className="mt-0.5 break-words text-[10px] text-ink-100/45">
              <span className="text-ink-100/30">supporting: </span>
              {supportingSummary}
            </div>
          ) : null}
        </button>
        <button
          type="button"
          onClick={onDelete}
          title="Delete this concept (memories are not affected)"
          className="shrink-0 rounded border border-white/10 px-1.5 py-0.5 text-[10px] hover:border-rose-400 hover:text-rose-200"
        >
          delete
        </button>
      </div>

      {expanded ? (
        <div className="mt-2 space-y-2">
          {concept.rationale ? (
            <div className="whitespace-pre-wrap break-words text-ink-100/70">
              <span className="text-ink-100/40">rationale: </span>
              {concept.rationale}
            </div>
          ) : null}
          <div>
            <div className="mb-1 text-[10px] uppercase tracking-wide text-ink-100/40">
              Evidence ({concept.evidence.length})
            </div>
            {concept.evidence.length === 0 ? (
              <div className="text-[10px] text-ink-100/40">
                No evidence edges.
              </div>
            ) : (
              <ul className="space-y-1">
                {concept.evidence.map((e, i) => (
                  <li
                    key={`${e.src_type}:${e.src_id}:${i}`}
                    className="rounded border border-white/5 bg-white/[0.02] p-1.5"
                  >
                    <div className="mb-0.5 flex flex-wrap items-center gap-1 text-[10px] uppercase tracking-wide text-ink-100/40">
                      <span>{e.src_type}</span>
                      <span>#{e.src_id}</span>
                      {e.relation !== "evidence" ? (
                        <span>· {e.relation}</span>
                      ) : null}
                      {e.polarity < 0 ? (
                        <span className="text-rose-200/70">· contra</span>
                      ) : null}
                    </div>
                    {/* Full evidence text, wrapped -- never truncated. */}
                    <div className="whitespace-pre-wrap break-words text-ink-100/75">
                      {e.label || (
                        <span className="italic text-ink-100/30">
                          (source row not found)
                        </span>
                      )}
                    </div>
                  </li>
                ))}
              </ul>
            )}
          </div>
        </div>
      ) : null}
    </li>
  );
}

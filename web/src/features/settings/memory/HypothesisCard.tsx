import { useCallback, useState } from "react";
import type {
  GroundedHypothesisRow,
  HypothesisRow,
  HypothesisVerdictResult,
} from "../../../types";
import { formatRelative } from "../SettingsSection";

/** Status tones mirror the lifecycle rather than the concept palette: an
 *  invented guess that is still open is a *weaker* thing than a candidate
 *  concept, and the colour should not suggest otherwise. */
const STATUS_TONE: Record<string, string> = {
  open: "border-sky-400/25 bg-sky-500/5",
  supported: "border-emerald-400/30 bg-emerald-500/5",
  refuted: "border-rose-400/30 bg-rose-500/5",
  expired: "border-white/10 bg-white/[0.02] opacity-60",
  merged: "border-violet-400/30 bg-violet-500/5 opacity-80",
  graduated: "border-amber-400/30 bg-amber-500/5 opacity-80",
};

const STATUS_HELP: Record<string, string> = {
  open: "Invented and never answered. Ages out after hypothesis_ttl_hours if it is never asked.",
  supported:
    "Confirmed at least once. Graduates into a concept once it clears graduate_min_support and graduate_min_credence.",
  refuted:
    "Denied. Closed outright rather than merely weakened -- Aiko made it up, and being told no is the end of it. Kept as a row so the novelty gate cannot re-invent it.",
  expired:
    "Aged out without ever being asked. Nothing was learned, so it does NOT block re-inventing the same ground.",
  merged:
    "Proved true, but a concept already held the belief -- folded into that one instead of forking a near-twin.",
  graduated:
    "Proved true and became a new candidate concept (or, for a world guess, a durable memory).",
};

const VERDICT_PROMPT: Record<string, string> = {
  confirm:
    "What would they have said? Required -- it is stored as the memory a graduated concept rests on, and without it graduation would mint a concept resting on nothing.",
  correct:
    "Their better wording. This replaces the statement and costs half a credence step, because being close enough to be refined is partly a hit.",
  deny: "What they said (optional). A deny closes the row outright.",
};

interface Props {
  row: HypothesisRow;
  expanded: boolean;
  busy: boolean;
  result: HypothesisVerdictResult | null;
  onToggle: () => void;
  onVerdict: (verdict: string, text: string) => void;
  onDelete: () => void;
}

export function HypothesisCard({
  row,
  expanded,
  busy,
  result,
  onToggle,
  onVerdict,
  onDelete,
}: Props) {
  const [pending, setPending] = useState<string | null>(null);
  const [text, setText] = useState("");

  const open = useCallback((verdict: string) => {
    setPending((prev) => (prev === verdict ? null : verdict));
    setText("");
  }, []);

  const apply = useCallback(() => {
    if (!pending) return;
    onVerdict(pending, text);
    setPending(null);
    setText("");
  }, [onVerdict, pending, text]);

  const tone = STATUS_TONE[row.status] ?? "border-white/10 bg-white/[0.02]";
  const orphaned = row.linked_concept_id !== null && row.live;
  const confirmBlocked = pending === "confirm" && text.trim().length === 0;

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
            <span>#{row.hypothesis_id}</span>
            <span>·</span>
            <span>{row.subject}</span>
            <span>·</span>
            <span>{row.kind}</span>
            <span>·</span>
            <span title={STATUS_HELP[row.status] ?? row.status}>
              {row.status}
            </span>
            <span>·</span>
            <span title="Credence: how likely Aiko thinks this is. Asserted by the proposer and never recomputed -- this is NOT a concept's derived confidence.">
              cred {row.credence.toFixed(2)}
            </span>
            <span>·</span>
            <span title="Confirmations / denials from real or forced answers.">
              {row.support_count}+ / {row.refute_count}-
            </span>
            <span>·</span>
            <span title="Times a cue was published for this row. max_asks=1, so anything above 0 means the ask side is done with it.">
              asked {row.asked_count}
            </span>
            <span>·</span>
            <span title="Unsettledness: how far from decided. Drives which row the ask worker picks.">
              unsettled {row.unsettled.toFixed(2)}
            </span>
            {row.linked_concept_id !== null ? (
              <>
                <span>·</span>
                <span
                  className="rounded bg-violet-500/20 px-1 text-violet-100"
                  title="A concept already carries this belief. Linked rows are hidden from the lane, the ask worker and recall_hypotheses on purpose -- the concept speaks for it now."
                >
                  linked #{row.linked_concept_id}
                </span>
              </>
            ) : null}
          </div>
          <div className="whitespace-pre-wrap break-words font-medium text-ink-100/85">
            {row.statement}
          </div>
          {orphaned ? (
            <div className="mt-1 break-words text-[10px] text-amber-200/70">
              Linked and still live, so nothing will surface it. If concept #
              {row.linked_concept_id} still exists this is normal; if it was
              deleted the link is released automatically on delete.
            </div>
          ) : null}
        </button>
        <div className="flex shrink-0 flex-col items-end gap-1">
          {row.live ? (
            <div className="flex gap-1">
              {(["confirm", "correct", "deny"] as const).map((v) => (
                <button
                  key={v}
                  type="button"
                  onClick={() => open(v)}
                  disabled={busy}
                  title={VERDICT_PROMPT[v]}
                  className={
                    "rounded border px-1.5 py-0.5 text-[10px] disabled:opacity-40 " +
                    (pending === v
                      ? "border-ink-400 bg-ink-400/10 text-ink-100"
                      : "border-white/10 text-ink-100/60 hover:border-ink-400/60")
                  }
                >
                  {v}
                </button>
              ))}
            </div>
          ) : null}
          <button
            type="button"
            onClick={onDelete}
            title="Delete this guess outright. No memory or concept is affected. Unlike a deny it leaves nothing for the novelty gate, so the same guess can be invented again -- right for clearing test rows, wrong for 'the user said no'."
            className="rounded border border-white/10 px-1.5 py-0.5 text-[10px] hover:border-rose-400 hover:text-rose-200"
          >
            delete
          </button>
        </div>
      </div>

      {pending ? (
        <div className="mt-2 space-y-1 rounded border border-white/10 bg-black/20 p-2">
          <div className="whitespace-pre-wrap break-words text-[10px] text-ink-100/50">
            {VERDICT_PROMPT[pending]}
          </div>
          <textarea
            value={text}
            onChange={(e) => setText(e.target.value)}
            rows={2}
            placeholder={
              pending === "correct"
                ? "it's more that I just hate sitting still"
                : "yeah, pretty much"
            }
            className="w-full rounded border border-white/10 bg-black/30 p-1.5 text-[11px] text-ink-100/85"
          />
          <div className="flex items-center justify-end gap-1">
            {confirmBlocked ? (
              <span className="mr-auto text-[10px] text-amber-200/70">
                a confirm needs the answer text
              </span>
            ) : null}
            <button
              type="button"
              onClick={() => setPending(null)}
              className="rounded border border-white/10 px-1.5 py-0.5 text-[10px] hover:border-ink-400"
            >
              cancel
            </button>
            <button
              type="button"
              onClick={apply}
              disabled={busy || confirmBlocked}
              className="rounded border border-white/10 px-1.5 py-0.5 text-[10px] hover:border-emerald-400 hover:text-emerald-200 disabled:opacity-40"
            >
              {busy ? "applying..." : `apply ${pending}`}
            </button>
          </div>
        </div>
      ) : null}

      {result ? <VerdictDiff result={result} /> : null}

      {expanded ? (
        <div className="mt-2 space-y-1 text-[10px] text-ink-100/60">
          {row.rationale ? (
            <div className="whitespace-pre-wrap break-words">
              <span className="text-ink-100/40">rationale: </span>
              {row.rationale}
            </div>
          ) : null}
          <div>
            <span className="text-ink-100/40">origin: </span>
            {row.origin}
            {row.origin_refs.length > 0
              ? ` (refs ${row.origin_refs.join(", ")})`
              : ""}
          </div>
          {row.answer_memory_ids.length > 0 ? (
            <div title="The memories the user's own answers were stored as -- the only evidence a graduated concept inherits.">
              <span className="text-ink-100/40">answer memories: </span>
              {row.answer_memory_ids.join(", ")}
            </div>
          ) : null}
          {row.graduated_concept_id !== null ? (
            <div>
              <span className="text-ink-100/40">became concept: </span>#
              {row.graduated_concept_id}
            </div>
          ) : null}
          {row.graduated_memory_id !== null ? (
            <div title="A world-subject guess has no concept kind to become, so it exits as a durable fact memory.">
              <span className="text-ink-100/40">anchored as memory: </span>#
              {row.graduated_memory_id}
            </div>
          ) : null}
          <div className="flex flex-wrap gap-2">
            <span>invented {formatRelative(row.created_at)}</span>
            {row.last_tested_at ? (
              <span>tested {formatRelative(row.last_tested_at)}</span>
            ) : null}
            {row.closed_at ? (
              <span>closed {formatRelative(row.closed_at)}</span>
            ) : null}
          </div>
        </div>
      ) : null}
    </li>
  );
}

/** The before/after of a forced verdict. The write goes through the live
 *  post-turn path, which returns nothing, so the row is re-read and
 *  diffed -- and the diff is the interesting part: whether it linked,
 *  whether it graduated, where the credence landed. */
function VerdictDiff({ result }: { result: HypothesisVerdictResult }) {
  const { before, after } = result;
  if (!after) return null;
  const parts: string[] = [
    `credence ${before.credence.toFixed(2)} \u2192 ${after.credence.toFixed(2)}`,
  ];
  if (before.status !== after.status) {
    parts.push(`${before.status} \u2192 ${after.status}`);
  }
  if (after.support_count !== before.support_count) {
    parts.push(`support ${after.support_count}`);
  }
  if (after.refute_count !== before.refute_count) {
    parts.push(`refute ${after.refute_count}`);
  }
  if (before.linked_concept_id !== after.linked_concept_id) {
    parts.push(`linked #${after.linked_concept_id}`);
  }
  if (after.graduated_concept_id !== null) {
    parts.push(`concept #${after.graduated_concept_id}`);
  }
  if (after.graduated_memory_id !== null) {
    parts.push(`memory #${after.graduated_memory_id}`);
  }
  if (before.statement !== after.statement) {
    parts.push("restated");
  }
  if (result.answer_memory_id) {
    parts.push(`answer stored as memory #${result.answer_memory_id}`);
  }
  return (
    <div className="mt-1 break-words text-[10px] text-emerald-200/70">
      {result.verdict}: {parts.join(" \u00b7 ")}
    </div>
  );
}

/** The grounded half. Read-only: a candidate concept has no hypothesis
 *  row, so a verdict here belongs to the concept write path and deleting
 *  belongs in the Concepts panel. Offering either button would write
 *  somewhere the reader did not expect. */
export function GroundedCard({ row }: { row: GroundedHypothesisRow }) {
  return (
    <li className="rounded border border-white/10 bg-white/[0.02] p-2 text-[11px]">
      <div className="mb-1 flex flex-wrap items-center gap-1 text-[10px] uppercase tracking-wide text-ink-100/50">
        <span>concept #{row.concept_id}</span>
        <span>·</span>
        <span>{row.subject}</span>
        <span>·</span>
        <span>{row.kind}</span>
        <span>·</span>
        <span title="Derived from evidence and re-derived by L3 every tick -- not a credence.">
          conf {row.confidence.toFixed(2)}
        </span>
        <span>·</span>
        <span>{row.distinct_source_count} distinct</span>
        <span>·</span>
        <span>unsettled {row.unsettled.toFixed(2)}</span>
      </div>
      <div className="whitespace-pre-wrap break-words text-ink-100/80">
        {row.statement}
      </div>
      {row.rationale ? (
        <div className="mt-0.5 whitespace-pre-wrap break-words text-[10px] text-ink-100/45">
          <span className="text-ink-100/30">rationale: </span>
          {row.rationale}
        </div>
      ) : null}
    </li>
  );
}

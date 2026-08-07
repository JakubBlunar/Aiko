import { useCallback, useMemo, useState } from "react";
import { api } from "../../../api";
import type { HypothesisShelf, HypothesisVerdictResult } from "../../../types";
import { useAsyncResource } from "@/hooks/useAsyncResource";
import { Panel } from "@/components/Panel";
import { RefreshButton } from "@/components/RefreshButton";
import { ErrorBanner } from "@/components/ErrorBanner";
import { EmptyState } from "@/components/EmptyState";
import { GroundedCard, HypothesisCard } from "./HypothesisCard";

const ALL = "all";

/** No paging, unlike the Concepts panel. `hypothesis_max_open` is 12 and
 *  closed rows accumulate over weeks, so the whole shelf is a handful of
 *  rows carrying no resolved source text — the thing that made the
 *  concept snapshot heavy enough to need pages. */
export function HypothesesPanel() {
  const [origin, setOrigin] = useState<string>(ALL);
  const [status, setStatus] = useState<string>(ALL);
  const [subject, setSubject] = useState<string>(ALL);

  const loader = useCallback(
    () =>
      api.getHypothesisShelf({
        status: status === ALL ? undefined : status,
        subject: subject === ALL ? undefined : subject,
      }),
    [status, subject],
  );
  const { data, loading, error, setError, refresh } =
    useAsyncResource<HypothesisShelf | null>(loader, null);

  const [expanded, setExpanded] = useState<Set<number>>(new Set());
  const [busyId, setBusyId] = useState<number | null>(null);
  const [running, setRunning] = useState(false);
  const [runResult, setRunResult] = useState<string | null>(null);
  const [verdicts, setVerdicts] = useState<
    Record<number, HypothesisVerdictResult>
  >({});
  const [savingSwitch, setSavingSwitch] = useState(false);

  const toggle = useCallback((id: number) => {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }, []);

  const handleInvent = useCallback(async () => {
    setRunning(true);
    setRunResult(null);
    setError(null);
    try {
      const { result } = await api.runHypothesisProposer();
      // The rejection counts are the interesting output: "proposed 4,
      // kept 0" with `rejected_novelty 4` says the gate is doing its job,
      // not that the LLM failed.
      const parts = Object.entries(result ?? {})
        .filter(([, v]) => typeof v === "number" && Number(v) !== 0)
        .map(([k, v]) => `${k} ${v}`);
      setRunResult(parts.length > 0 ? parts.join(" · ") : "nothing kept");
      await refresh();
    } catch (err) {
      setError(String(err));
    } finally {
      setRunning(false);
    }
  }, [refresh, setError]);

  const handleAsk = useCallback(async () => {
    setRunning(true);
    setRunResult(null);
    setError(null);
    try {
      const { result } = await api.runHypothesisAsk();
      const drafted = Number(
        (result as Record<string, unknown>)?.drafted ?? 0,
      );
      if (drafted > 0) {
        setRunResult(
          `queued ${drafted} cue${drafted === 1 ? "" : "s"} — see the Cues tab`,
        );
      } else {
        // The worker says *why* it drafted nothing (no_candidate,
        // all_asked, disabled), which is the whole answer to a quiet lane.
        const reason = Object.keys(result ?? {}).find(
          (k) => k !== "drafted" && k !== "questions",
        );
        setRunResult(reason ? `nothing queued: ${reason}` : "nothing queued");
      }
      await refresh();
    } catch (err) {
      setError(String(err));
    } finally {
      setRunning(false);
    }
  }, [refresh, setError]);

  const handleVerdict = useCallback(
    async (id: number, verdict: string, text: string) => {
      setBusyId(id);
      setError(null);
      try {
        const result = await api.forceHypothesisVerdict(id, verdict, text);
        setVerdicts((prev) => ({ ...prev, [id]: result }));
        await refresh();
      } catch (err) {
        setError(String(err));
      } finally {
        setBusyId(null);
      }
    },
    [refresh, setError],
  );

  const handleDelete = useCallback(
    async (id: number, statement: string) => {
      if (
        !window.confirm(
          `Delete hypothesis "${statement}"?\n\nNo memory or concept is ` +
            `touched. Unlike a deny this leaves nothing behind, so the same ` +
            `guess can be invented again.`,
        )
      ) {
        return;
      }
      try {
        await api.deleteHypothesis(id);
        setVerdicts((prev) => {
          const next = { ...prev };
          delete next[id];
          return next;
        });
        await refresh();
      } catch (err) {
        setError(String(err));
      }
    },
    [refresh, setError],
  );

  const setSwitch = useCallback(
    async (key: string, value: boolean) => {
      setSavingSwitch(true);
      setError(null);
      try {
        await api.patchSettings({ companion: { [key]: value } });
        await refresh();
      } catch (err) {
        setError(String(err));
      } finally {
        setSavingSwitch(false);
      }
    },
    [refresh, setError],
  );

  const state = data?.state;
  const invented = data?.invented ?? [];
  const grounded = data?.grounded ?? [];
  const showInvented = origin !== "grounded";
  const showGrounded = origin !== "invented" && status === ALL;

  const statusOptions = useMemo(
    () => [ALL, ...Object.keys(state?.by_status ?? {}).sort()],
    [state?.by_status],
  );
  const subjectOptions = useMemo(() => {
    const seen = new Set<string>();
    for (const row of invented) seen.add(row.subject);
    for (const row of grounded) seen.add(row.subject);
    return [ALL, ...Array.from(seen).sort()];
  }, [grounded, invented]);

  if (data && state && !state.store) {
    return (
      <Panel className="text-[11px] text-ink-100/40">
        The hypotheses table is not available, so nothing can be invented.
        This needs the concept layer: enable
        <code className="mx-1">concepts_enabled</code> in agent settings and
        restart.
      </Panel>
    );
  }

  return (
    <Panel>
      <div className="flex items-center justify-between gap-2 text-[11px]">
        <span
          className="font-medium text-ink-100/70"
          title="Guesses Aiko has not established. Invented rows she made up herself out of curiosity; grounded ones are candidate concepts the L30a lane can raise. A confirmed guess graduates into a concept. See docs/hypotheses.md."
        >
          Hypotheses
          {state ? (
            <span className="ml-2 text-ink-100/40">
              ({state.live}
              {state.max_open !== null ? ` of ${state.max_open} live` : " live"}
              {state.linked ? `, ${state.linked} linked` : ""})
            </span>
          ) : null}
        </span>
        <div className="flex items-center gap-1">
          {runResult ? (
            <span className="break-words text-[10px] text-emerald-200/70">
              {runResult}
            </span>
          ) : null}
          <RefreshButton
            onClick={() => void handleInvent()}
            loading={running}
            label="invent now"
            title="Run the hypothesis proposer once instead of waiting for its slow cadence. The rejection counts in the result are the interesting part."
          />
          <RefreshButton
            onClick={() => void handleAsk()}
            loading={running}
            label="queue ask"
            title="Pick testable rows and queue cues for them. Queuing is not asking -- the cue still waits for a topic match or a typed gap, so watch the Cues tab afterwards."
          />
          <RefreshButton onClick={() => void refresh()} loading={loading} />
        </div>
      </div>

      {state ? <StateStrip state={state} /> : null}

      {state ? (
        <div className="flex flex-wrap items-center gap-3 text-[10px] text-ink-100/50">
          <SwitchToggle
            label="invention"
            checked={state.invention_enabled}
            disabled={savingSwitch}
            title="Whether the proposer invents new guesses at all. Takes effect live -- the worker re-reads this every tick. Turn it off to test the ask loop on the rows already on the shelf."
            onChange={(v) => void setSwitch("hypothesis_invention_enabled", v)}
          />
          <SwitchToggle
            label="asking"
            checked={state.ask_enabled}
            disabled={savingSwitch}
            title="Whether the ask worker queues cues for testable rows. Takes effect live. Off means guesses pile up unasked and eventually expire."
            onChange={(v) => void setSwitch("concept_hypothesis_ask_enabled", v)}
          />
          <span
            className="text-ink-100/30"
            title="Cadences, max_open, both novelty bars and the TTL are captured when the workers are built, so they are read-only here: a control for them would look like it worked and change nothing until a restart. Edit config.json instead."
          >
            other knobs: restart to change
          </span>
        </div>
      ) : null}

      <div className="flex flex-wrap items-center gap-1 text-[10px] uppercase tracking-wide text-ink-100/40">
        <span>origin:</span>
        {[ALL, "invented", "grounded"].map((o) => (
          <FilterPill
            key={o}
            active={origin === o}
            onClick={() => setOrigin(o)}
            label={o}
          />
        ))}
        <span className="ml-2">status:</span>
        {statusOptions.map((s) => (
          <FilterPill
            key={s}
            active={status === s}
            onClick={() => setStatus(s)}
            label={s}
          />
        ))}
        {subjectOptions.length > 1 ? (
          <>
            <span className="ml-2">subject:</span>
            {subjectOptions.map((s) => (
              <FilterPill
                key={s}
                active={subject === s}
                onClick={() => setSubject(s)}
                label={s}
              />
            ))}
          </>
        ) : null}
      </div>

      {error ? <ErrorBanner compact>{error}</ErrorBanner> : null}

      {showInvented ? (
        invented.length === 0 ? (
          <EmptyState>
            {state && !state.invention_enabled
              ? "No invented guesses, and invention is off. Turn it on above, or use \u201cinvent now\u201d for a single pass."
              : "No invented guesses in this view. Use \u201cinvent now\u201d to run the proposer; it needs some concepts and memories to riff off."}
          </EmptyState>
        ) : (
          <ul className="space-y-1">
            {invented.map((row) => (
              <HypothesisCard
                key={row.hypothesis_id}
                row={row}
                expanded={expanded.has(row.hypothesis_id)}
                busy={busyId === row.hypothesis_id}
                result={verdicts[row.hypothesis_id] ?? null}
                onToggle={() => toggle(row.hypothesis_id)}
                onVerdict={(verdict, text) =>
                  void handleVerdict(row.hypothesis_id, verdict, text)
                }
                onDelete={() =>
                  void handleDelete(row.hypothesis_id, row.statement)
                }
              />
            ))}
          </ul>
        )
      ) : null}

      {showGrounded && grounded.length > 0 ? (
        <>
          <div
            className="pt-1 text-[10px] uppercase tracking-wide text-ink-100/40"
            title="Candidate concepts unsettled enough for the L30a lane to raise. They have no hypothesis row, so there is nothing here to give a verdict to or delete -- that belongs to the Concepts panel."
          >
            grounded — read-only
          </div>
          <ul className="space-y-1">
            {grounded.map((row) => (
              <GroundedCard key={row.concept_id} row={row} />
            ))}
          </ul>
        </>
      ) : null}
    </Panel>
  );
}

/** The four "nothing is happening" states look identical from the chat
 *  window and are told apart only here: a bare shelf, a full shelf where
 *  every row is linked, rows that exist but were never asked, and cues
 *  queued that never fired. */
function StateStrip({
  state,
}: {
  state: NonNullable<HypothesisShelf["state"]>;
}) {
  const counts = Object.entries(state.by_status)
    .map(([k, n]) => `${k} ${n}`)
    .join(" · ");
  return (
    <div className="space-y-0.5 rounded border border-white/10 bg-white/[0.02] p-2 text-[10px] text-ink-100/50">
      <div className="break-words">{counts || "the shelf is empty"}</div>
      <div className="flex flex-wrap gap-2 text-ink-100/40">
        {state.ttl_hours !== null ? (
          <span title="An open row that is never asked is closed as expired after this. An expired row does not block re-inventing the same ground.">
            ttl {state.ttl_hours}h
          </span>
        ) : null}
        {state.graduate_min_support !== null ? (
          <span title="Confirmations needed before a supported row can become a concept.">
            graduates at {state.graduate_min_support} support
          </span>
        ) : null}
        {state.graduate_min_credence !== null ? (
          <span title="And credence must reach this. Both bars, not either.">
            + {state.graduate_min_credence} credence
          </span>
        ) : null}
      </div>
      {state.max_open !== null && state.live >= state.max_open ? (
        <div className="break-words text-amber-200/70">
          The shelf is full, so nothing new will be invented until a row
          settles or expires.
        </div>
      ) : null}
      {state.live > 0 && (state.linked ?? 0) >= state.live ? (
        <div className="break-words text-amber-200/70">
          Every live row is linked to a concept, so the lane has nothing to
          raise even though the shelf looks stocked.
        </div>
      ) : null}
    </div>
  );
}

function SwitchToggle({
  label,
  checked,
  disabled,
  title,
  onChange,
}: {
  label: string;
  checked: boolean;
  disabled: boolean;
  title: string;
  onChange: (value: boolean) => void;
}) {
  return (
    <label className="flex items-center gap-1" title={title}>
      <input
        type="checkbox"
        checked={checked}
        disabled={disabled}
        onChange={(e) => onChange(e.target.checked)}
      />
      <span>{label}</span>
    </label>
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
        "rounded-full border px-1.5 py-0.5 " +
        (active
          ? "border-ink-400 bg-ink-400/10 text-ink-100"
          : "border-white/10 text-ink-100/50 hover:border-ink-400/60")
      }
    >
      {label}
    </button>
  );
}

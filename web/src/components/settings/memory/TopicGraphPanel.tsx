import { useCallback, useEffect, useState } from "react";
import { api } from "../../../api";
import type { TopicGraphCluster, TopicGraphSnapshot } from "../../../types";

export function TopicGraphPanel() {
  const [data, setData] = useState<TopicGraphSnapshot | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [status, setStatus] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const snapshot = await api.getTopicGraph();
      setData(snapshot);
    } catch (err) {
      setError(String(err));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const clusters = data?.clusters ?? [];
  const enabled = data?.enabled ?? false;
  const manageable = Boolean(data?.persistent);

  return (
    <div className="mt-4 space-y-2 rounded-md border border-white/5 bg-white/[0.02] p-3">
      <div className="flex items-center justify-between gap-2 text-[11px]">
        <span
          className="font-medium text-ink-100/70"
          title="Cosine-clustered view of Aiko's memories -- the topic territory she's covered. Each cluster is a knot of memories close together in embedding space. Rename, pin, or forget a whole topic to steer her mental map."
        >
          Topic graph
          <span className="ml-2 text-ink-100/40">({data?.total_clusters ?? 0})</span>
        </span>
        <button
          type="button"
          onClick={refresh}
          disabled={loading}
          className="rounded border border-white/10 px-2 py-0.5 hover:border-ink-400 disabled:opacity-40"
        >
          {loading ? "..." : "refresh"}
        </button>
      </div>

      {error ? (
        <div className="rounded border border-rose-400/40 bg-rose-500/10 px-2 py-1 text-[11px] text-rose-200">
          {error}
        </div>
      ) : null}

      {status ? (
        <div className="rounded border border-emerald-400/30 bg-emerald-500/10 px-2 py-1 text-[11px] text-emerald-200">
          {status}
        </div>
      ) : null}

      {!enabled ? (
        <p className="text-[11px] text-ink-100/40">
          The topic graph is off (no memory store, or
          {" "}
          <code className="text-ink-100/60">topic_graph_enabled</code> is
          disabled).
        </p>
      ) : (
        <>
          <div className="text-[10px] text-ink-100/40">
            {data?.clustered_memories ?? 0} of {data?.total_memories ?? 0}{" "}
            memories clustered · sim {(data?.similarity ?? 0).toFixed(2)} · min
            size {data?.min_cluster_size ?? 0}
            {manageable ? null : " · read-only (non-persistent mode)"}
          </div>
          {clusters.length === 0 ? (
            <p className="text-[11px] text-ink-100/40">
              No clusters yet. A cluster forms once at least{" "}
              {data?.min_cluster_size ?? 3} memories share a topic.
            </p>
          ) : (
            <ul className="space-y-1.5">
              {clusters.map((cluster) => (
                <TopicClusterRow
                  key={cluster.cluster_id}
                  cluster={cluster}
                  manageable={manageable}
                  onChanged={refresh}
                  onStatus={setStatus}
                  onError={setError}
                />
              ))}
            </ul>
          )}
        </>
      )}
    </div>
  );
}

interface TopicClusterRowProps {
  cluster: TopicGraphCluster;
  manageable: boolean;
  onChanged: () => Promise<void> | void;
  onStatus: (msg: string | null) => void;
  onError: (msg: string | null) => void;
}

type RowAction = "rename" | "pin" | "unpin" | "forget" | null;

function TopicClusterRow({
  cluster,
  manageable,
  onChanged,
  onStatus,
  onError,
}: TopicClusterRowProps) {
  const [open, setOpen] = useState(false);
  const [renaming, setRenaming] = useState(false);
  const [draft, setDraft] = useState(cluster.summary || "");
  const [confirmForget, setConfirmForget] = useState(false);
  const [busy, setBusy] = useState<RowAction>(null);
  const kindEntries = Object.entries(cluster.kind_counts);

  const run = useCallback(
    async (action: RowAction, fn: () => Promise<string>) => {
      setBusy(action);
      onError(null);
      onStatus(null);
      try {
        const msg = await fn();
        onStatus(msg);
        await onChanged();
      } catch (err) {
        onError(String(err));
      } finally {
        setBusy(null);
      }
    },
    [onChanged, onStatus, onError],
  );

  const saveRename = useCallback(async () => {
    const label = draft.trim();
    if (!label || label === cluster.summary) {
      setRenaming(false);
      return;
    }
    await run("rename", async () => {
      await api.renameTopicCluster(cluster.cluster_id, label);
      return `Renamed cluster #${cluster.cluster_id} to "${label}".`;
    });
    setRenaming(false);
  }, [draft, cluster.cluster_id, cluster.summary, run]);

  const setPinned = useCallback(
    (pinned: boolean) =>
      run(pinned ? "pin" : "unpin", async () => {
        const res = await api.pinTopicCluster(cluster.cluster_id, pinned);
        return `${pinned ? "Pinned" : "Unpinned"} ${res.affected} ${
          res.affected === 1 ? "memory" : "memories"
        } in this topic.`;
      }),
    [cluster.cluster_id, run],
  );

  const forget = useCallback(
    () =>
      run("forget", async () => {
        const res = await api.forgetTopicCluster(cluster.cluster_id);
        const kept =
          res.skipped_pinned > 0
            ? ` (kept ${res.skipped_pinned} pinned)`
            : "";
        return `Archived ${res.archived} ${
          res.archived === 1 ? "memory" : "memories"
        } from this topic${kept}.`;
      }),
    [cluster.cluster_id, run],
  );

  const anyBusy = busy !== null;

  return (
    <li className="rounded border border-white/5 bg-white/[0.02] p-2 text-[11px]">
      <div className="flex w-full items-start justify-between gap-2">
        {renaming ? (
          <div className="flex min-w-0 flex-1 items-center gap-1">
            <input
              autoFocus
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") void saveRename();
                if (e.key === "Escape") setRenaming(false);
              }}
              className="min-w-0 flex-1 rounded border border-white/10 bg-white/[0.04] px-1.5 py-0.5 text-ink-100/90 outline-none focus:border-ink-400"
            />
            <button
              type="button"
              onClick={() => void saveRename()}
              disabled={anyBusy}
              className="rounded border border-emerald-400/40 px-1.5 py-0.5 text-emerald-200 hover:border-emerald-300 disabled:opacity-40"
            >
              save
            </button>
            <button
              type="button"
              onClick={() => setRenaming(false)}
              disabled={anyBusy}
              className="rounded border border-white/10 px-1.5 py-0.5 hover:border-ink-400 disabled:opacity-40"
            >
              cancel
            </button>
          </div>
        ) : (
          <button
            type="button"
            onClick={() => setOpen((v) => !v)}
            className="flex min-w-0 flex-1 items-start justify-between gap-2 text-left"
          >
            <span className="min-w-0 flex-1">
              <span className="text-ink-100/90">
                {cluster.summary || "(untitled cluster)"}
              </span>
              <span className="ml-2 inline-flex flex-wrap gap-1 align-middle">
                {kindEntries.map(([kind, count]) => (
                  <span
                    key={kind}
                    className="rounded bg-white/[0.06] px-1 py-px text-[9px] text-ink-100/60"
                  >
                    {kind} {count}
                  </span>
                ))}
              </span>
            </span>
            <span className="shrink-0 text-[10px] text-ink-100/40">
              {cluster.size} · {open ? "hide" : "show"}
            </span>
          </button>
        )}
      </div>

      {manageable && !renaming ? (
        <div className="mt-1.5 flex flex-wrap items-center gap-1 text-[10px]">
          <button
            type="button"
            onClick={() => {
              setDraft(cluster.summary || "");
              setRenaming(true);
            }}
            disabled={anyBusy}
            className="rounded border border-white/10 px-1.5 py-0.5 hover:border-ink-400 disabled:opacity-40"
          >
            rename
          </button>
          <button
            type="button"
            onClick={() => void setPinned(true)}
            disabled={anyBusy}
            className="rounded border border-white/10 px-1.5 py-0.5 hover:border-amber-300/60 disabled:opacity-40"
          >
            {busy === "pin" ? "pinning..." : "pin all"}
          </button>
          <button
            type="button"
            onClick={() => void setPinned(false)}
            disabled={anyBusy}
            className="rounded border border-white/10 px-1.5 py-0.5 hover:border-ink-400 disabled:opacity-40"
          >
            {busy === "unpin" ? "unpinning..." : "unpin all"}
          </button>
          {confirmForget ? (
            <>
              <span className="text-rose-200/80">forget topic?</span>
              <button
                type="button"
                onClick={() => {
                  setConfirmForget(false);
                  void forget();
                }}
                disabled={anyBusy}
                className="rounded border border-rose-400/40 px-1.5 py-0.5 text-rose-200 hover:border-rose-300 disabled:opacity-40"
              >
                {busy === "forget" ? "archiving..." : "yes, archive"}
              </button>
              <button
                type="button"
                onClick={() => setConfirmForget(false)}
                disabled={anyBusy}
                className="rounded border border-white/10 px-1.5 py-0.5 hover:border-ink-400 disabled:opacity-40"
              >
                cancel
              </button>
            </>
          ) : (
            <button
              type="button"
              onClick={() => setConfirmForget(true)}
              disabled={anyBusy}
              className="rounded border border-white/10 px-1.5 py-0.5 text-rose-200/70 hover:border-rose-400/50 disabled:opacity-40"
              title="Archive every non-pinned memory in this topic (reversible from the Memory list)."
            >
              forget
            </button>
          )}
        </div>
      ) : null}

      {open ? (
        <ul className="mt-1.5 space-y-1 border-t border-white/5 pt-1.5">
          {cluster.members.map((member) => (
            <li key={member.id} className="text-ink-100/70">
              <span className="text-[10px] uppercase tracking-wide text-ink-100/40">
                #{member.id} · {member.kind} · {member.tier} · sal{" "}
                {member.salience.toFixed(2)}
              </span>
              <div className="text-ink-100/80">{member.content}</div>
            </li>
          ))}
        </ul>
      ) : null}
    </li>
  );
}

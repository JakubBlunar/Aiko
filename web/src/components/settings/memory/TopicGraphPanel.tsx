import { useCallback, useEffect, useState } from "react";
import { api } from "../../../api";
import type { TopicGraphCluster, TopicGraphSnapshot } from "../../../types";

export function TopicGraphPanel() {
  const [data, setData] = useState<TopicGraphSnapshot | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

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

  return (
    <div className="mt-4 space-y-2 rounded-md border border-white/5 bg-white/[0.02] p-3">
      <div className="flex items-center justify-between gap-2 text-[11px]">
        <span
          className="font-medium text-ink-100/70"
          title="Cosine-clustered view of Aiko's memories -- the topic territory she's covered. Each cluster is a knot of memories close together in embedding space. Read-only; this is what the curiosity-seed worker uses to avoid re-mining old ground."
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
          </div>
          {clusters.length === 0 ? (
            <p className="text-[11px] text-ink-100/40">
              No clusters yet. A cluster forms once at least{" "}
              {data?.min_cluster_size ?? 3} memories share a topic.
            </p>
          ) : (
            <ul className="space-y-1.5">
              {clusters.map((cluster) => (
                <TopicClusterRow key={cluster.cluster_id} cluster={cluster} />
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
}

function TopicClusterRow({ cluster }: TopicClusterRowProps) {
  const [open, setOpen] = useState(false);
  const kindEntries = Object.entries(cluster.kind_counts);
  return (
    <li className="rounded border border-white/5 bg-white/[0.02] p-2 text-[11px]">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-start justify-between gap-2 text-left"
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

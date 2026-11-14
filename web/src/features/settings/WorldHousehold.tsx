import { useEffect, useRef, useState } from "react";
import { api } from "../../api";
import type { HomeHouseholdSnapshot, WorldSnapshot } from "../../types";

export function WorldHousehold({ world }: { world: WorldSnapshot }) {
  const [view, setView] = useState<HomeHouseholdSnapshot | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const generation = useRef(0);
  useEffect(() => {
    let active = true;
    const load = async () => {
      const current = ++generation.current;
      try {
        const next = await api.homeHousehold();
        if (active && current === generation.current) { setView(next); setError(""); }
      } catch (cause) {
        if (active && current === generation.current) setError(String(cause));
      }
    };
    void load();
    const timer = window.setInterval(() => void load(), 30_000);
    return () => { active = false; window.clearInterval(timer); };
  }, [world.items, world.state.updated_at]);
  if (!view?.enabled) return error ? <p role="alert" className="text-xs text-rose-200">
    Home belongings unavailable: {error}
  </p> : null;
  const restore = async (token: string) => {
    setBusy(true);
    try {
      await api.restoreHomePossession(token);
      const current = ++generation.current;
      const next = await api.homeHousehold();
      if (current === generation.current) setView(next);
      setError("");
    } catch (cause) { setError(String(cause)); }
    finally { setBusy(false); }
  };
  return <WorldHouseholdContents view={view} error={error} busy={busy} onRestore={restore} />;
}

export function WorldHouseholdContents({ view, error, busy, onRestore }: {
  view: HomeHouseholdSnapshot;
  error: string;
  busy: boolean;
  onRestore: (token: string) => void | Promise<void>;
}) {
  const names = new Map(view.catalogue.map((product) => [product.id, product.name]));
  return (
    <details className="rounded border border-white/10 p-3 text-xs text-ink-100/75">
      <summary className="cursor-pointer font-medium text-ink-100">Home shop and belongings</summary>
      <p className="mt-2 text-[11px] text-ink-100/50">
        {view.active_space_used} / {view.capacity} active belongings · storage {view.stored.length} / 8
      </p>
      <p className="mt-2">Virtual shop: {view.catalogue.map((product) => product.name).join(", ")}</p>
      {view.wishlist?.length ? <p className="mt-2">
        Wishlist: {view.wishlist.map((id) => names.get(id) ?? id).join(", ")}
      </p> : null}
      {view.orders.length ? <ul className="mt-2 space-y-1" aria-label="Home orders">
        {view.orders.slice().reverse().map((order) => <li key={order.id}>
          {names.get(order.product) ?? order.product} · {order.status}
          {!["cancelled", "unpacked"].includes(order.status)
            ? ` · arrival ${new Date(order.arrives_at).toLocaleString()}` : ""}
        </li>)}
      </ul> : null}
      {(["stored", "bin"] as const).map((key) => <div key={key} className="mt-3">
        <div className="font-medium">{key === "stored" ? "Storage" : "Recoverable bin"}</div>
        {view[key].length ? <ul className="mt-1 space-y-1">
          {view[key].map((item) => {
            const option = view.actions.find((action) => action.item_id === item.id &&
              ["retrieve_item", "restore_item"].includes(action.action));
            return <li key={item.id} className="flex flex-wrap items-center gap-2">
              <span>{item.name}</span>
              {item.lifecycle.expires_at ? <span className="text-[10px] text-ink-100/45">
                clears after {new Date(item.lifecycle.expires_at).toLocaleString()}
              </span> : null}
              {option ? <button type="button" disabled={busy} onClick={() => void onRestore(option.token)}
                className="rounded border border-white/15 px-2 py-0.5 disabled:opacity-40">
                Bring back
              </button> : <span className="text-[10px] text-ink-100/45">
                Make room or return to the apartment to bring this back.
              </span>}
            </li>;
          })}
        </ul> : <p className="text-ink-100/40">Empty</p>}
      </div>)}
      {error ? <p role="alert" className="mt-2 text-rose-200">{error}</p> : null}
    </details>
  );
}

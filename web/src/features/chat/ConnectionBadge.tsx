import { useAssistantStore } from "@/store";

export function ConnectionBadge() {
  const status = useAssistantStore((s) => s.connection.status);
  const tone =
    status === "connected"
      ? "bg-emerald-500/20 text-emerald-200 border-emerald-400/40"
      : status === "connecting"
        ? "bg-amber-400/20 text-amber-100 border-amber-300/40"
        : "bg-rose-500/20 text-rose-200 border-rose-400/40";
  const label =
    status === "connected"
      ? "online"
      : status === "connecting"
        ? "connecting"
        : "offline";
  return (
    <span
      className={`rounded-full border px-3 py-1 text-xs font-medium ${tone}`}
    >
      {label}
    </span>
  );
}

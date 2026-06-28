import type { ToolEvent } from "@/types";

const TOOL_LABELS: Record<
  string,
  { call: string; result: string; icon: string }
> = {
  get_time: {
    call: "checking the time",
    result: "got the current time",
    icon: "⏱️",
  },
  recall: {
    call: "searching her notebook",
    result: "found something in her notebook",
    icon: "📔",
  },
  web_search: {
    call: "searching the web",
    result: "found something on the web",
    icon: "🔎",
  },
};

export function ToolActivityStrip({ activity }: { activity: ToolEvent[] }) {
  if (activity.length === 0) return null;
  const items = activity.slice(-4);
  return (
    <ul className="mx-auto mt-3 flex max-w-3xl flex-col gap-1 text-xs text-ink-100/55">
      {items.map((evt, idx) => {
        const meta = TOOL_LABELS[evt.name] ?? {
          call: `running ${evt.name}`,
          result: `${evt.name} returned`,
          icon: "🛠",
        };
        const failed = evt.event === "result" && evt.ok === false;
        const phrase =
          evt.event === "call"
            ? meta.call
            : failed
              ? `${evt.name} failed`
              : meta.result;
        return (
          <li
            key={`${evt.name}-${evt.at}-${idx}`}
            className={`flex items-center gap-2 ${failed ? "text-rose-300/80" : ""}`}
          >
            <span aria-hidden="true">{meta.icon}</span>
            <span>aiko is {phrase}…</span>
          </li>
        );
      })}
    </ul>
  );
}

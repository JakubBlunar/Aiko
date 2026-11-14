import { useId } from "react";

export type SettingsTabId = "chat" | "models" | "behavior" | "voice" | "tools" | "avatar"
  | "memory" | "diary" | "archive" | "world" | "together" | "knowledge" | "tasks";

export const SETTINGS_GROUPS: readonly {
  label: string;
  tabs: readonly { id: SettingsTabId; label: string }[];
}[] = [
  { label: "Aiko", tabs: [
    { id: "chat", label: "General" }, { id: "behavior", label: "Behavior" },
    { id: "voice", label: "Voice" }, { id: "avatar", label: "Avatar" },
    { id: "world", label: "World" }, { id: "together", label: "Together" },
  ] },
  { label: "Library", tabs: [
    { id: "memory", label: "Memory" }, { id: "diary", label: "Diary" },
    { id: "archive", label: "Archive" }, { id: "knowledge", label: "Documents" },
  ] },
  { label: "System", tabs: [
    { id: "models", label: "Models" }, { id: "tools", label: "Tools" },
    { id: "tasks", label: "Tasks" },
  ] },
];

export function SettingsNavigation({ active, onSelect }: {
  active: SettingsTabId;
  onSelect: (tab: SettingsTabId) => void;
}) {
  const id = useId();
  return <>
    <div className="border-b border-white/10 px-5 py-3 lg:hidden">
      <label htmlFor={id} className="mb-1 block text-[11px] text-ink-100/50">Settings section</label>
      <select id={id} value={active} onChange={(event) => onSelect(event.target.value as SettingsTabId)}
        className="w-full rounded-md border border-white/10 bg-[#18112b] px-3 py-2 text-sm text-ink-100">
        {SETTINGS_GROUPS.map((group) => <optgroup key={group.label} label={group.label}>
          {group.tabs.map((tab) => <option key={tab.id} value={tab.id}>{tab.label}</option>)}
        </optgroup>)}
      </select>
    </div>
    <nav aria-label="Settings sections"
      className="hidden w-40 shrink-0 space-y-5 overflow-y-auto border-r border-white/10 px-3 py-4 lg:block">
      {SETTINGS_GROUPS.map((group) => <div key={group.label}>
        <p className="mb-1 px-2 text-[10px] uppercase tracking-wider text-ink-100/35">{group.label}</p>
        {group.tabs.map((tab) => <button key={tab.id} type="button" onClick={() => onSelect(tab.id)}
          aria-current={active === tab.id ? "page" : undefined}
          className={`block w-full rounded-md px-2 py-2 text-left text-xs transition focus-visible:outline focus-visible:outline-ink-400 ${
            active === tab.id ? "bg-ink-500/30 font-medium text-ink-100"
              : "text-ink-100/60 hover:bg-white/5 hover:text-ink-100"
          }`}>{tab.label}</button>)}
      </div>)}
    </nav>
  </>;
}

import type { AssistantSettings } from "@/types";
import { Toggle } from "@/components/Toggle";
import { Section } from "./SettingsSection";

export interface ToolsTabProps {
  settings: AssistantSettings;
  apply: (patch: Record<string, unknown>) => Promise<void>;
}

/**
 * The "Tools" settings tab: the master tool switch plus the three live tool
 * toggles (get_time / recall / web_search). Extracted from SettingsDrawer
 * (phase 4c) so the drawer shell stays a thin tab dispatcher.
 */
export function ToolsTab({ settings, apply }: ToolsTabProps) {
  const toolsEnabled = settings.tools?.enabled ?? true;
  return (
    <Section title="Tools">
      <p className="text-[11px] text-ink-100/50">
        Tools let Aiko reach for fresh facts before answering: the current
        time, your notebook, or the public web. Disable any she shouldn't use.
      </p>
      <Toggle
        className="mt-1"
        checked={toolsEnabled}
        onChange={(checked) => void apply({ tools: { enabled: checked } })}
      >
        Tools enabled
      </Toggle>
      <Toggle
        className="ml-4"
        checked={settings.tools?.get_time ?? true}
        disabled={!toolsEnabled}
        onChange={(checked) => void apply({ tools: { get_time: checked } })}
      >
        get_time — current date/time
      </Toggle>
      <Toggle
        className="ml-4"
        checked={settings.tools?.recall ?? true}
        disabled={!toolsEnabled}
        onChange={(checked) => void apply({ tools: { recall: checked } })}
      >
        recall — search Aiko's notebook
      </Toggle>
      <Toggle
        className="ml-4"
        checked={settings.tools?.web_search ?? true}
        disabled={!toolsEnabled}
        onChange={(checked) => void apply({ tools: { web_search: checked } })}
      >
        web_search — DuckDuckGo
      </Toggle>
      {settings.tools?.available && settings.tools.available.length > 0 ? (
        <div className="rounded-md bg-white/[0.02] px-3 py-2 text-[11px] text-ink-100/60">
          Active: {settings.tools.available.join(", ")}
        </div>
      ) : (
        <div className="rounded-md bg-white/[0.02] px-3 py-2 text-[11px] text-ink-100/50 italic">
          No tools currently available.
        </div>
      )}
    </Section>
  );
}

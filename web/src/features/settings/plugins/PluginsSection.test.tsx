import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { PluginCard } from "./PluginsSection";
import { initialValue, SchemaField } from "./SchemaField";
import type { PluginRow, SettingsSchema } from "./types";
import { notifyPluginsUpdated, subscribePluginsUpdated } from "./updates";

const row = (patch: Partial<PluginRow> = {}): PluginRow => ({
  id: "test", name: "Test plugin", enabled: false, phase: "disabled", error: "", revision: "one",
  applied_revision: null, generation: "one", tool_count: 0, schema: { type: "object", properties: {} },
  ui: { description: "Plugin-owned description" }, config: {}, access: [], warnings: [], ...patch,
});
const card = (patch: Partial<PluginRow> = {}) => renderToStaticMarkup(<PluginCard row={row(patch)} onUpdate={() => {}} />);

describe("plugin settings", () => {
  it("shows legacy toggle-only plugins without exposing opaque configuration", () => {
    const html = card({ config: { token: "private" } });
    expect(html).toContain("Test plugin");
    expect(html).toContain("Plugin-owned description");
    expect(html).not.toContain("private");
    expect(html).not.toContain("Apply settings");
  });
  it("distinguishes a saved pending configuration from active tools", () => {
    expect(card({ enabled: true, phase: "starting" })).toContain("Settings are saved.");
    expect(card({ enabled: true, phase: "starting" })).not.toContain("Saved settings are active");
    expect(card({ enabled: true, phase: "ready", tool_count: 9 })).toContain("9 background tools");
  });
  it("shows configured access, read/write policy and narrower source paths", () => {
    const html = card({ access: [
      { name: "Source", path: "F:/Source", mode: "read_only", scope: ["app", "web/src"] },
      { name: "Files", path: "F:/Files", mode: "read_write" },
    ] });
    expect(html).toContain("Configured access");
    expect(html).toContain("read only");
    expect(html).toContain("Allowed relative paths: app, web/src");
    expect(html).toContain("read and write; task approval rules apply");
  });
  it("offers retry with the actual activation error", () => {
    const html = card({ enabled: true, phase: "error", error: "Dependency unavailable" });
    expect(html).toContain("Dependency unavailable");
    expect(html).toContain("Retry activation");
  });
  it("renders reusable workspace arrays, stable ids, directories and advanced settings", () => {
    const html = card({ schema: { type: "object", properties: {
      resources: { type: "array", title: "Workspaces", "x-uniqueBy": "id", items: {
        type: "object", properties: { id: { type: "string", title: "Stable ID" },
          root: { type: "string", title: "Folder", format: "host-directory" } } } },
      limit: { type: "integer", title: "Limit", minimum: 1, maximum: 100 },
    } }, ui: { advanced: ["limit"] }, config: { resources: [{ id: "one", root: "F:/Source" }], limit: 3 } });
    expect(html).toContain("Stable ID");
    expect(html).toContain("Absolute folder path on Aiko");
    expect(html).toContain("Remove Workspaces item 1");
    expect(html).toContain("Advanced settings");
    expect(html).toContain('type="number"');
    expect(html).toContain('min="1" max="100"');
  });
  it("renders boolean, enum and hidden secret controls from metadata", () => {
    const schema: SettingsSchema = { type: "object", properties: {
      flag: { type: "boolean", title: "Flag" },
      adapter: { type: "string", title: "Adapter", enum: ["generic", "special"] },
      token: { type: "string", title: "Token", writeOnly: true },
    } };
    const html = renderToStaticMarkup(<SchemaField schema={schema} root={schema}
      label="Settings" value={{ flag: true, adapter: "generic" }} onChange={() => {}} />);
    expect(html).toContain('type="password"');
    expect(html).toContain("Stored value is hidden");
    expect(html).toContain("special");
    expect(html).toContain("Flag");
  });
  it("uses independent defaults for repeated items", () => {
    const schema: SettingsSchema = { type: "object", properties: {
      paths: { type: "array", default: ["src"] }, id: { type: "string" },
    } };
    const first = initialValue(schema) as { paths: string[]; id: string };
    first.paths.push("other");
    expect(initialValue(schema)).toEqual({ paths: ["src"], id: "" });
  });
  it("stops invalidating once the settings panel unsubscribes", () => {
    let calls = 0;
    const stop = subscribePluginsUpdated(() => calls++);
    notifyPluginsUpdated(); stop(); notifyPluginsUpdated();
    expect(calls).toBe(1);
  });
});

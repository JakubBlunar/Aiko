import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { SettingsNavigation, SETTINGS_GROUPS } from "./SettingsNavigation";
import { SettingsNavigationProvider, SettingsSections } from "./SettingsSections";

describe("settings navigation", () => {
  it("keeps draft panels mounted while exposing only the selected section", () => {
    const html = renderToStaticMarkup(<SettingsNavigationProvider>
      <SettingsSections area="Models" sections={[
        { id: "routes", label: "Routes", content: <input aria-label="Route draft" defaultValue="draft" /> },
        { id: "providers", label: "Providers", content: <input aria-label="Provider draft" /> },
      ]} />
    </SettingsNavigationProvider>);
    expect(html).toContain('role="tablist" aria-label="Models sections"');
    expect(html).toContain('aria-selected="true" tabindex="0"');
    expect(html).toContain('aria-selected="false" tabindex="-1"');
    expect(html).toContain('aria-label="Provider draft"');
    expect(html).toMatch(/role="tabpanel"[^>]*hidden=""/);
    expect(html).toContain('value="draft"');
    const controls = [...html.matchAll(/aria-controls="([^"]+)"/g)].map((match) => match[1]);
    for (const id of controls) expect(html).toContain(`id="${id}"`);
  });

  it("makes every top-level area available in desktop and compact navigation", () => {
    const html = renderToStaticMarkup(<SettingsNavigation active="models" onSelect={() => {}} />);
    for (const group of SETTINGS_GROUPS) {
      for (const tab of group.tabs) {
        expect(html).toContain(`value="${tab.id}"`);
        expect(html).toContain(`>${tab.label}</button>`);
      }
    }
    expect(html).toContain('aria-current="page"');
    expect(html).toContain("Settings section");
    expect(html).toContain(">Behavior</button>");
    expect(html).toContain(">Models</button>");
  });
});

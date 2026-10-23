import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import type { MetricsSnapshot } from "../../types";
import { DiagnosticsSection } from "./DiagnosticsSection";

vi.mock("./LeadFollowPanel", () => ({ LeadFollowPanel: () => null }));
vi.mock("./PersonaRegressionPanel", () => ({
  PersonaRegressionPanel: () => null,
}));

function render(metrics: MetricsSnapshot) {
  return renderToStaticMarkup(
    <DiagnosticsSection
      metrics={null}
      liveLastMetrics={metrics}
      onApplyPatch={() => undefined}
      busy={false}
    />,
  );
}

describe("reasoning usage diagnostics", () => {
  it("shows the reported split alongside the full completion total", () => {
    const html = render({
      completion_tokens: 120,
      reasoning_tokens: 90,
      answer_tokens: 30,
    });
    expect(html).toMatch(/Completion<\/span><span[^>]*>120<\/span>/);
    expect(html).toMatch(/Reasoning<\/span><span[^>]*>90<\/span>/);
    expect(html).toMatch(/Answer tokens<\/span><span[^>]*>30<\/span>/);
  });

  it("distinguishes known zero from an unreported split", () => {
    expect(render({ reasoning_tokens: 0, answer_tokens: 30 })).toMatch(
      /Reasoning<\/span><span[^>]*>0<\/span>/,
    );
    expect(render({ completion_tokens: 30 })).toContain("Not reported");
    expect(render({ reasoning_tokens: null, answer_tokens: null })).toContain(
      "Not reported",
    );
  });
});

import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import type { HypothesisRow } from "../../../types";
import { HypothesisCard } from "./HypothesisCard";

const row: HypothesisRow = {
  hypothesis_id: 1, statement: "A short walk may help switch out of project mode.",
  kind: "pattern", subject: "user", rationale: "A possible transition ritual.",
  origin: "memory", origin_refs: [42], status: "open", credence: 0.35,
  support_count: 0, refute_count: 0, asked_count: 0, unsettled: 0.7, live: true,
  linked_concept_id: null, graduated_concept_id: null, graduated_memory_id: null,
  answer_memory_ids: [], created_at: "2026-11-05T08:00:00+00:00",
  last_tested_at: null, closed_at: null,
};

function render(value: HypothesisRow) {
  return renderToStaticMarkup(<HypothesisCard row={value} expanded busy={false}
    result={null} onToggle={() => {}} onVerdict={() => {}} onDelete={() => {}} />);
}

describe("hypothesis motivation", () => {
  it("shows the leap, competing explanation and source as context rather than proof", () => {
    const html = render({ ...row, quality: {
      version: 1, useful_because: "Choose a fitting way to unwind.",
      alternative: "Quiet music could work just as well.",
      raise_when: "When a project session is winding down.",
      check_question: "What helps you switch out of work mode?",
      inspiration: { type: "memory", id: 42, text: "A much longer original memory.",
        quote: "tidying the desk before drawing" },
    } });
    for (const text of ["Choose a fitting way to unwind", "Quiet music",
      "project session is winding down", "switch out of work mode",
      "tidying the desk before drawing", "context, not proof"]) {
      expect(html).toContain(text);
    }
    expect(html).not.toContain("A much longer original memory");
    expect(html).not.toContain("usefulness needs review");
  });

  it("identifies an earlier unreviewed guess without inventing a motivation", () => {
    expect(render(row)).toContain("usefulness needs review before surfacing");
  });
});

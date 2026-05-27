import { describe, expect, it, vi } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";

import { TogetherTab } from "./SettingsDrawer";
import type {
  SharedMoment,
  TogetherSummary,
} from "../types";

/**
 * Smoke tests for the ``Together`` tab.
 *
 * We render the component statically with ``react-dom/server`` to keep
 * the suite running under the Node-only vitest environment (no jsdom).
 * That lets us assert that header copy, milestones, axes bars, the
 * anniversary card, and the timeline pagination are wired correctly
 * without a DOM testing library install.
 */

function makeSummary(overrides: Partial<TogetherSummary> = {}): TogetherSummary {
  return {
    phase: "anchored",
    days_known: 42,
    total_turns: 999,
    total_sessions: 17,
    first_seen_at: "2026-01-01T00:00:00+00:00",
    axes: {
      user_id: "jacob",
      closeness: 0.62,
      humor: 0.41,
      trust: 0.31,
      comfort: 0.18,
      updated_at: "2026-05-27T12:00:00+00:00",
    },
    milestones: [
      {
        label: "first_week",
        human: "first week together",
        crossed: true,
        crossed_at: "2026-04-01T00:00:00+00:00",
      },
      {
        label: "one_month",
        human: "one month milestone",
        crossed: false,
        crossed_at: null,
      },
    ],
    anniversary_today: null,
    recent_moments_count: 3,
    ...overrides,
  };
}

function makeMoment(
  id: number,
  when: string,
  vibe: SharedMoment["vibe"] = "warm",
  overrides: Partial<SharedMoment> = {},
): SharedMoment {
  return {
    id,
    summary: `moment ${id}`,
    vibe,
    when,
    created_at: when,
    salience: 0.7,
    pinned: false,
    source: "manual",
    confidence: 1.0,
    source_message_ids: [],
    last_anniversaried_at: null,
    ...overrides,
  };
}

const noop = () => {};
const NOOPS = {
  onSetVibeFilter: noop,
  onSetPage: noop,
  setNewOpen: noop,
  setNewDraft: noop,
  onCreate: noop,
  setEditingId: noop,
  setEditDraft: noop,
  onSaveEdit: noop,
  onDelete: noop,
  onTogglePin: noop,
  onRefresh: noop,
} as const;

const DRAFT = { summary: "", vibe: "general", when: "" };

describe("TogetherTab — header", () => {
  it("renders phase chip + days/turns/sessions counts", () => {
    const html = renderToStaticMarkup(
      <TogetherTab
        summary={makeSummary()}
        moments={[]}
        total={0}
        page={0}
        pageSize={20}
        vibeFilter={null}
        loading={false}
        error={null}
        newOpen={false}
        newDraft={DRAFT}
        editingId={null}
        editDraft={DRAFT}
        {...NOOPS}
      />,
    );
    expect(html).toContain("anchored");
    expect(html).toContain("42");
    expect(html).toContain("999");
    expect(html).toContain("17");
  });

  it("renders an error banner when error is set", () => {
    const html = renderToStaticMarkup(
      <TogetherTab
        summary={makeSummary()}
        moments={[]}
        total={0}
        page={0}
        pageSize={20}
        vibeFilter={null}
        loading={false}
        error={"boom: timeline failed to load"}
        newOpen={false}
        newDraft={DRAFT}
        editingId={null}
        editDraft={DRAFT}
        {...NOOPS}
      />,
    );
    expect(html).toContain("boom: timeline failed to load");
  });

  it("falls back to placeholder when no summary loaded", () => {
    const html = renderToStaticMarkup(
      <TogetherTab
        summary={null}
        moments={[]}
        total={0}
        page={0}
        pageSize={20}
        vibeFilter={null}
        loading={true}
        error={null}
        newOpen={false}
        newDraft={DRAFT}
        editingId={null}
        editDraft={DRAFT}
        {...NOOPS}
      />,
    );
    expect(html).toContain("Loading");
  });
});

describe("TogetherTab — milestones", () => {
  it("renders crossed milestones with a check mark and date", () => {
    const html = renderToStaticMarkup(
      <TogetherTab
        summary={makeSummary()}
        moments={[]}
        total={0}
        page={0}
        pageSize={20}
        vibeFilter={null}
        loading={false}
        error={null}
        newOpen={false}
        newDraft={DRAFT}
        editingId={null}
        editDraft={DRAFT}
        {...NOOPS}
      />,
    );
    expect(html).toContain("first week together");
    expect(html).toContain("one month milestone");
    expect(html).toContain("✓");
  });
});

describe("TogetherTab — axes bars", () => {
  it("renders one bar per axis with the value", () => {
    const html = renderToStaticMarkup(
      <TogetherTab
        summary={makeSummary()}
        moments={[]}
        total={0}
        page={0}
        pageSize={20}
        vibeFilter={null}
        loading={false}
        error={null}
        newOpen={false}
        newDraft={DRAFT}
        editingId={null}
        editDraft={DRAFT}
        {...NOOPS}
      />,
    );
    expect(html).toContain("Closeness");
    expect(html).toContain("Humor");
    expect(html).toContain("Trust");
    expect(html).toContain("Comfort");
  });
});

describe("TogetherTab — anniversary card", () => {
  it("renders when summary.anniversary_today is present", () => {
    const summary = makeSummary({
      anniversary_today: {
        moment_id: 1,
        summary: "we debugged the proactive bug",
        vibe: "focused",
        days_ago: 30,
        window_label: "a month ago today",
      },
    });
    const html = renderToStaticMarkup(
      <TogetherTab
        summary={summary}
        moments={[]}
        total={0}
        page={0}
        pageSize={20}
        vibeFilter={null}
        loading={false}
        error={null}
        newOpen={false}
        newDraft={DRAFT}
        editingId={null}
        editDraft={DRAFT}
        {...NOOPS}
      />,
    );
    expect(html).toContain("On your mind today");
    expect(html).toContain("a month ago today");
    expect(html).toContain("we debugged the proactive bug");
    expect(html).toContain("focused");
  });

  it("does not render the card when anniversary_today is null", () => {
    const html = renderToStaticMarkup(
      <TogetherTab
        summary={makeSummary()}
        moments={[]}
        total={0}
        page={0}
        pageSize={20}
        vibeFilter={null}
        loading={false}
        error={null}
        newOpen={false}
        newDraft={DRAFT}
        editingId={null}
        editDraft={DRAFT}
        {...NOOPS}
      />,
    );
    expect(html).not.toContain("On your mind today");
  });
});

describe("TogetherTab — timeline pagination", () => {
  it("shows the timeline count and the moment summaries", () => {
    const moments = [
      makeMoment(1, "2026-05-15T12:00:00+00:00", "warm", {
        summary: "we laughed about cookies",
      }),
      makeMoment(2, "2026-05-01T12:00:00+00:00", "tender", {
        summary: "you told me about Mochi",
      }),
    ];
    const html = renderToStaticMarkup(
      <TogetherTab
        summary={makeSummary()}
        moments={moments}
        total={42}
        page={0}
        pageSize={20}
        vibeFilter={null}
        loading={false}
        error={null}
        newOpen={false}
        newDraft={DRAFT}
        editingId={null}
        editDraft={DRAFT}
        {...NOOPS}
      />,
    );
    expect(html).toContain("Shared moments (42)");
    expect(html).toContain("we laughed about cookies");
    expect(html).toContain("you told me about Mochi");
  });

  it("renders an empty-state when total is zero", () => {
    const html = renderToStaticMarkup(
      <TogetherTab
        summary={makeSummary()}
        moments={[]}
        total={0}
        page={0}
        pageSize={20}
        vibeFilter={null}
        loading={false}
        error={null}
        newOpen={false}
        newDraft={DRAFT}
        editingId={null}
        editDraft={DRAFT}
        {...NOOPS}
      />,
    );
    expect(html).toContain("No moments yet.");
  });

  it("shows the vibe filter dropdown", () => {
    const html = renderToStaticMarkup(
      <TogetherTab
        summary={makeSummary()}
        moments={[]}
        total={0}
        page={0}
        pageSize={20}
        vibeFilter={"warm"}
        loading={false}
        error={null}
        newOpen={false}
        newDraft={DRAFT}
        editingId={null}
        editDraft={DRAFT}
        {...NOOPS}
      />,
    );
    expect(html).toContain("all vibes");
    expect(html).toContain("warm");
    expect(html).toContain("tender");
  });
});

describe("TogetherTab — actions", () => {
  it("does not call onRefresh during render", () => {
    const onRefresh = vi.fn();
    renderToStaticMarkup(
      <TogetherTab
        summary={makeSummary()}
        moments={[]}
        total={0}
        page={0}
        pageSize={20}
        vibeFilter={null}
        loading={false}
        error={null}
        newOpen={false}
        newDraft={DRAFT}
        editingId={null}
        editDraft={DRAFT}
        {...NOOPS}
        onRefresh={onRefresh}
      />,
    );
    expect(onRefresh).not.toHaveBeenCalled();
  });
});

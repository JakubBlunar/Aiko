import { describe, expect, it } from "vitest";
import { stripJournalPrefix } from "./journalText";

describe("stripJournalPrefix", () => {
  it("strips a [dream] prefix and reports the dream badge", () => {
    const { text, badge } = stripJournalPrefix("[dream] I was back in the orchard.");
    expect(text).toBe("I was back in the orchard.");
    expect(badge).toBe("dream");
  });

  it("strips a [mindmap] prefix and reports the noticing badge", () => {
    const { text, badge } = stripJournalPrefix("[mindmap] most of what I carry circles work.");
    expect(text).toBe("most of what I carry circles work.");
    expect(badge).toBe("noticing");
  });

  it("leaves unprefixed content untouched with no badge", () => {
    const { text, badge } = stripJournalPrefix("just a plain reflection");
    expect(text).toBe("just a plain reflection");
    expect(badge).toBeNull();
  });

  it("left-trims extra whitespace after the prefix", () => {
    const { text } = stripJournalPrefix("[dream]    spaced out");
    expect(text).toBe("spaced out");
  });

  it("is defensive against null / undefined / empty", () => {
    expect(stripJournalPrefix(null)).toEqual({ text: "", badge: null });
    expect(stripJournalPrefix(undefined)).toEqual({ text: "", badge: null });
    expect(stripJournalPrefix("")).toEqual({ text: "", badge: null });
  });

  it("only strips a prefix at the very start", () => {
    const { text, badge } = stripJournalPrefix("note: [dream] not a real prefix");
    expect(text).toBe("note: [dream] not a real prefix");
    expect(badge).toBeNull();
  });
});

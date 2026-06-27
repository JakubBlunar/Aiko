/**
 * Journal-entry content prefixes Aiko's inner-life workers stamp onto
 * memory rows so the render path can tell dream / mindmap output apart
 * from ordinary waking reflections.
 *
 * These mirror the Python constants — ``_DREAM_PREFIX`` in
 * ``app/core/proactive/dream_worker.py`` and ``MINDMAP_PREFIX`` in
 * ``app/core/proactive/knowledge_map_reflection_worker.py`` — and the
 * strip logic in ``app/core/session/inner_life/turning_over.py``. The
 * prefix is a *functional discriminator* stored on the row (RAG and the
 * turning-over picker depend on it), so it must never be stripped at the
 * API / storage layer — only at the display layer, which is what this
 * helper is for. Keep the literals in sync if the Python side changes.
 */

const DREAM_PREFIX = "[dream] ";
const MINDMAP_PREFIX = "[mindmap] ";

/** Display badge derived from a stripped journal prefix.
 * ``null`` means the content carried no recognised prefix. */
export type JournalBadge = "dream" | "noticing" | null;

export interface StrippedJournalText {
  /** Content with any recognised journal prefix removed and left-trimmed. */
  text: string;
  /** A short badge label for the stripped prefix, or ``null`` if none. */
  badge: JournalBadge;
}

/**
 * Strip a leading ``[dream] `` / ``[mindmap] `` prefix from a memory's
 * content and report which badge (if any) the prefix maps to. Pure and
 * defensive: a ``null`` / ``undefined`` content collapses to an empty
 * string with no badge.
 */
export function stripJournalPrefix(content: string | null | undefined): StrippedJournalText {
  const raw = content ?? "";
  if (raw.startsWith(DREAM_PREFIX)) {
    return { text: raw.slice(DREAM_PREFIX.length).replace(/^\s+/, ""), badge: "dream" };
  }
  if (raw.startsWith(MINDMAP_PREFIX)) {
    return { text: raw.slice(MINDMAP_PREFIX.length).replace(/^\s+/, ""), badge: "noticing" };
  }
  return { text: raw, badge: null };
}

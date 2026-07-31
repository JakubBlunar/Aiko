/**
 * P37: streaming re-pin granularity, in draft characters.
 *
 * Small enough that the tail never drifts more than about one wrapped
 * line behind the text, large enough that a model streaming ~40 chars per
 * chunk re-pins once or twice per chunk instead of on every token.
 */
export const STREAM_REPIN_CHARS = 48;

/**
 * Signature `ChatView` watches to decide when to re-pin the scroll.
 *
 * Virtuoso's `followOutput` only fires on `messages` *count* changes, and
 * since P9 the streaming text lives in an isolated `streamingDraft` — so
 * the growing reply is invisible to it and something has to stand in.
 * That something used to be the draft's exact length, which meant a
 * `ChatView` re-render (composer, voice strip, header, Virtuoso wrapper)
 * per token to move the scroll a few pixels.
 *
 * Quantising to `STREAM_REPIN_CHARS` keeps the tail visible at a fraction
 * of the renders. The draft **id** stays exact and un-bucketed: without
 * it, a new turn starting at length 0 would produce the same signature as
 * the previous turn's first bucket and the first re-pin of every reply
 * would be skipped.
 */
export function streamRepinSignature(
  draft: { id: string; content: string } | null | undefined,
): string {
  if (!draft) return "";
  const bucket = Math.floor(draft.content.length / STREAM_REPIN_CHARS);
  return `${draft.id}:${bucket}`;
}

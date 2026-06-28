import { USER_REACTION_KINDS } from "@/types";

/**
 * Plain-language description of what tapping each reaction tells Aiko.
 * Keyed by the canonical reaction ``kind`` so it stays aligned with
 * {@link USER_REACTION_KINDS} (a drift test pins full coverage). The
 * copy is deliberately about *what the user is signalling*, not about
 * the relationship-axes math under the hood.
 */
export const REACTION_DESCRIPTIONS: Record<string, string> = {
  heart: "you loved this one",
  hug: "a hug back",
  laugh: "she made you laugh",
  thumbs: "solid — you agree",
  rose: "a little romance",
  grateful: "thank you for this",
  blush: "that was sweet",
  eyeroll: "playful — caught her teasing",
  moved: "that one touched you",
  surprise: "whoa, didn't expect that",
};

/**
 * Collapsible legend for the emoji-reaction tray, shown inside the
 * Settings → Avatar → "Touch & reactions" section. Renders straight
 * from the shared taxonomy so adding a reaction kind surfaces here
 * automatically. The footer explains the J11 contract: reactions are
 * sparse *confirmations*, never required, and gently bias which ways
 * of showing care Aiko leans into.
 */
export function ReactionLegend() {
  return (
    <details className="mt-1 rounded-md border border-white/5 bg-white/[0.02] px-3 py-2">
      <summary className="cursor-pointer select-none text-[11px] uppercase tracking-wide text-ink-100/50 hover:text-ink-100/80">
        What do the reactions mean?
      </summary>
      <ul className="mt-2 grid grid-cols-1 gap-1.5 sm:grid-cols-2">
        {USER_REACTION_KINDS.map((r) => (
          <li key={r.kind} className="flex items-start gap-2 text-[11px]">
            <span aria-hidden="true" className="text-base leading-none">
              {r.emoji}
            </span>
            <span className="text-ink-100/70">
              <span className="text-ink-100/90">{r.label}</span>
              {REACTION_DESCRIPTIONS[r.kind] ? (
                <span className="text-ink-100/45">
                  {" "}
                  — {REACTION_DESCRIPTIONS[r.kind]}
                </span>
              ) : null}
            </span>
          </li>
        ))}
      </ul>
      <p className="mt-2 text-[10px] leading-relaxed text-ink-100/40">
        Tap an emoji on any of Aiko's messages to react. Reactions are
        quick confirmations — you never have to use them, but when you
        do, Aiko notices which kinds land and gently leans into the ways
        of showing she cares that you respond to most.
      </p>
    </details>
  );
}

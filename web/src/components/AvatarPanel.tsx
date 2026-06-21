import { Live2DAvatar } from "./Live2DAvatar";
import { useAssistantStore } from "../store";
import type { WorldSnapshot } from "../types";

/** Turn slugs like ``window_seat`` / ``curled_up`` / ``watching_screens``
 * into a readable caption fragment ("window seat", "curled up",
 * "watching screens"). */
function humanize(token: string | null | undefined): string {
  return (token ?? "").trim().replace(/_/g, " ");
}

/** Build the short "what Aiko is doing" caption from the world snapshot:
 * ``{location} · {posture} · {activity}`` (e.g. "garden · standing ·
 * stretching"). Pieces that are missing or trivial (``idle`` activity)
 * are dropped. Returns ``""`` when the world is disabled / unknown so
 * the caller can fall back to the plain status line. */
function worldCaption(world: WorldSnapshot | null): string {
  if (!world || world.enabled === false || !world.state) return "";
  const { state, locations } = world;
  const loc =
    state.location_id != null
      ? locations.find((l) => l.id === state.location_id)?.name
      : undefined;
  const activity = humanize(state.activity);
  const parts = [
    humanize(loc),
    humanize(state.posture),
    activity && activity !== "idle" ? activity : "",
  ].filter(Boolean);
  return parts.join(" · ");
}

/**
 * Avatar panel — wraps the Live2D renderer with the side-rail layout
 * + a status footer.
 *
 * Replaces the old PersonaPanel which doubled as an upload fallback;
 * the avatar is now always bundled (Alexia by default) so we drop
 * the SVG portrait. If the bundle directory is missing on disk we
 * still degrade gracefully — the renderer itself shows a small
 * "missing model" message rather than crashing the panel.
 */
export function AvatarPanel() {
  const ttsState = useAssistantStore((s) => s.ttsState);
  const reaction = useAssistantStore((s) => s.reaction);
  const voiceMode = useAssistantStore((s) => s.voiceMode);
  const avatar = useAssistantStore((s) => s.avatar);
  const world = useAssistantStore((s) => s.world);
  const connectionStatus = useAssistantStore((s) => s.connection.status);
  const panelWidth = useAssistantStore((s) => s.personaPanelWidth);

  // Until we've heard from the backend at least once we don't actually
  // know whether the avatar bundle is missing — show a friendlier
  // "waiting for backend" line instead of falsely accusing the user
  // of missing files. ``avatar`` becomes non-null on the ``hello``
  // frame; ``connected`` flips on the first WS open. The "missing"
  // message is reserved for the truly bad case: WS is up but the
  // backend reports ``loaded === false``.
  const showAvatar = Boolean(avatar && avatar.loaded);
  const stillBooting = !avatar && connectionStatus !== "connected";

  return (
    <aside
      style={{ width: panelWidth }}
      className="hidden h-full shrink-0 flex-col items-center border-l border-white/5 bg-gradient-to-b from-white/[0.04] to-transparent px-4 py-6 lg:flex"
    >
      <div className="text-xs uppercase tracking-[0.2em] text-ink-100/40">
        Aiko
      </div>

      <div className="relative my-4 flex w-full flex-1 items-center justify-center overflow-hidden">
        {showAvatar && avatar ? (
          <Live2DAvatar manifest={avatar} />
        ) : (
          <div className="flex h-full w-full items-center justify-center text-center text-[11px] text-ink-100/40">
            {stillBooting ? "Waiting for backend…" : "Avatar files missing."}
          </div>
        )}
      </div>

      <div className="w-full max-w-xs text-center">
        <div className="text-sm font-medium text-ink-100">
          {
            /* {avatar?.display_name || */ LABEL_FOR_REACTION[reaction] ||
              "Aiko"
          }
        </div>
        <div className="mt-1 text-[10px] uppercase tracking-[0.2em] text-ink-100/40">
          {ttsState === "speaking"
            ? "speaking"
            : voiceMode !== "off"
              ? voiceMode
              : worldCaption(world) || "idle"}
        </div>
      </div>
    </aside>
  );
}

const LABEL_FOR_REACTION: Record<string, string> = {
  neutral: "Neutral",
  cheerful: "Cheerful",
  excited: "Excited",
  enthusiastic: "Enthusiastic",
  friendly: "Friendly",
  calm: "Calm",
  serious: "Focused",
  sad: "A little sad",
  gentle: "Gentle",
  angry: "Frustrated",
  surprised: "Surprised",
};

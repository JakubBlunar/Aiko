import { Live2DAvatar } from "./Live2DAvatar";
import { useAssistantStore } from "../store";

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

  return (
    <aside className="hidden h-full w-[440px] shrink-0 flex-col items-center border-l border-white/5 bg-gradient-to-b from-white/[0.04] to-transparent px-4 py-6 lg:flex">
      <div className="text-xs uppercase tracking-[0.2em] text-ink-100/40">
        Aiko
      </div>

      <div className="relative my-4 flex w-full flex-1 items-center justify-center overflow-hidden">
        {avatar && avatar.loaded ? (
          <Live2DAvatar manifest={avatar} />
        ) : (
          <div className="flex h-full w-full items-center justify-center text-center text-[11px] text-ink-100/40">
            Avatar files missing.
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
              : "idle"}
          {avatar?.loaded ? ` · cubism v${avatar.cubism_version}` : ""}
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

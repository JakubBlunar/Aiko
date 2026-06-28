import type { VoiceMode } from "@/types";

interface MicButtonProps {
  voiceMode: VoiceMode;
  audioLevel: number;
  connected: boolean;
  onClick: () => void;
  /** Optional size variant. ``compact`` is used inside the persona
   * window where horizontal real estate is tight; ``default`` matches
   * the original ``ChatView`` mic button. */
  size?: "default" | "compact";
  /**
   * Multi-client voice-ownership state. When ``remotelyOwned`` is true
   * a different client is currently holding the mic; the button stays
   * clickable (clicking takes over) but renders the "take over" tint
   * so the user can tell at a glance that voice mode is shared.
   */
  remotelyOwned?: boolean;
}

/**
 * Microphone toggle that drives the live-mode pipeline. Extracted from
 * the original inline ``MicButton`` in ``ChatView.tsx`` so the persona
 * window can reuse the exact same affordance — same emoji, same pulse
 * ring, same pressed-state styling.
 *
 * Purely presentational: the parent owns the WS plumbing and decides
 * whether ``onClick`` should call ``send({ type: "voice_start" })`` or
 * ``send({ type: "voice_stop" })`` based on the current ``voiceMode``.
 */
export function MicButton({
  voiceMode,
  audioLevel,
  connected,
  onClick,
  size = "default",
  remotelyOwned = false,
}: MicButtonProps) {
  const isOn = voiceMode !== "off";
  const dims =
    size === "compact"
      ? "h-9 w-9 rounded-lg text-base"
      : "h-12 w-12 rounded-xl text-xl";
  const ringRadius = size === "compact" ? "rounded-lg" : "rounded-xl";
  const tone = isOn
    ? "border-pink-400/60 bg-pink-500/20 text-pink-100 hover:bg-pink-500/30"
    : remotelyOwned
      ? "border-amber-300/60 bg-amber-500/15 text-amber-100 hover:bg-amber-500/25"
      : "border-white/10 bg-black/30 text-ink-100/70 hover:border-ink-400 hover:text-ink-100";
  const title = isOn
    ? "Stop voice mode"
    : remotelyOwned
      ? "Another device has the mic — take over"
      : "Start voice mode";
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={!connected}
      title={title}
      aria-label={title}
      aria-pressed={isOn}
      className={`relative flex shrink-0 items-center justify-center self-center border transition ${dims} ${tone} disabled:cursor-not-allowed disabled:opacity-40`}
    >
      {isOn && voiceMode === "listening" ? (
        <span
          aria-hidden="true"
          className={`absolute inset-0 border-2 border-pink-400/40 ${ringRadius}`}
          style={{
            transform: `scale(${1 + Math.min(audioLevel, 1) * 0.25})`,
            transition: "transform 60ms linear",
            opacity: 0.6,
          }}
        />
      ) : null}
      <span className="relative">
        {isOn ? "🎙️" : remotelyOwned ? "🎧" : "🎤"}
      </span>
    </button>
  );
}

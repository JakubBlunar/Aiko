import { useAssistantStore } from "@/store";
import type { VoiceMode } from "@/types";

interface VoiceStripProps {
  voiceMode: VoiceMode;
  lastTranscript: string;
  currentPartial: string;
}

export function VoiceStrip({
  voiceMode,
  lastTranscript,
  currentPartial,
}: VoiceStripProps) {
  const isOn = voiceMode !== "off";
  if (!isOn && !lastTranscript && !currentPartial) {
    return null;
  }
  const labelMap: Record<VoiceStripProps["voiceMode"], string> = {
    off: "Voice off",
    listening: "Listening",
    transcribing: "Transcribing…",
    thinking: "Thinking…",
    speaking: "Speaking…",
  };
  // Show the live partial only while we're actively listening — once
  // the user has stopped talking the "you said:" pill takes over.
  const showPartial = currentPartial && voiceMode === "listening";
  return (
    <div className="mb-2 flex flex-wrap items-center gap-2 text-xs">
      {isOn ? (
        <span className="inline-flex items-center gap-2 rounded-full border border-pink-400/30 bg-pink-500/10 px-3 py-1 text-pink-100">
          <LevelDot />
          <span className="font-medium uppercase tracking-wider">
            {labelMap[voiceMode]}
          </span>
          <AudioMeter active={voiceMode === "listening"} />
        </span>
      ) : null}
      {showPartial ? (
        <span className="inline-flex max-w-full items-center gap-2 truncate rounded-full border border-pink-400/20 bg-pink-500/5 px-3 py-1 text-pink-100/70">
          <span className="text-pink-100/40">hearing:</span>
          <span className="italic truncate">{currentPartial}…</span>
        </span>
      ) : null}
      {lastTranscript ? (
        <span className="inline-flex max-w-full items-center gap-2 truncate rounded-full border border-white/10 bg-white/[0.03] px-3 py-1 text-ink-100/70">
          <span className="text-ink-100/40">you said:</span>
          <span className="italic truncate">{lastTranscript}</span>
        </span>
      ) : null}
    </div>
  );
}

/**
 * P37: the two level-reactive elements subscribe to ``audioLevel``
 * themselves rather than taking it as a prop.
 *
 * The value updates ~20x/second while the mic is open. Read one level up,
 * it re-rendered the whole strip and — because the strip's own props came
 * from ``ChatView`` — the entire chat chrome with it. Confined here, a mic
 * frame repaints one dot and four bars.
 */
function LevelDot() {
  const audioLevel = useAssistantStore((s) => s.audioLevel);
  return (
    <span
      aria-hidden="true"
      className="h-1.5 w-1.5 rounded-full bg-pink-300"
      style={{
        opacity: 0.4 + Math.min(1, audioLevel) * 0.6,
        transition: "opacity 80ms linear",
      }}
    />
  );
}

function AudioMeter({ active }: { active: boolean }) {
  // Quantised to the number of lit bars, so the selector returns a stable
  // value across the many level updates that don't change the display.
  const filledLevel = useAssistantStore(
    (s) => Math.round(Math.max(0, Math.min(1, s.audioLevel)) * 4),
  );
  const level = filledLevel / 4;
  const bars = 4;
  const filled = Math.round(Math.max(0, Math.min(1, level)) * bars);
  return (
    <div className="flex h-1.5 items-end gap-0.5" aria-hidden="true">
      {Array.from({ length: bars }).map((_, i) => {
        const isLit = active && i < filled;
        const height = active ? 4 + i * 2 : 4;
        return (
          <span
            key={i}
            className={`w-0.5 rounded-sm transition-colors ${
              isLit ? "bg-pink-400" : "bg-white/10"
            }`}
            style={{ height: `${height}px` }}
          />
        );
      })}
    </div>
  );
}

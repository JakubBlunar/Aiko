import { useIsMobile } from "../../hooks/useIsMobile";
import { useAssistantStore } from "../../store";
import { Section } from "./SettingsSection";

/**
 * Mobile-only "Enable sound" control. iOS (and PWAs especially) keep the
 * Web Audio context locked until a real user gesture, and silently
 * re-suspend it whenever the app is backgrounded or another app grabs the
 * audio session (a YouTube video, a phone call). The persistent gesture
 * unlock in ``useAssistantSocket`` recovers automatically on most taps,
 * but this gives the user an explicit, obvious affordance to turn sound on
 * (and a clear readout of whether it's actually live) when Aiko goes
 * silent on a phone.
 *
 * Renders nothing on desktop — the AudioContext unlocks on the first click
 * there and never needs a manual nudge.
 */
export function MobileAudioSection() {
  const isMobile = useIsMobile();
  const audioOutput = useAssistantStore((s) => s.audioOutput);
  const audioUnlocked = useAssistantStore((s) => s.audioUnlocked);

  if (!isMobile) return null;

  const enable = () => {
    // The click itself is the gesture that satisfies the autoplay policy.
    // ``onForeground`` flushes any stale scheduled audio before resuming so
    // a backlog from a previous interruption doesn't burst out.
    void audioOutput?.onForeground().catch(() => {
      /* still locked — another tap will retry */
    });
  };

  return (
    <Section title="Sound (mobile)">
      <p className="text-[11px] text-ink-100/50">
        On phones, audio stays muted until you tap to allow it, and iOS can
        re-lock it after you switch apps or play a video elsewhere. If Aiko
        goes quiet, tap below to turn sound back on.
      </p>
      <div className="mt-3 flex items-center justify-between gap-3">
        <div
          className={`flex items-center gap-2 text-xs ${
            audioUnlocked ? "text-emerald-300/90" : "text-amber-200/90"
          }`}
        >
          <span
            className={`inline-block h-2 w-2 rounded-full ${
              audioUnlocked ? "bg-emerald-400" : "bg-amber-400"
            }`}
          />
          {audioUnlocked ? "Sound is on" : "Sound is off"}
        </div>
        <button
          type="button"
          onClick={enable}
          className="rounded-md border border-pink-300/50 bg-pink-500/15 px-4 py-2 text-sm font-medium text-pink-100 hover:bg-pink-500/25 active:bg-pink-500/30"
        >
          {audioUnlocked ? "Restart sound" : "Enable sound"}
        </button>
      </div>
    </Section>
  );
}

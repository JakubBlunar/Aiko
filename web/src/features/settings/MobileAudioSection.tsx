import { useIsMobile } from "../../hooks/useIsMobile";
import { useAssistantStore } from "../../store";
import { Section } from "./SettingsSection";

/**
 * "Sound" settings section. Two concerns live here:
 *
 *  1. **Per-device mute (all platforms).** Aiko's voice only ever plays on
 *     ONE device at a time -- the one you most recently used -- so it
 *     follows you between phone and desktop without echoing. The mute
 *     toggle lets you silence the current device explicitly: the server
 *     drops a muted client from the audio-owner election, so another
 *     device keeps playing (or everything goes quiet if you mute them
 *     all). The choice persists per-device in ``localStorage``.
 *
 *  2. **iOS unlock (mobile only).** iOS (and PWAs especially) keep the Web
 *     Audio context locked until a real user gesture, and silently
 *     re-suspend it whenever the app is backgrounded or another app grabs
 *     the audio session (a YouTube video, a phone call). The persistent
 *     gesture unlock in ``useAssistantSocket`` recovers automatically on
 *     most taps, but this gives the user an explicit affordance (and a
 *     clear readout) to turn sound back on when Aiko goes silent on a phone.
 *     This block is hidden on desktop, where the context unlocks on the
 *     first click and never needs a manual nudge.
 */
export function MobileAudioSection() {
  const isMobile = useIsMobile();
  const audioOutput = useAssistantStore((s) => s.audioOutput);
  const audioUnlocked = useAssistantStore((s) => s.audioUnlocked);
  const audioMuted = useAssistantStore((s) => s.audioMuted);
  const setAudioMuted = useAssistantStore((s) => s.setAudioMuted);
  const audioOwnerId = useAssistantStore((s) => s.audioOwnerId);
  const clientId = useAssistantStore((s) => s.clientId);

  // We own playback when the server hasn't elected anyone yet (single
  // client) or it elected us.
  const isOwner = !audioOwnerId || audioOwnerId === clientId;

  const enable = () => {
    // The click itself is the gesture that satisfies the autoplay policy.
    // ``onForeground`` flushes any stale scheduled audio before resuming so
    // a backlog from a previous interruption doesn't burst out.
    void audioOutput?.onForeground().catch(() => {
      /* still locked — another tap will retry */
    });
  };

  const toggleMute = () => {
    const next = !audioMuted;
    setAudioMuted(next);
    if (next) {
      // Stop whatever is playing here immediately — don't wait for the
      // server's re-election to land.
      audioOutput?.flush();
    } else {
      // Unmuting is a gesture: resume the context (also unlocks iOS) and,
      // server-side, marks this device active so it takes over playback.
      void audioOutput?.onForeground().catch(() => {
        /* still locked — a tap will retry */
      });
    }
  };

  let muteStatus: string;
  if (audioMuted) {
    muteStatus = "Muted on this device";
  } else if (isOwner) {
    muteStatus = "Playing on this device";
  } else {
    muteStatus = "Another device is playing";
  }

  return (
    <Section title="Sound">
      <p className="text-[11px] text-ink-100/50">
        Aiko's voice plays on whichever device you most recently used. Mute a
        device to silence it here — another device keeps playing, or
        everything goes quiet if you mute them all.
      </p>
      <div className="mt-3 flex items-center justify-between gap-3">
        <div
          className={`flex items-center gap-2 text-xs ${
            audioMuted ? "text-ink-100/50" : "text-emerald-300/90"
          }`}
        >
          <span
            className={`inline-block h-2 w-2 rounded-full ${
              audioMuted ? "bg-ink-100/40" : "bg-emerald-400"
            }`}
          />
          {muteStatus}
        </div>
        <button
          type="button"
          onClick={toggleMute}
          aria-pressed={audioMuted}
          className={`rounded-md border px-4 py-2 text-sm font-medium ${
            audioMuted
              ? "border-emerald-300/50 bg-emerald-500/15 text-emerald-100 hover:bg-emerald-500/25 active:bg-emerald-500/30"
              : "border-white/15 bg-white/5 text-ink-100/90 hover:bg-white/10 active:bg-white/15"
          }`}
        >
          {audioMuted ? "Unmute" : "Mute"}
        </button>
      </div>

      {isMobile && (
        <div className="mt-4 border-t border-white/5 pt-3">
          <p className="text-[11px] text-ink-100/50">
            On phones, iOS can re-lock audio after you switch apps or play a
            video elsewhere. If Aiko goes quiet, tap below to turn sound back
            on.
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
        </div>
      )}
    </Section>
  );
}

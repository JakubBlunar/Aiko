import { useState } from "react";
import { useAssistantStore } from "@/store";
import {
  disableLivePresence,
  enableLivePresence,
  liveMicNeedsConsent,
} from "./livePresence";

interface LiveModeToggleProps {
  /** ``bar`` matches the 36px mobile top-bar buttons. */
  variant?: "pill" | "bar";
}

/**
 * Header control for Live presence. The microphone stays in the composer;
 * this only flips ``behavior_posture`` and persists through PATCH.
 */
export function LiveModeToggle({ variant = "pill" }: LiveModeToggleProps) {
  const posture =
    useAssistantStore((s) => s.companionSettings?.behavior_posture) ??
    "turn_based";
  const consented = Boolean(
    useAssistantStore((s) => s.companionSettings?.live_mic_consented),
  );
  const setCompanionSettings = useAssistantStore(
    (s) => s.setCompanionSettings,
  );
  const [busy, setBusy] = useState(false);
  const [consentOpen, setConsentOpen] = useState(false);
  const liveOn = posture === "live_presence";
  const title = liveOn
    ? "Live mode is on — click to return to Text/Speak"
    : "Turn on Live mode (independent of the microphone)";

  const applyLive = (next: "turn_based" | "live_presence", patch: Promise<{ companion?: object }>) => {
    const previous = posture;
    setCompanionSettings({ behavior_posture: next });
    setBusy(true);
    void patch
      .then((settings) => {
        if (settings.companion) {
          setCompanionSettings(settings.companion);
        }
      })
      .catch(() => {
        setCompanionSettings({ behavior_posture: previous });
      })
      .finally(() => {
        setBusy(false);
        setConsentOpen(false);
      });
  };

  const toggle = () => {
    if (busy) return;
    if (liveOn) {
      applyLive("turn_based", disableLivePresence());
      return;
    }
    void liveMicNeedsConsent(consented).then((needs) => {
      if (needs) {
        setConsentOpen(true);
        return;
      }
      applyLive(
        "live_presence",
        enableLivePresence({ consented: true }),
      );
    });
  };

  const shape =
    variant === "bar"
      ? "h-9 px-2.5 rounded-md"
      : "rounded-full px-3 py-1";
  const tone = liveOn
    ? "border-pink-400/70 bg-pink-500/15 text-pink-100"
    : "border-white/10 bg-black/30 text-ink-100/70 hover:border-pink-400 hover:text-pink-100";

  return (
    <>
      <button
        type="button"
        onClick={toggle}
        disabled={busy}
        title={title}
        aria-label={title}
        aria-pressed={liveOn}
        className={`flex shrink-0 items-center justify-center border text-xs font-medium transition ${shape} ${tone} disabled:opacity-60`}
      >
        Live
      </button>
      {consentOpen ? (
        <LiveMicConsentDialog
          busy={busy}
          onCancel={() => setConsentOpen(false)}
          onConfirm={() =>
            applyLive("live_presence", enableLivePresence({ consented: true }))
          }
          onVisualOnly={() =>
            applyLive(
              "live_presence",
              enableLivePresence({ visualOnly: true }),
            )
          }
        />
      ) : null}
    </>
  );
}

export function LiveMicConsentDialog({
  busy,
  onCancel,
  onConfirm,
  onVisualOnly,
}: {
  busy?: boolean;
  onCancel: () => void;
  onConfirm: () => void;
  onVisualOnly: () => void;
}) {
  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4"
      role="dialog"
      aria-modal="true"
      aria-labelledby="live-mic-consent-title"
    >
      <div className="max-w-sm rounded-lg border border-white/15 bg-[#160d24] p-4 text-sm text-ink-100 shadow-xl">
        <h2 id="live-mic-consent-title" className="text-sm font-medium">
          Live is always-listening
        </h2>
        <p className="mt-2 text-[13px] text-ink-100/70">
          Live can notice when you are in the room. The microphone stays
          off until you tap the mic button — this does not open it. Confirm
          that you understand, or stay visual-only with no unprompted speech.
        </p>
        <div className="mt-4 flex flex-col gap-2">
          <button
            type="button"
            disabled={busy}
            onClick={onConfirm}
            className="rounded-md border border-pink-300/60 bg-pink-500/20 px-3 py-1.5 text-xs text-pink-50"
          >
            Enable Live
          </button>
          <button
            type="button"
            disabled={busy}
            onClick={onVisualOnly}
            className="rounded-md border border-white/15 bg-black/30 px-3 py-1.5 text-xs text-ink-100/80"
          >
            Visual only
          </button>
          <button
            type="button"
            disabled={busy}
            onClick={onCancel}
            className="rounded-md px-3 py-1.5 text-xs text-ink-100/55"
          >
            Cancel
          </button>
        </div>
      </div>
    </div>
  );
}

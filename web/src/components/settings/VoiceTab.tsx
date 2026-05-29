import type { Dispatch, SetStateAction } from "react";
import type { AssistantSettings } from "../../types";
import { useAssistantStore } from "../../store";
import { Row, Section } from "./SettingsSection";

export interface DeviceLists {
  inputs: { deviceId: string; label: string; groupId: string }[];
  outputs: { deviceId: string; label: string; groupId: string }[];
}

export interface DspPrefs {
  echoCancellation: boolean;
  noiseSuppression: boolean;
  autoGainControl: boolean;
}

export type MicPermission = "granted" | "denied" | "prompt" | "unknown";

export interface VoiceTabProps {
  settings: AssistantSettings;
  voices: string[];
  deviceLists: DeviceLists;
  setDeviceLists: Dispatch<SetStateAction<DeviceLists>>;
  inputDeviceId: string;
  setInputDeviceId: Dispatch<SetStateAction<string>>;
  outputDeviceId: string;
  setOutputDeviceId: Dispatch<SetStateAction<string>>;
  micPermission: MicPermission;
  setMicPermission: Dispatch<SetStateAction<MicPermission>>;
  dspPrefs: DspPrefs;
  setDspPrefs: Dispatch<SetStateAction<DspPrefs>>;
  tauri: boolean;
  liveActiveApp: string | null;
  apply: (patch: Record<string, unknown>) => Promise<void>;
}

/**
 * Mini status pill describing the current voice ownership state.
 * "Owned by this window" / "Owned by another window" / "Idle".
 */
function VoiceOwnerRow() {
  const clientId = useAssistantStore((s) => s.clientId);
  const voiceOwnerId = useAssistantStore((s) => s.voiceOwnerId);
  const label = !voiceOwnerId
    ? "No active microphone"
    : clientId && voiceOwnerId === clientId
      ? "This window"
      : "Another window";
  const tint = !voiceOwnerId
    ? "border-white/10 bg-black/30 text-ink-100/60"
    : clientId && voiceOwnerId === clientId
      ? "border-pink-300/50 bg-pink-500/10 text-pink-100/90"
      : "border-amber-300/50 bg-amber-500/10 text-amber-100/90";
  return (
    <div
      className={`mt-3 flex items-center justify-between rounded-md border px-3 py-2 text-xs ${tint}`}
    >
      <span className="font-medium">Voice owner</span>
      <span className="text-[10px] uppercase tracking-[0.2em]">{label}</span>
    </div>
  );
}

export function VoiceTab({
  settings,
  voices,
  deviceLists,
  setDeviceLists,
  inputDeviceId,
  setInputDeviceId,
  outputDeviceId,
  setOutputDeviceId,
  micPermission,
  setMicPermission,
  dspPrefs,
  setDspPrefs,
  tauri,
  liveActiveApp,
  apply,
}: VoiceTabProps) {
  return (
    <>
      <Section title="Voice (TTS)">
        <label className="block text-xs text-ink-100/60">Voice</label>
        <select
          value={settings.tts.voice}
          onChange={(e) =>
            void apply({ tts: { voice: e.target.value } })
          }
          className="mt-1 w-full rounded-md border border-white/10 bg-black/40 px-3 py-2 text-sm text-ink-100"
        >
          {(voices.length > 0 ? voices : [settings.tts.voice]).map(
            (v) => (
              <option key={v} value={v}>
                {v}
              </option>
            ),
          )}
        </select>
        <label className="mt-3 flex items-center gap-2 text-xs text-ink-100/70">
          <input
            type="checkbox"
            checked={settings.tts.enabled}
            onChange={(e) =>
              void apply({ tts: { enabled: e.target.checked } })
            }
          />
          Speak responses out loud
        </label>
        <Row label="Provider" value={settings.tts.provider} />
      </Section>

      <Section title="Audio devices">
        <p className="mb-3 text-[11px] text-ink-100/50">
          Audio capture and playback now run in this browser /
          desktop window. Aiko's voice plays through the device
          you pick here, and the microphone is shared across
          all connected windows with a one-at-a-time lock
          (whoever clicks the mic last gets the floor).
        </p>
        {micPermission !== "granted" ? (
          <div className="mb-3 rounded-md border border-amber-300/40 bg-amber-500/10 p-3 text-[11px] text-amber-100/90">
            <div className="font-medium">
              Microphone permission required
            </div>
            <div className="mt-1 text-amber-100/70">
              Click the mic button (or grant access here) so we
              can list the available input devices with their
              real names.
            </div>
            <button
              type="button"
              onClick={async () => {
                const mod = await import("../../audio/DeviceManager");
                const ok = await mod.requestMicPermission();
                if (ok) {
                  setMicPermission("granted");
                  setDeviceLists(await mod.listDevices());
                } else {
                  setMicPermission("denied");
                }
              }}
              className="mt-2 rounded-md border border-amber-300/60 bg-amber-500/20 px-3 py-1 text-amber-100 hover:bg-amber-500/30"
            >
              Grant microphone access
            </button>
          </div>
        ) : null}
        <label className="block text-xs text-ink-100/60">
          Microphone
        </label>
        <select
          value={inputDeviceId}
          onChange={async (e) => {
            const id = e.target.value;
            setInputDeviceId(id);
            const mod = await import("../../audio/DeviceManager");
            mod.setStoredInputDeviceId(id);
          }}
          className="mt-1 w-full rounded-md border border-white/10 bg-black/40 px-3 py-2 text-sm text-ink-100"
        >
          <option value="">System default</option>
          {deviceLists.inputs.map((d, idx) => (
            <option key={d.deviceId || `in-${idx}`} value={d.deviceId}>
              {d.label || `Microphone ${idx + 1}`}
            </option>
          ))}
        </select>
        <label className="mt-3 block text-xs text-ink-100/60">
          Output
        </label>
        <select
          value={outputDeviceId}
          onChange={async (e) => {
            const id = e.target.value;
            setOutputDeviceId(id);
            const mod = await import("../../audio/DeviceManager");
            mod.setStoredOutputDeviceId(id);
          }}
          className="mt-1 w-full rounded-md border border-white/10 bg-black/40 px-3 py-2 text-sm text-ink-100"
        >
          <option value="">System default</option>
          {deviceLists.outputs.map((d, idx) => (
            <option key={d.deviceId || `out-${idx}`} value={d.deviceId}>
              {d.label || `Speaker ${idx + 1}`}
            </option>
          ))}
        </select>
        <Row label="STT model" value={settings.stt.model} />
        <label className="mt-3 flex items-center gap-2 text-xs text-ink-100/70">
          <input
            type="checkbox"
            checked={settings.audio.barge_in_enabled}
            onChange={(e) =>
              void apply({
                audio: { barge_in_enabled: e.target.checked },
              })
            }
          />
          Allow barge-in (interrupt while Aiko is speaking)
        </label>
      </Section>

      <Section title="Microphone DSP">
        <p className="text-[11px] text-ink-100/50">
          Browser-level audio processing applied before frames
          are sent to the server. Defaults match modern
          videoconferencing clients; turn one off if your model
          already cleans up the signal itself.
        </p>
        <label className="mt-3 flex items-center justify-between gap-3 text-xs text-ink-100/80">
          <div>
            <div className="font-medium">Echo cancellation</div>
            <div className="text-[10px] text-ink-100/50">
              Removes the assistant's voice from the
              microphone feed when you have her on speakers.
            </div>
          </div>
          <input
            type="checkbox"
            checked={dspPrefs.echoCancellation}
            onChange={async (e) => {
              const next = {
                ...dspPrefs,
                echoCancellation: e.target.checked,
              };
              setDspPrefs(next);
              const mod = await import("../../audio/DeviceManager");
              mod.setStoredDspPreferences({
                echoCancellation: next.echoCancellation,
              });
            }}
          />
        </label>
        <label className="mt-2 flex items-center justify-between gap-3 text-xs text-ink-100/80">
          <div>
            <div className="font-medium">Noise suppression</div>
            <div className="text-[10px] text-ink-100/50">
              Cuts steady background noise (fans, traffic) at
              the cost of softer breaths/whispers.
            </div>
          </div>
          <input
            type="checkbox"
            checked={dspPrefs.noiseSuppression}
            onChange={async (e) => {
              const next = {
                ...dspPrefs,
                noiseSuppression: e.target.checked,
              };
              setDspPrefs(next);
              const mod = await import("../../audio/DeviceManager");
              mod.setStoredDspPreferences({
                noiseSuppression: next.noiseSuppression,
              });
            }}
          />
        </label>
        <label className="mt-2 flex items-center justify-between gap-3 text-xs text-ink-100/80">
          <div>
            <div className="font-medium">Auto gain control</div>
            <div className="text-[10px] text-ink-100/50">
              Keeps your level steady as you move toward / away
              from the mic. Disable for studio-quality input.
            </div>
          </div>
          <input
            type="checkbox"
            checked={dspPrefs.autoGainControl}
            onChange={async (e) => {
              const next = {
                ...dspPrefs,
                autoGainControl: e.target.checked,
              };
              setDspPrefs(next);
              const mod = await import("../../audio/DeviceManager");
              mod.setStoredDspPreferences({
                autoGainControl: next.autoGainControl,
              });
            }}
          />
        </label>
        <VoiceOwnerRow />
      </Section>

      <Section title="Proactive nudges">
        <p className="text-[11px] text-ink-100/50">
          When you've been quiet, Aiko can pick up a thread on her
          own. Voice mode and typed chat are tuned independently
          because the right cadence is very different.
        </p>
        <p className="mt-3 text-[11px] font-semibold text-ink-100/70">
          In voice mode
        </p>
        <Row
          label="Silence threshold (s)"
          value={
            <input
              type="number"
              min={10}
              step={5}
              value={settings.proactive?.silence_seconds ?? 45}
              onChange={(e) =>
                void apply({
                  proactive: {
                    silence_seconds: Number(e.target.value),
                  },
                })
              }
              className="w-24 rounded border border-white/10 bg-black/40 px-2 py-1 text-right text-xs text-ink-100"
            />
          }
        />
        <Row
          label="Cooldown (s)"
          value={
            <input
              type="number"
              min={30}
              step={10}
              value={settings.proactive?.cooldown_seconds ?? 120}
              onChange={(e) =>
                void apply({
                  proactive: {
                    cooldown_seconds: Number(e.target.value),
                  },
                })
              }
              className="w-24 rounded border border-white/10 bg-black/40 px-2 py-1 text-right text-xs text-ink-100"
            />
          }
        />
        <p className="mt-4 text-[11px] font-semibold text-ink-100/70">
          In typed chat
        </p>
        <p className="text-[11px] text-ink-100/50">
          Aiko may speak first when you've been quiet a while. Only
          fires while the app window is in focus — backgrounded
          tabs and alt-tabbed windows pause the timer.
        </p>
        <label className="mt-2 flex items-center gap-2 text-xs text-ink-100/70">
          <input
            type="checkbox"
            checked={settings.proactive?.typed_enabled ?? true}
            onChange={(e) =>
              void apply({
                proactive: { typed_enabled: e.target.checked },
              })
            }
          />
          Let Aiko speak first in typed chat
        </label>
        <Row
          label="Silence threshold (s)"
          value={
            <input
              type="number"
              min={60}
              step={10}
              value={
                settings.proactive?.silence_seconds_typed ?? 240
              }
              onChange={(e) =>
                void apply({
                  proactive: {
                    silence_seconds_typed: Number(e.target.value),
                  },
                })
              }
              disabled={
                !(settings.proactive?.typed_enabled ?? true)
              }
              className="w-24 rounded border border-white/10 bg-black/40 px-2 py-1 text-right text-xs text-ink-100 disabled:opacity-40"
            />
          }
        />
        <Row
          label="Cooldown (s)"
          value={
            <input
              type="number"
              min={120}
              step={30}
              value={
                settings.proactive?.cooldown_seconds_typed ?? 600
              }
              onChange={(e) =>
                void apply({
                  proactive: {
                    cooldown_seconds_typed: Number(e.target.value),
                  },
                })
              }
              disabled={
                !(settings.proactive?.typed_enabled ?? true)
              }
              className="w-24 rounded border border-white/10 bg-black/40 px-2 py-1 text-right text-xs text-ink-100 disabled:opacity-40"
            />
          }
        />
        {/*
         * Opt-in to "talk even when I'm not at the window".
         * Default is OFF: typed proactive respects window
         * visibility / focus and disarms when every Aiko
         * window is hidden or alt-tabbed away. Flipping this
         * on bypasses the presence gate so the silence
         * timer fires regardless. See
         * ``SessionController._is_typed_proactive_eligible``
         * for the exact gate.
         */}
        <label className="mt-2 flex items-center gap-2 text-xs text-ink-100/70">
          <input
            type="checkbox"
            checked={settings.proactive?.typed_when_away ?? false}
            onChange={(e) =>
              void apply({
                proactive: { typed_when_away: e.target.checked },
              })
            }
            disabled={
              !(settings.proactive?.typed_enabled ?? true)
            }
          />
          Chime in even when I'm not at the window
        </label>
      </Section>

      <Section title="Activity awareness (desktop)">
        <p className="text-[11px] text-ink-100/50">
          Desktop only. Aiko can see which app is in the foreground
          (just the app name — no window titles, no URLs) and may
          reference it when natural. Off by default. Browser tabs
          see the toggle but it's a no-op there.
        </p>
        <label className="mt-2 flex items-center gap-2 text-xs text-ink-100/70">
          <input
            type="checkbox"
            checked={
              settings.activity?.awareness_enabled ?? false
            }
            onChange={(e) =>
              void apply({
                activity: { awareness_enabled: e.target.checked },
              })
            }
          />
          Share the foreground app name with Aiko
        </label>
        {settings.activity?.awareness_enabled ? (
          <p className="mt-2 text-[11px] text-ink-100/60">
            {tauri ? (
              <>
                Currently sees:{" "}
                <span className="font-mono text-ink-100/80">
                  {liveActiveApp ?? "—"}
                </span>
              </>
            ) : (
              <em>
                Browser shell — desktop app required to share
                foreground state.
              </em>
            )}
          </p>
        ) : null}
      </Section>

      <Section title="Endpointing (when do I stop listening?)">
        <p className="text-[11px] text-ink-100/50">
          Aiko waits for silence to know your turn is over. With
          hesitation extension on, words like "um", "and...", or
          "you know" reset the silence clock so you have the full
          end-of-thought window to find the next word.
        </p>
        <Row
          label="End-of-thought wait (s)"
          value={
            <input
              type="number"
              min={1}
              max={5}
              step={0.1}
              value={
                settings.endpointing?.turn_silence_seconds ?? 3.0
              }
              onChange={(e) =>
                void apply({
                  endpointing: {
                    turn_silence_seconds: Number(e.target.value),
                  },
                })
              }
              className="w-24 rounded border border-white/10 bg-black/40 px-2 py-1 text-right text-xs text-ink-100"
            />
          }
        />
        <Row
          label="Quick-close wait (s)"
          value={
            <input
              type="number"
              min={0.4}
              max={2}
              step={0.1}
              value={
                settings.endpointing?.phrase_silence_seconds ?? 1.0
              }
              onChange={(e) =>
                void apply({
                  endpointing: {
                    phrase_silence_seconds: Number(e.target.value),
                  },
                })
              }
              className="w-24 rounded border border-white/10 bg-black/40 px-2 py-1 text-right text-xs text-ink-100"
            />
          }
        />
        <Row
          label="Sustained-speech for barge-in (s)"
          value={
            <input
              type="number"
              min={0.2}
              max={1.5}
              step={0.1}
              value={
                settings.endpointing?.barge_in_min_speech_seconds ?? 0.7
              }
              onChange={(e) =>
                void apply({
                  endpointing: {
                    barge_in_min_speech_seconds: Number(e.target.value),
                  },
                })
              }
              className="w-24 rounded border border-white/10 bg-black/40 px-2 py-1 text-right text-xs text-ink-100"
            />
          }
        />
        <label className="mt-1 flex items-center gap-2 text-xs text-ink-100/70">
          <input
            type="checkbox"
            checked={
              settings.endpointing?.hesitation_extend_to_turn ?? true
            }
            onChange={(e) =>
              void apply({
                endpointing: {
                  hesitation_extend_to_turn: e.target.checked,
                },
              })
            }
          />
          Extend capture on hesitation words ("um", "and", "you
          know"…)
        </label>
      </Section>
    </>
  );
}

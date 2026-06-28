import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "@/api";
import {
  getStoredDspPreferences,
  getStoredInputDeviceId,
  getStoredOutputDeviceId,
  listDevices,
  onDeviceListChange,
  queryMicPermission,
  requestMicPermission,
  setStoredDspPreferences,
  setStoredInputDeviceId,
  setStoredOutputDeviceId,
  type DeviceListing,
  type DspPreferences,
  type MicPermissionState,
} from "@/audio/DeviceManager";
import { useAssistantStore } from "@/store";

type OnboardingStep = "name" | "audio";

/**
 * Blocking first-run modal that asks the user for the name Aiko should
 * use when referring to them. Shown exactly when ``identity.needs_onboarding``
 * is true; closes when the backend confirms persistence via the
 * ``identity_changed`` WS broadcast (which flips the gate to false).
 *
 * Intentionally not dismissable -- every prompt block, transcript
 * formatter, and worker LLM call routes through ``user_display_name``,
 * so letting the modal be skipped would leak the ``"friend"`` fallback
 * into long-term memory rows.
 *
 * After the name step we walk the user through audio setup:
 * microphone permission, the input/output device pickers, a level meter
 * for sanity, and a "play test sound" button so they know Aiko's voice
 * will land on the right speakers before they start talking. The device
 * preferences live in ``localStorage`` (see ``DeviceManager``) so they
 * survive reloads without round-tripping through the backend.
 *
 * A re-opener for renames lives in the General tab of
 * :file:`SettingsDrawer.tsx`; this component only handles the empty-state
 * onboarding path.
 */
export function FirstRunOnboarding() {
  const identity = useAssistantStore((s) => s.identity);
  const setIdentity = useAssistantStore((s) => s.setIdentity);
  const pushToast = useAssistantStore((s) => s.pushToast);

  const [step, setStep] = useState<OnboardingStep>("name");
  const [name, setName] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement | null>(null);

  useEffect(() => {
    if (identity?.needs_onboarding && step === "name") {
      inputRef.current?.focus();
    }
  }, [identity?.needs_onboarding, step]);

  const submit = useCallback(
    async (event?: React.FormEvent) => {
      event?.preventDefault();
      const cleaned = name.trim();
      if (!cleaned) {
        setError("Please tell Aiko what to call you.");
        inputRef.current?.focus();
        return;
      }
      if (cleaned.length > 32) {
        setError("Keep it under 32 characters.");
        return;
      }
      setSubmitting(true);
      setError(null);
      try {
        const next = await api.setIdentity(cleaned);
        // The WS broadcast usually beats this response, but we still
        // want to advance to the audio step regardless of which
        // arrives first. ``identity.needs_onboarding`` may already be
        // false here, hence the explicit ``setStep`` below.
        setIdentity(next);
        pushToast("info", `Aiko will call you ${next.user_display_name}.`);
        setStep("audio");
      } catch (err) {
        const message =
          err instanceof Error ? err.message : "Couldn't save the name.";
        setError(message);
      } finally {
        setSubmitting(false);
      }
    },
    [name, setIdentity, pushToast],
  );

  if (!identity || (!identity.needs_onboarding && step === "name")) {
    return null;
  }

  return (
    <div
      className="fixed inset-0 z-[60] flex items-center justify-center bg-black/70 backdrop-blur-sm"
      role="dialog"
      aria-modal="true"
      aria-labelledby="first-run-title"
    >
      {step === "name" ? (
        <NameStep
          name={name}
          setName={setName}
          submitting={submitting}
          error={error}
          setError={setError}
          submit={submit}
          inputRef={inputRef}
        />
      ) : (
        <AudioStep onDone={() => setStep("name")} onClose={() => undefined} />
      )}
    </div>
  );
}

function NameStep({
  name,
  setName,
  submitting,
  error,
  setError,
  submit,
  inputRef,
}: {
  name: string;
  setName: (next: string) => void;
  submitting: boolean;
  error: string | null;
  setError: (next: string | null) => void;
  submit: (event?: React.FormEvent) => void | Promise<void>;
  inputRef: React.MutableRefObject<HTMLInputElement | null>;
}) {
  return (
    <form
      onSubmit={submit}
      className="w-[min(420px,calc(100vw-2rem))] rounded-2xl border border-white/10 bg-neutral-900 p-6 shadow-2xl"
    >
      <h2 id="first-run-title" className="text-lg font-semibold text-neutral-100">
        Hi! What should Aiko call you?
      </h2>
      <p className="mt-2 text-sm text-neutral-400">
        Aiko will use this in chat, in her inner thoughts, and when she
        tells stories about your time together. You can change it later
        in Settings.
      </p>
      <label className="mt-5 block">
        <span className="sr-only">Your name</span>
        <input
          ref={inputRef}
          type="text"
          value={name}
          onChange={(e) => {
            setName(e.target.value);
            if (error) setError(null);
          }}
          maxLength={32}
          autoComplete="off"
          spellCheck={false}
          placeholder="Your name"
          disabled={submitting}
          className="block w-full rounded-lg border border-neutral-700 bg-neutral-800 px-3 py-2 text-base text-neutral-100 placeholder:text-neutral-500 focus:border-sky-500 focus:outline-none focus:ring-1 focus:ring-sky-500 disabled:opacity-60"
        />
      </label>
      {error ? (
        <p className="mt-2 text-sm text-rose-400" role="alert">
          {error}
        </p>
      ) : null}
      <div className="mt-6 flex justify-end">
        <button
          type="submit"
          disabled={submitting || name.trim().length === 0}
          className="rounded-lg bg-sky-600 px-4 py-2 text-sm font-medium text-white shadow hover:bg-sky-500 focus:outline-none focus:ring-2 focus:ring-sky-400 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {submitting ? "Saving…" : "Meet Aiko"}
        </button>
      </div>
    </form>
  );
}

/**
 * Step 2: audio devices. Lets the user grant microphone permission,
 * pick their preferred input + output, glance at a level meter, and
 * trigger a test tone routed through the chosen output sink. None of
 * these settings round-trip through the backend — they live in
 * ``localStorage`` so the next launch remembers them.
 */
function AudioStep({ onDone }: { onDone: () => void; onClose: () => void }) {
  const [inputs, setInputs] = useState<DeviceListing[]>([]);
  const [outputs, setOutputs] = useState<DeviceListing[]>([]);
  const [inputId, setInputId] = useState<string>("");
  const [outputId, setOutputId] = useState<string>("");
  const [permission, setPermission] = useState<MicPermissionState>("unknown");
  const [level, setLevel] = useState<number>(0);
  const [dsp, setDsp] = useState<DspPreferences>({
    echoCancellation: true,
    noiseSuppression: true,
    autoGainControl: true,
  });

  const audioCtxRef = useRef<AudioContext | null>(null);
  const analyserRef = useRef<AnalyserNode | null>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const rafRef = useRef<number | null>(null);
  const testAudioRef = useRef<HTMLAudioElement | null>(null);

  useEffect(() => {
    let cancelled = false;
    setInputId(getStoredInputDeviceId());
    setOutputId(getStoredOutputDeviceId());
    setDsp(getStoredDspPreferences());

    const refresh = async () => {
      const lists = await listDevices();
      if (!cancelled) {
        setInputs(lists.inputs);
        setOutputs(lists.outputs);
      }
    };
    void queryMicPermission().then((state) => {
      if (!cancelled) setPermission(state);
    });
    void refresh();
    const unsub = onDeviceListChange(() => void refresh());
    return () => {
      cancelled = true;
      unsub();
    };
  }, []);

  // Tear down the level-meter audio graph on unmount so we don't leak
  // a held microphone when the user advances past the modal.
  useEffect(() => {
    return () => {
      stopLevelMeter();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const stopLevelMeter = () => {
    if (rafRef.current !== null) {
      cancelAnimationFrame(rafRef.current);
      rafRef.current = null;
    }
    if (streamRef.current) {
      for (const track of streamRef.current.getTracks()) {
        try {
          track.stop();
        } catch {
          /* ignore */
        }
      }
      streamRef.current = null;
    }
    if (audioCtxRef.current) {
      void audioCtxRef.current.close().catch(() => {
        /* already closed */
      });
      audioCtxRef.current = null;
    }
    analyserRef.current = null;
    setLevel(0);
  };

  const startLevelMeter = async () => {
    stopLevelMeter();
    try {
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          deviceId: inputId ? { exact: inputId } : undefined,
          echoCancellation: dsp.echoCancellation,
          noiseSuppression: dsp.noiseSuppression,
          autoGainControl: dsp.autoGainControl,
        },
        video: false,
      });
      streamRef.current = stream;
      const AC =
        window.AudioContext ||
        (window as unknown as { webkitAudioContext: typeof AudioContext })
          .webkitAudioContext;
      const ctx = new AC();
      audioCtxRef.current = ctx;
      const source = ctx.createMediaStreamSource(stream);
      const analyser = ctx.createAnalyser();
      analyser.fftSize = 512;
      source.connect(analyser);
      analyserRef.current = analyser;
      const buf = new Uint8Array(analyser.fftSize);
      const tick = () => {
        if (!analyserRef.current) return;
        analyserRef.current.getByteTimeDomainData(buf);
        let sumSq = 0;
        for (let i = 0; i < buf.length; i++) {
          const v = (buf[i] - 128) / 128;
          sumSq += v * v;
        }
        const rms = Math.sqrt(sumSq / buf.length);
        setLevel(Math.min(1, rms * 3));
        rafRef.current = requestAnimationFrame(tick);
      };
      rafRef.current = requestAnimationFrame(tick);
      setPermission("granted");
    } catch (err) {
      console.warn("Level meter failed", err);
      setPermission("denied");
    }
  };

  const playTestSound = async () => {
    if (!testAudioRef.current) {
      const el = document.createElement("audio");
      // Synthesise a quick 0.4s 440 Hz tone fade-in/out so we don't
      // ship a binary asset just for the onboarding chime.
      const ctx = new AudioContext();
      const sampleCount = ctx.sampleRate * 0.4;
      const buffer = ctx.createBuffer(1, sampleCount, ctx.sampleRate);
      const data = buffer.getChannelData(0);
      for (let i = 0; i < sampleCount; i++) {
        const t = i / ctx.sampleRate;
        const envelope = Math.min(1, t / 0.05) * Math.min(1, (0.4 - t) / 0.1);
        data[i] = 0.4 * envelope * Math.sin(2 * Math.PI * 440 * t);
      }
      const dest = ctx.createMediaStreamDestination();
      const source = ctx.createBufferSource();
      source.buffer = buffer;
      source.connect(dest);
      source.start();
      el.srcObject = dest.stream;
      el.autoplay = true;
      testAudioRef.current = el;
    }
    const el = testAudioRef.current;
    if (outputId && "setSinkId" in HTMLMediaElement.prototype) {
      const withSink = el as unknown as {
        setSinkId: (id: string) => Promise<void>;
      };
      try {
        await withSink.setSinkId(outputId);
      } catch (err) {
        console.warn("setSinkId failed during onboarding", err);
      }
    }
    el.currentTime = 0;
    try {
      await el.play();
    } catch (err) {
      console.warn("Test sound play() rejected", err);
    }
  };

  return (
    <form
      className="w-[min(480px,calc(100vw-2rem))] rounded-2xl border border-white/10 bg-neutral-900 p-6 shadow-2xl"
      onSubmit={(e) => {
        e.preventDefault();
        stopLevelMeter();
        onDone();
      }}
    >
      <h2 id="first-run-title" className="text-lg font-semibold text-neutral-100">
        Set up your microphone and speakers
      </h2>
      <p className="mt-2 text-sm text-neutral-400">
        Audio capture and playback run inside this window. Aiko will
        always remember these choices.
      </p>

      {permission !== "granted" ? (
        <div className="mt-4 rounded-md border border-amber-300/40 bg-amber-500/10 p-3 text-sm text-amber-100/90">
          <div className="font-medium">Microphone access</div>
          <p className="mt-1 text-amber-100/70">
            We need permission to record voice. The mic stays off until
            you click the mic button — this just unlocks the device list.
          </p>
          <button
            type="button"
            onClick={async () => {
              const ok = await requestMicPermission();
              if (ok) {
                setPermission("granted");
                const lists = await listDevices();
                setInputs(lists.inputs);
                setOutputs(lists.outputs);
              } else {
                setPermission("denied");
              }
            }}
            className="mt-2 rounded-md border border-amber-300/60 bg-amber-500/20 px-3 py-1.5 text-amber-100 hover:bg-amber-500/30"
          >
            Grant microphone access
          </button>
        </div>
      ) : null}

      <label className="mt-4 block text-xs text-neutral-400">Microphone</label>
      <select
        value={inputId}
        onChange={(e) => {
          setInputId(e.target.value);
          setStoredInputDeviceId(e.target.value);
        }}
        className="mt-1 w-full rounded-md border border-neutral-700 bg-neutral-800 px-3 py-2 text-sm text-neutral-100"
      >
        <option value="">System default</option>
        {inputs.map((d, idx) => (
          <option key={d.deviceId || `in-${idx}`} value={d.deviceId}>
            {d.label || `Microphone ${idx + 1}`}
          </option>
        ))}
      </select>

      <div className="mt-3 flex items-center gap-3">
        <button
          type="button"
          onClick={() => {
            if (rafRef.current === null) void startLevelMeter();
            else stopLevelMeter();
          }}
          className="rounded-md border border-neutral-700 bg-neutral-800 px-3 py-1.5 text-xs text-neutral-100 hover:bg-neutral-700"
        >
          {rafRef.current === null ? "Test microphone" : "Stop test"}
        </button>
        <div className="relative h-2 flex-1 overflow-hidden rounded-full bg-neutral-800">
          <div
            className="absolute inset-y-0 left-0 bg-emerald-400/80 transition-[width] duration-75"
            style={{ width: `${Math.round(level * 100)}%` }}
          />
        </div>
      </div>

      <label className="mt-4 block text-xs text-neutral-400">Speakers</label>
      <select
        value={outputId}
        onChange={(e) => {
          setOutputId(e.target.value);
          setStoredOutputDeviceId(e.target.value);
        }}
        className="mt-1 w-full rounded-md border border-neutral-700 bg-neutral-800 px-3 py-2 text-sm text-neutral-100"
      >
        <option value="">System default</option>
        {outputs.map((d, idx) => (
          <option key={d.deviceId || `out-${idx}`} value={d.deviceId}>
            {d.label || `Speaker ${idx + 1}`}
          </option>
        ))}
      </select>
      <button
        type="button"
        onClick={() => void playTestSound()}
        className="mt-2 rounded-md border border-neutral-700 bg-neutral-800 px-3 py-1.5 text-xs text-neutral-100 hover:bg-neutral-700"
      >
        Play test sound
      </button>

      <fieldset className="mt-4 border-t border-neutral-800 pt-3">
        <legend className="text-xs uppercase tracking-wide text-neutral-500">
          Microphone DSP
        </legend>
        <label className="mt-2 flex items-center justify-between gap-3 text-xs text-neutral-300">
          <span>Echo cancellation</span>
          <input
            type="checkbox"
            checked={dsp.echoCancellation}
            onChange={(e) => {
              const next = { ...dsp, echoCancellation: e.target.checked };
              setDsp(next);
              setStoredDspPreferences({ echoCancellation: next.echoCancellation });
            }}
          />
        </label>
        <label className="mt-1 flex items-center justify-between gap-3 text-xs text-neutral-300">
          <span>Noise suppression</span>
          <input
            type="checkbox"
            checked={dsp.noiseSuppression}
            onChange={(e) => {
              const next = { ...dsp, noiseSuppression: e.target.checked };
              setDsp(next);
              setStoredDspPreferences({ noiseSuppression: next.noiseSuppression });
            }}
          />
        </label>
        <label className="mt-1 flex items-center justify-between gap-3 text-xs text-neutral-300">
          <span>Auto gain control</span>
          <input
            type="checkbox"
            checked={dsp.autoGainControl}
            onChange={(e) => {
              const next = { ...dsp, autoGainControl: e.target.checked };
              setDsp(next);
              setStoredDspPreferences({ autoGainControl: next.autoGainControl });
            }}
          />
        </label>
      </fieldset>

      <div className="mt-6 flex justify-end">
        <button
          type="submit"
          className="rounded-lg bg-sky-600 px-4 py-2 text-sm font-medium text-white shadow hover:bg-sky-500"
        >
          Done
        </button>
      </div>
    </form>
  );
}

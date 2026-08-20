import type { AssistantSettings, TtsProviderInfo } from "../../types";

interface Props {
  settings: AssistantSettings;
  apply: (patch: Record<string, unknown>) => Promise<void>;
}

/**
 * Engine and device selection for TTS.
 *
 * Was a read-only row showing `settings.tts.provider`, which was honest
 * about the old state of things: the setting was stored and switchable
 * over the API but the factory ignored it, so every value meant
 * pocket-tts.
 *
 * Unavailable engines are listed and disabled rather than filtered out.
 * They are unavailable because a virtualenv has not been created, which
 * is a thing the user can fix in one command — and a dropdown that
 * simply omits Chatterbox gives no hint that it exists or how to get it.
 */
export function TtsEnginePicker({ settings, apply }: Props) {
  const catalogue: TtsProviderInfo[] = settings.tts.providers ?? [];
  const current =
    catalogue.find((p) => p.name === settings.tts.provider) ?? null;
  const devices = current?.devices ?? [];

  // No device row when the engine has only one option. pocket-tts is
  // CPU-only, so offering it a choice would be a lie.
  const showDevices = devices.length > 1;

  return (
    <>
      <label className="block text-xs text-ink-100/60">Engine</label>
      <select
        value={settings.tts.provider}
        onChange={(e) => void apply({ tts: { provider: e.target.value } })}
        className="mt-1 w-full rounded-md border border-white/10 bg-black/40 px-3 py-2 text-sm text-ink-100"
      >
        {catalogue.length === 0 ? (
          <option value={settings.tts.provider}>
            {settings.tts.provider}
          </option>
        ) : (
          catalogue.map((provider) => (
            <option
              key={provider.name}
              value={provider.name}
              disabled={!provider.available}
            >
              {provider.label}
              {provider.available ? "" : " — not installed"}
            </option>
          ))
        )}
      </select>

      {current && !current.available ? (
        <p className="mt-1 text-[11px] text-amber-300/80">{current.reason}</p>
      ) : null}

      {current?.notes ? (
        <p className="mt-1 text-[11px] text-ink-100/50">{current.notes}</p>
      ) : null}

      {showDevices ? (
        <>
          <label className="mt-3 block text-xs text-ink-100/60">
            Device
          </label>
          <div className="mt-1 flex gap-2">
            {["auto", ...devices].map((device) => {
              // Compared against the *effective* device rather than the
              // configured one, so "auto" never looks selected while the
              // engine is actually somewhere else.
              const active = settings.tts.device === device;
              return (
                <button
                  key={device}
                  type="button"
                  onClick={() => void apply({ tts: { device } })}
                  className={
                    "flex-1 rounded-md border px-3 py-1.5 text-xs " +
                    (active
                      ? "border-accent/60 bg-accent/20 text-ink-100"
                      : "border-white/10 bg-black/40 text-ink-100/70 hover:bg-black/60")
                  }
                >
                  {device}
                </button>
              );
            })}
          </div>
          <p className="mt-1 text-[11px] text-ink-100/50">
            Running on <span className="text-ink-100/80">
              {settings.tts.device ?? "cpu"}
            </span>
            . GPU is faster for the larger engines but competes with games
            for frame time; CPU stays out of the way.
          </p>
        </>
      ) : null}

      {current?.voice_kind === "clip" ? (
        <p className="mt-2 text-[11px] text-ink-100/50">
          This engine clones from a reference clip on each load, so the
          voice list below is wav files under <code>voices/</code> rather
          than saved speaker embeddings.
        </p>
      ) : null}
    </>
  );
}

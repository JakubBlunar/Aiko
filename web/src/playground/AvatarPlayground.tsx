import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { Live2DAvatar } from "@/features/avatar/Live2DAvatar";
import { MIST_COLORS, mistWeights, type MistColor } from "@/features/avatar/moodMistPalette";
import { useAssistantStore } from "@/store";
import type { MoodState, ResolvedOutfit } from "@/types";
import { MOOD_LABELS, PRESETS, pretty, type MoodPreset, type PlaygroundProfile } from "./presets";
import "./playground.css";

function Field({ label, children }: { label: string; children: ReactNode }) {
  return <label className="playground-field"><span>{label}</span>{children}</label>;
}

function Slider({ label, value, min = 0, max = 1, onChange }: {
  label: string; value: number; min?: number; max?: number; onChange: (value: number) => void;
}) {
  return <Field label={`${label} · ${value.toFixed(2)}`}>
    <input type="range" min={min} max={max} step="0.01" value={value}
      aria-label={label} onChange={(e) => onChange(Number(e.target.value))} />
  </Field>;
}

export function AvatarPlayground({ profile }: { profile: PlaygroundProfile }) {
  const mood = useAssistantStore((s) => s.mood);
  const reaction = useAssistantStore((s) => s.reaction);
  const speaking = useAssistantStore((s) => s.ttsState === "speaking");
  const mistEnabled = useAssistantStore((s) => s.moodMistEnabled);
  const settings = useAssistantStore((s) => s.avatar?.settings ?? profile.settings);
  const outfit = useAssistantStore((s) => s.avatar?.resolved_outfit ?? profile.resolved_outfit ?? "");
  const [title, setTitle] = useState(PRESETS[0].title);
  const [description, setDescription] = useState(PRESETS[0].description);
  const [expression, setExpression] = useState("");
  const [carriedReaction, setCarriedReaction] = useState(PRESETS[0].reaction);
  const [mouth, setMouth] = useState(0);
  const [cycling, setCycling] = useState(false);
  const [presentation, setPresentation] = useState(false);
  const [modelKey, setModelKey] = useState(0);
  const presetIndex = useRef(0);

  const applyPreset = useCallback((preset: MoodPreset) => {
    const store = useAssistantStore.getState();
    store.setVoiceMode("off");
    store.setMood(preset.mood);
    store.setTtsState("speaking", "", preset.reaction);
    store.setAudioAmplitude(0);
    store.setAvatarOverlay(null);
    setExpression("");
    setMouth(0);
    setCarriedReaction(preset.reaction);
    setTitle(preset.title);
    setDescription(preset.description);
  }, []);

  useEffect(() => {
    const store = useAssistantStore.getState();
    store.setAvatar(profile);
    applyPreset(PRESETS[0]);
  }, [profile, applyPreset]);

  useEffect(() => {
    if (!cycling) return;
    const timer = window.setInterval(() => {
      if (document.hidden) return;
      presetIndex.current = (presetIndex.current + 1) % PRESETS.length;
      applyPreset(PRESETS[presetIndex.current]);
    }, 8000);
    return () => window.clearInterval(timer);
  }, [cycling, applyPreset]);

  const custom = () => {
    setCycling(false);
    setTitle("Your expression study");
    setDescription("A lingering feeling and a passing expression, blended your way.");
  };
  const patchMood = (patch: Partial<MoodState>) => {
    custom();
    useAssistantStore.getState().setMood({ ...mood, ...patch });
  };
  const changeReaction = (next: string) => {
    custom();
    setExpression("");
    setCarriedReaction(next);
    useAssistantStore.getState().setTtsState(speaking ? "speaking" : "idle", "", next);
  };
  const weights = mistWeights(mood, reaction, speaking);
  const outfits: [ResolvedOutfit, string][] = [
    ["day", "day_clothes"], ["pajamas", "pajamas"], ["pajamas_hooded", "pajamas_hooded"],
  ];
  const activeExpression = expression ? profile.playground_expressions[expression]
    : profile.reaction_mapping[carriedReaction];

  return <main className={`avatar-playground ${presentation ? "avatar-playground--presentation" : ""}`}>
    <header className="playground-header">
      <div><span className="playground-eyebrow">AIKO / AVATAR PLAYGROUND</span>
        <p>{profile.display_name} <span>· Cubism {profile.cubism_version}</span></p></div>
      <button className="playground-button" onClick={() => setPresentation(!presentation)}>
        {presentation ? "Show controls" : "Presentation view"}
      </button>
    </header>

    <div className="playground-layout">
      <section className="playground-stage" aria-label="Model preview">
        <div className="playground-avatar"><Live2DAvatar key={modelKey} manifest={profile}
          scaleMultiplier={settings.scale_multiplier} /></div>
        <div className="playground-caption"><span className="playground-eyebrow">{speaking ? "SPEAKING PREVIEW / SILENT" : "AT REST"}</span>
          <h1>{title}</h1><p>{description}</p></div>
      </section>

      {!presentation && <aside className="playground-controls" aria-label="Playground controls">
        <section className="playground-section">
          <div className="playground-section-heading"><h2>Start with a feeling</h2>
            <button className={cycling ? "playground-link is-active" : "playground-link"}
              aria-pressed={cycling} onClick={() => setCycling(!cycling)}>
              {cycling ? "Stop showcase" : "Cycle showcase"}
            </button></div>
          <div className="playground-presets">{PRESETS.map((preset, index) =>
            <button key={preset.title} aria-pressed={title === preset.title}
              onClick={() => { presetIndex.current = index; setCycling(false); applyPreset(preset); }}>
              {preset.title}
            </button>)}</div>
        </section>

        <section className="playground-section">
          <h2>Mood & atmosphere</h2>
          <div className="playground-pair">
            <Field label="Lingering mood"><select value={mood.label}
              onChange={(e) => patchMood({ label: e.target.value })}>
              {MOOD_LABELS.map((label) => <option key={label} value={label}>{pretty(label)}</option>)}
            </select></Field>
            <Field label="Spoken reaction"><select value={carriedReaction}
              onChange={(e) => changeReaction(e.target.value)}>
              {profile.playground_reactions.map((label) =>
                <option key={label} value={label}>{pretty(label)}</option>)}
            </select></Field>
          </div>
          <Slider label="Mood intensity" value={mood.intensity} onChange={(intensity) => patchMood({ intensity })} />
          <Slider label="Arousal" value={mood.arousal} onChange={(arousal) => patchMood({ arousal })} />
          <Slider label="Valence" min={-1} value={mood.valence} onChange={(valence) => patchMood({ valence })} />
          <div className="playground-checks">
            <label><input type="checkbox" checked={speaking} onChange={(e) => {
              custom();
              const store = useAssistantStore.getState();
              store.setTtsState(e.target.checked ? "speaking" : "idle", "", expression || carriedReaction);
              if (!e.target.checked) { store.setAudioAmplitude(0); setMouth(0); }
            }} />Speaking preview</label>
            <label><input type="checkbox" checked={mistEnabled}
              onChange={(e) => useAssistantStore.getState().setMoodMistEnabled(e.target.checked)} />Mood mist</label>
          </div>
          <div className="playground-swatches" aria-label="Current atmosphere colors">
            {(Object.keys(MIST_COLORS) as MistColor[]).filter((key) => weights[key] > 0).map((key) =>
              <span key={key}><i style={{ background: MIST_COLORS[key] }} />{pretty(key)} {Math.round(weights[key] * 100)}%</span>)}
          </div>
        </section>

        <section className="playground-section">
          <h2>Explore this model</h2>
          <Field label={`Raw expression · ${profile.expressions.length} available`}>
            <select value={expression} onChange={(e) => {
              custom(); setExpression(e.target.value);
              useAssistantStore.getState().setTtsState(speaking ? "speaking" : "idle", "", e.target.value || carriedReaction);
            }}><option value="">Use reaction mapping</option>
              {Object.entries(profile.playground_expressions).map(([alias, name]) =>
                <option key={alias} value={alias}>{name}</option>)}
            </select>
          </Field>
          <p className="playground-note">Expression target: {activeExpression || "renderer fallback"}. Outfit rules still apply.</p>
          <Slider label="Body language" value={settings.expressiveness} max={1.5}
            onChange={(expressiveness) => useAssistantStore.getState().setAvatarSettings({ expressiveness })} />
          <Slider label="Avatar size" value={settings.scale_multiplier} min={0.5} max={2}
            onChange={(scale_multiplier) => useAssistantStore.getState().setAvatarSettings({ scale_multiplier })} />
          <Slider label="Mouth opening (silent)" value={mouth} onChange={(value) => {
            setMouth(value); setCycling(false);
            const store = useAssistantStore.getState();
            if (value > 0) store.setTtsState("speaking", "", expression || carriedReaction);
            store.setAudioAmplitude(value);
          }} />
          {outfits.some(([, key]) => profile.outfits[key]) && <Field label="Outfit">
            <select value={outfit} onChange={(e) => useAssistantStore.getState().updateAvatarWorldState({
              resolved_outfit: e.target.value as ResolvedOutfit,
            })}><option value="">Model default</option>
              {outfits.filter(([, key]) => profile.outfits[key]).map(([value, name]) =>
              <option key={value} value={value}>{pretty(name)}</option>)}</select>
          </Field>}
          {Object.keys(profile.motions).length > 0 && <Field label="Play a motion">
            <select value="" onChange={(e) => {
              const [group, index] = JSON.parse(e.target.value) as [string, number];
              useAssistantStore.getState().setAvatarMotion({
                name: profile.motions[group][index].name, group, index, firedAt: Date.now(), priority: "force",
              });
            }}><option value="" disabled>Choose a motion</option>
              {Object.entries(profile.motions).flatMap(([group, motions]) => motions.map((motion, index) =>
                <option key={`${group}:${index}`} value={JSON.stringify([group, index])}>{group} / {motion.name}</option>))}
            </select>
          </Field>}
          {Object.keys(profile.overlays).length > 0 && <div className="playground-overlay-list">
            {Object.entries(profile.overlays).map(([name, binding]) => <button key={name}
              onClick={() => useAssistantStore.getState().setAvatarOverlay({
                name, expiresAt: Date.now() + (binding.decay_ms || 1800),
              })}>{binding.label_en || pretty(name)}</button>)}
          </div>}
        </section>

        <footer className="playground-footer"><button className="playground-button" onClick={() => {
          setCycling(false); useAssistantStore.getState().setAvatar(profile);
          useAssistantStore.getState().setAvatarMotion(null);
          applyPreset(PRESETS[0]); setModelKey((key) => key + 1);
        }}>Reset preview</button>
          <p>Local visual sandbox. To load another model, relaunch with <code>--model "path/to/model"</code>.</p>
        </footer>
      </aside>}
    </div>
  </main>;
}

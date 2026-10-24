import { useEffect } from "react";
import { useAssistantStore } from "@/store";
import { MOOD_MIST_STORAGE_KEY } from "@/stores/slices/avatar";
import { readBool } from "@/stores/persist";
import { useWindowVisible } from "@/hooks/useWindowVisible";
import { MIST_COLORS, mistWeights, type MistColor } from "./moodMistPalette";
import "./moodMist.css";

/** Decorative atmosphere only: no rig writes, audio scheduling or semantic
 * playback beats. It also works in the transparent persona window. */
export function MoodMist() {
  const mood = useAssistantStore((s) => s.mood);
  const reaction = useAssistantStore((s) => s.reaction);
  const speaking = useAssistantStore((s) => s.ttsState === "speaking");
  const enabled = useAssistantStore((s) => s.moodMistEnabled);
  const sleep = useAssistantStore((s) => s.sleep?.status);
  const visible = useWindowVisible();
  useEffect(() => {
    // Persona and chat are separate webviews. Apply an appearance change
    // there too, without writing the received storage event back again.
    const sync = () => useAssistantStore.setState({
      moodMistEnabled: readBool(MOOD_MIST_STORAGE_KEY, true),
    });
    const onStorage = (event: StorageEvent) => {
      if (event.key === MOOD_MIST_STORAGE_KEY || event.key === null) sync();
    };
    sync();
    window.addEventListener("storage", onStorage);
    return () => window.removeEventListener("storage", onStorage);
  }, []);
  const weights = mistWeights(mood, reaction, speaking);
  const intensity = Number.isFinite(mood.intensity)
    ? Math.max(0, Math.min(1, mood.intensity)) : 0.5;
  const opacity = sleep === "asleep" ? 0.08 : (speaking ? 0.42 : 0.22) + intensity * 0.08;

  return (
    <div
      aria-hidden="true"
      className="aiko-mood-mist"
      data-active={enabled && visible}
      style={{ opacity: enabled && visible ? opacity : 0 }}
    >
      {(Object.keys(MIST_COLORS) as MistColor[]).map((family) => (
        <div
          key={family}
          className="aiko-mood-mist__color"
          style={{ color: MIST_COLORS[family], opacity: weights[family] }}
        >
          <div className="aiko-mood-mist__veil aiko-mood-mist__veil--near" />
          <div className="aiko-mood-mist__veil aiko-mood-mist__veil--far" />
        </div>
      ))}
    </div>
  );
}

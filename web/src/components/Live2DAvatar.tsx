import { useEffect, useRef } from "react";
import * as PIXI from "pixi.js";
import { Live2DModel, MotionPriority } from "pixi-live2d-display";
import { useAssistantStore } from "../store";
import type { Persona } from "../types";

// Make pixi-live2d-display drive its own ticker via the standard PIXI ticker.
// (The library expects this to be registered exactly once before any model is
// created -- the call is idempotent.)
Live2DModel.registerTicker(PIXI.Ticker);

interface Live2DAvatarProps {
  manifest: Persona;
}

const MOUTH_PARAM_CUBISM_4 = "ParamMouthOpenY";
const MOUTH_PARAM_CUBISM_2 = "PARAM_MOUTH_OPEN_Y";

/**
 * Renders a Live2D model. Drives:
 *   - mouth open via the audio amplitude broadcast from the backend (TTS RMS)
 *   - facial expression via the assistant's last reaction tag
 *   - body motion when TTS starts speaking (manifest.talk_motion_group)
 */
export function Live2DAvatar({ manifest }: Live2DAvatarProps) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const appRef = useRef<PIXI.Application | null>(null);
  const modelRef = useRef<InstanceType<typeof Live2DModel> | null>(null);
  // Smoothed mouth-open value [0..1]; written by the rAF loop, read by Pixi.
  const mouthSmoothRef = useRef<number>(0);
  // Latest reaction we already applied -- avoids re-firing the same expression.
  const lastReactionRef = useRef<string>("");
  // Track talk motion start so we don't spam motion() each token frame.
  const lastTtsStateRef = useRef<"idle" | "speaking">("idle");

  // ── 1. Boot Pixi + load the Live2D model. Reruns when persona changes. ──
  useEffect(() => {
    const container = containerRef.current;
    if (!container) {
      return;
    }

    // Defensive: ensure the relevant runtime is available.
    const w = window as unknown as {
      Live2D?: unknown;
      Live2DCubismCore?: unknown;
    };
    const cubism4Ready = !!w.Live2DCubismCore;
    const cubism2Ready = !!w.Live2D;
    if (manifest.cubism_version === 3 && !cubism4Ready) {
      console.warn(
        "Live2D Cubism 4 runtime missing; expected " +
          "/live2d/live2dcubismcore.min.js to be loaded.",
      );
    }
    if (manifest.cubism_version === 2 && !cubism2Ready) {
      console.warn(
        "Live2D Cubism 2 runtime missing; expected /live2d/live2d.min.js " +
          "to be loaded.",
      );
    }

    const canvas = document.createElement("canvas");
    canvas.style.width = "100%";
    canvas.style.height = "100%";
    canvas.style.display = "block";
    container.appendChild(canvas);

    const app = new PIXI.Application({
      view: canvas,
      autoStart: true,
      backgroundAlpha: 0,
      antialias: true,
      resolution: Math.max(1, window.devicePixelRatio || 1),
      autoDensity: true,
      resizeTo: container,
    });
    appRef.current = app;

    let cancelled = false;
    const url = "/personas/" + manifest.entry_filename.replace(/^\/+/, "");

    Live2DModel.from(url, { autoInteract: false })
      .then((model) => {
        if (cancelled) {
          model.destroy({ children: true });
          return;
        }
        modelRef.current = model;
        app.stage.addChild(model);
        fitModelToContainer(model, app);

        // The first reaction we already have in the store should drive the
        // initial expression so the avatar doesn't pop in with the default.
        const reaction = useAssistantStore.getState().reaction;
        applyReaction(model, manifest, reaction);
        lastReactionRef.current = reaction;
      })
      .catch((err) => {
        console.error("Live2D model failed to load", url, err);
      });

    const handleResize = () => {
      if (!modelRef.current || !appRef.current) {
        return;
      }
      fitModelToContainer(modelRef.current, appRef.current);
    };
    window.addEventListener("resize", handleResize);

    return () => {
      cancelled = true;
      window.removeEventListener("resize", handleResize);
      if (modelRef.current) {
        modelRef.current.destroy({ children: true });
        modelRef.current = null;
      }
      if (appRef.current) {
        appRef.current.destroy(true, { children: true, texture: true });
        appRef.current = null;
      }
      if (canvas.parentElement === container) {
        container.removeChild(canvas);
      }
      mouthSmoothRef.current = 0;
      lastReactionRef.current = "";
      lastTtsStateRef.current = "idle";
    };
  }, [manifest.id, manifest.entry_filename, manifest.cubism_version]);

  // ── 2. Lip-sync: rAF loop reads audioAmplitude from the store and eases ──
  useEffect(() => {
    let raf = 0;
    const tick = () => {
      const model = modelRef.current;
      if (model) {
        const target = useAssistantStore.getState().audioAmplitude || 0;
        // Critically-damped easing toward target -- avoids the step-look that
        // raw 30 Hz amplitude updates would produce on a 60 Hz canvas.
        const prev = mouthSmoothRef.current;
        const next = prev + (target - prev) * 0.35;
        mouthSmoothRef.current = next < 0 ? 0 : next > 1 ? 1 : next;
        applyMouthOpen(model, manifest, mouthSmoothRef.current);
      }
      raf = window.requestAnimationFrame(tick);
    };
    raf = window.requestAnimationFrame(tick);
    return () => window.cancelAnimationFrame(raf);
  }, [manifest.id, manifest.cubism_version]);

  // ── 3. Reaction changes -> expression + talk-motion on speaking start ──
  useEffect(() => {
    const unsub = useAssistantStore.subscribe((state, prev) => {
      const model = modelRef.current;
      if (!model) {
        return;
      }
      if (state.reaction !== prev.reaction) {
        if (state.reaction !== lastReactionRef.current) {
          applyReaction(model, manifest, state.reaction);
          lastReactionRef.current = state.reaction;
        }
      }
      const ts = state.ttsState;
      if (ts !== lastTtsStateRef.current) {
        lastTtsStateRef.current = ts;
        if (ts === "speaking" && manifest.talk_motion_group) {
          try {
            // Random index within the group; library signature: motion(group, index?, priority?).
            (model as unknown as {
              motion: (group: string, index?: number, priority?: number) => void;
            }).motion(
              manifest.talk_motion_group,
              undefined,
              MotionPriority.NORMAL,
            );
          } catch (err) {
            console.debug("talk motion failed", err);
          }
        }
      }
    });
    return () => unsub();
  }, [manifest.id, manifest.reaction_mapping, manifest.talk_motion_group]);

  return (
    <div
      ref={containerRef}
      className="relative h-full w-full"
      aria-label={`${manifest.display_name} (Live2D)`}
    />
  );
}

// ── helpers ─────────────────────────────────────────────────────────────

function fitModelToContainer(
  model: InstanceType<typeof Live2DModel>,
  app: PIXI.Application,
): void {
  // ``getBounds`` reflects the model's untransformed size; downscale until the
  // model fits the panel with a small margin.
  const bounds = model.getBounds();
  if (!bounds.width || !bounds.height) {
    return;
  }
  const margin = 0.92;
  const scale = Math.min(
    (app.screen.width * margin) / bounds.width,
    (app.screen.height * margin) / bounds.height,
  );
  model.scale.set(scale);
  model.x = app.screen.width / 2;
  model.y = app.screen.height;
  model.anchor.set(0.5, 1.0);
}

function applyMouthOpen(
  model: InstanceType<typeof Live2DModel>,
  manifest: Persona,
  level: number,
): void {
  const core = (model as unknown as {
    internalModel: { coreModel: unknown };
  }).internalModel?.coreModel;
  if (!core) {
    return;
  }
  const param =
    manifest.cubism_version === 2 ? MOUTH_PARAM_CUBISM_2 : MOUTH_PARAM_CUBISM_4;
  // The two SDK variants disagree on method names. Try Cubism 4 first, fall
  // back to Cubism 2's legacy API.
  const cm4 = (core as {
    setParameterValueById?: (id: string, value: number) => void;
  }).setParameterValueById;
  if (typeof cm4 === "function") {
    try {
      cm4.call(core, param, level);
      return;
    } catch {
      /* fall through */
    }
  }
  const cm2 = (core as {
    setParamFloat?: (id: string, value: number) => void;
  }).setParamFloat;
  if (typeof cm2 === "function") {
    try {
      cm2.call(core, param, level);
    } catch {
      /* swallow */
    }
  }
}

function applyReaction(
  model: InstanceType<typeof Live2DModel>,
  manifest: Persona,
  reaction: string,
): void {
  const expressionName = manifest.reaction_mapping[reaction];
  if (!expressionName) {
    return;
  }
  try {
    (model as unknown as {
      expression: (name?: string) => void;
    }).expression(expressionName);
  } catch (err) {
    console.debug("expression() failed", expressionName, err);
  }
}

import { useEffect, useRef } from "react";
import * as PIXI from "pixi.js";
import { Live2DModel, MotionPriority } from "pixi-live2d-display";
import { backendBase, isTauri } from "../desktop/runtime";
import { useAssistantStore } from "../store";
import type { AvatarProfile, VoiceMode } from "../types";
import {
  AvatarEngine,
  PixiLive2DAdapter,
  StoreBridge,
  createEngineState,
} from "../live2d";
import type { ChannelStoreSnapshot, MouseSource } from "../live2d";
import { AmbientBodyChannel } from "../live2d/channels/AmbientBodyChannel";
import { AccessoryChannel } from "../live2d/channels/AccessoryChannel";
import { ExpressionChannel } from "../live2d/channels/ExpressionChannel";
import { GazeChannel } from "../live2d/channels/GazeChannel";
import { GestureChannel } from "../live2d/channels/GestureChannel";
import { LipsyncChannel } from "../live2d/channels/LipsyncChannel";
import { MotionChannel } from "../live2d/channels/MotionChannel";
import { OutfitChannel } from "../live2d/channels/OutfitChannel";
import { OverlayChannel } from "../live2d/channels/OverlayChannel";
import { GlobalMouseSource } from "../live2d/GlobalMouseSource";
import { WindowMouseSource } from "../live2d/WindowMouseSource";
import { debugLog } from "../log";

// Make pixi-live2d-display drive its own ticker via the standard PIXI ticker.
// (The library expects this to be registered exactly once before any model is
// created -- the call is idempotent.)
Live2DModel.registerTicker(PIXI.Ticker);

interface Live2DAvatarProps {
  manifest: AvatarProfile;
}

/**
 * Renders a Live2D model.
 *
 * The component is now intentionally thin — it owns Pixi setup,
 * model loading, container resize, and the scheduled idle-motion
 * timer. Every per-frame parameter write (lipsync, expressions,
 * outfits, gaze, body language, gestures) lives in
 * ``web/src/live2d/channels/`` and is driven by ``AvatarEngine``.
 * That makes the channels unit-testable in Node without Pixi or
 * jsdom (see ``web/src/live2d/__fixtures__/`` and
 * ``web/src/live2d/channels/*.test.ts``).
 */
export function Live2DAvatar({ manifest }: Live2DAvatarProps) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const appRef = useRef<PIXI.Application | null>(null);
  const modelRef = useRef<InstanceType<typeof Live2DModel> | null>(null);
  // The engine + bridge handles are kept on refs so the cleanup
  // function in the model-load effect can stop them in the right
  // order (engine-stop must run BEFORE model destroy, otherwise the
  // ``beforeModelUpdate`` listener detach races a destroyed emitter).
  const engineRef = useRef<AvatarEngine | null>(null);
  const engineBridgeRef = useRef<StoreBridge | null>(null);

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
    // The bundled avatar (Alexia by default) is served by FastAPI at
    // ``/avatar/``. ``manifest.entry_filename`` is the file name
    // relative to that mount (e.g. ``"Alexia.model3.json"``). Inside a
    // Tauri webview we resolve through the absolute backend origin
    // because the webview's own origin (``tauri://localhost``) doesn't
    // serve the model files.
    const url =
      backendBase().http +
      "/avatar/" +
      manifest.entry_filename.replace(/^\/+/, "");

    Live2DModel.from(url, { autoInteract: false })
      .then((model) => {
        if (cancelled) {
          model.destroy({ children: true });
          return;
        }
        modelRef.current = model;
        app.stage.addChild(model);
        fitModelToContainer(
          model,
          app,
          manifest.settings.scale_multiplier ?? 1,
        );

        // Spin up the AvatarEngine. The registered channels handle
        // expression / motion / outfit / overlay / lipsync / etc.
        // ``ExpressionChannel.attach`` applies the initial reaction
        // expression so the rig doesn't pop in with the default.
        // See ``web/src/live2d/`` for the architecture.
        const engineState = createEngineState();
        const adapter = new PixiLive2DAdapter(model);
        const containerEl = containerRef.current;
        // Inside the Tauri shell we use a global cursor source so
        // Aiko's gaze tracks the mouse even when it's outside this
        // window — including across monitors. In a regular browser
        // the OS cursor isn't reachable, so we keep the DOM
        // pointermove path. ``GazeChannel`` consumes the same
        // ``MouseSnapshot`` either way.
        const mouseSource: MouseSource | undefined = containerEl
          ? isTauri()
            ? new GlobalMouseSource({ container: containerEl })
            : new WindowMouseSource({ container: containerEl })
          : undefined;
        const engine = new AvatarEngine({
          manifest,
          engineState,
          mouseSource,
          debug: (source, kind, payload) =>
            debugLog.log({ source, kind, payload }),
          getStoreSnapshot: (): ChannelStoreSnapshot => {
            const s = useAssistantStore.getState();
            return {
              reaction: s.reaction,
              ttsState: s.ttsState,
              voiceMode: s.voiceMode,
              turnInProgress: s.turnInProgress,
              audioAmplitude: s.audioAmplitude,
              avatarOverlay: s.avatarOverlay,
              avatarMotion: s.avatarMotion,
              mood: s.mood,
              resolvedOutfit: s.avatar?.resolved_outfit ?? "",
              backchannelHint: s.backchannelHint ?? "",
              circadianPeriod: s.avatar?.circadian_period ?? "",
              expressiveness: s.avatar?.settings?.expressiveness ?? 1,
            };
          },
        });
        engine.register(
          new MotionChannel(),
          new OutfitChannel(),
          new AccessoryChannel(),
          new OverlayChannel(),
          new LipsyncChannel(),
          new ExpressionChannel(),
          new GestureChannel(),
          new GazeChannel(),
          new AmbientBodyChannel(),
        );
        engine.start(adapter);
        const bridge = new StoreBridge(engine, useAssistantStore);
        bridge.start();
        engineRef.current = engine;
        engineBridgeRef.current = bridge;
      })
      .catch((err) => {
        console.error("Live2D model failed to load", url, err);
      });

    // Refit on every container size change.
    //
    // We deliberately observe the *container element*, not the window,
    // because the avatar panel is now resizable from the inline drag
    // handle (see ``PanelResizeHandle`` + ``personaPanelWidth`` in the
    // store). Dragging the handle changes the container's CSS width
    // without resizing the window, so a plain ``window.resize``
    // listener (and PIXI's own ``resizeTo`` machinery, which is also
    // window-driven) misses the event entirely. The model would then
    // keep its previous ``app.screen.width / 2`` x-coordinate, leaving
    // Aiko visibly off-center until the panel was remounted (i.e.
    // app reopen, or detach + close persona window).
    //
    // ResizeObserver fires for the OS window resize *and* CSS-driven
    // size changes. We:
    //   1. ``app.resize()`` -- forces PIXI to re-read its
    //      ``resizeTo`` target so ``app.screen`` matches the new
    //      container box;
    //   2. ``fitModelToContainer`` -- recomputes the scale + the
    //      anchor using the fresh ``app.screen``.
    // Both steps are cheap, but ``getBounds()`` walks the model's
    // display tree, so we coalesce bursts of observer callbacks into
    // one rAF tick. With a 120Hz cursor that drops the work from
    // ~2 calls/frame to 1.
    let rafPending = false;
    const handleResize = () => {
      if (rafPending) return;
      rafPending = true;
      window.requestAnimationFrame(() => {
        rafPending = false;
        const app = appRef.current;
        const model = modelRef.current;
        if (!app) return;
        app.resize();
        if (model) {
          fitModelToContainer(
            model,
            app,
            manifest.settings.scale_multiplier ?? 1,
          );
        }
      });
    };
    const resizeObserver = new ResizeObserver(handleResize);
    resizeObserver.observe(container);

    return () => {
      cancelled = true;
      resizeObserver.disconnect();
      // Stop the engine + bridge BEFORE destroying the model so the
      // ``beforeModelUpdate`` listener detaches cleanly while the
      // emitter is still alive.
      if (engineBridgeRef.current) {
        engineBridgeRef.current.stop();
        engineBridgeRef.current = null;
      }
      if (engineRef.current) {
        engineRef.current.stop();
        engineRef.current = null;
      }
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
    };
  }, [manifest.entry_filename, manifest.cubism_version]);

  // ── 1b. React to scale_multiplier changes without rebuilding the model.
  //    The user can drag a slider in Avatar settings; refit live so the
  //    canvas stays smooth instead of remounting (which flashes).
  useEffect(() => {
    if (!modelRef.current || !appRef.current) {
      return;
    }
    fitModelToContainer(
      modelRef.current,
      appRef.current,
      manifest.settings.scale_multiplier ?? 1,
    );
  }, [manifest.settings.scale_multiplier]);

  // ── 2. Idle motion loop -- only fires when not speaking. Cadence is
  //    biased by Aiko's current mood arousal: more restless = quicker
  //    idle motions, more tired/calm = slower. Falls back to the
  //    8-15 s neutral baseline.
  useEffect(() => {
    const idleGroup = manifest.idle_motion_group;
    if (!idleGroup) {
      return;
    }
    let timeoutId: number | null = null;
    const scheduleNext = () => {
      const moodNow = useAssistantStore.getState().mood;
      const { min, max } = idleCadenceMs(moodNow.label, moodNow.arousal);
      const delay = min + Math.random() * (max - min);
      timeoutId = window.setTimeout(() => {
        const model = modelRef.current;
        const state = useAssistantStore.getState();
        // Don't intrude on speaking, listening (user mid-utterance),
        // or thinking — the body language for those is handled below.
        const blocked =
          state.ttsState === "speaking" ||
          state.voiceMode === "listening" ||
          state.voiceMode === "transcribing" ||
          state.voiceMode === "thinking";
        if (model && !blocked) {
          try {
            (
              model as unknown as {
                motion: (
                  group: string,
                  index?: number,
                  priority?: number,
                ) => void;
              }
            ).motion(idleGroup, undefined, MotionPriority.IDLE);
          } catch (err) {
            console.debug("idle motion failed", err);
          }
        }
        scheduleNext();
      }, delay);
    };
    scheduleNext();
    return () => {
      if (timeoutId !== null) {
        window.clearTimeout(timeoutId);
      }
    };
  }, [manifest.idle_motion_group]);

  // ── 3. Subtle mood tinting on the avatar container. Subscribed via
  // useAssistantStore so it re-renders whenever the WS pushes mood_state.
  const mood = useAssistantStore((s) => s.mood);
  const voiceMode = useAssistantStore((s) => s.voiceMode);
  const turnInProgress = useAssistantStore((s) => s.turnInProgress);
  const ttsState = useAssistantStore((s) => s.ttsState);
  const tintFilter = moodToFilter(mood.label, mood.intensity);
  const auraStyle = stateAura(voiceMode, turnInProgress, ttsState);

  return (
    <div
      className="relative h-full w-full"
      aria-label={`${manifest.display_name} (Live2D)`}
    >
      {auraStyle && (
        <div
          aria-hidden="true"
          className="pointer-events-none absolute inset-0 transition-opacity duration-500 ease-out"
          style={auraStyle}
        />
      )}
      <div
        ref={containerRef}
        className="relative h-full w-full transition-[filter] duration-700 ease-out"
        style={{ filter: tintFilter }}
      />
    </div>
  );
}

// ── helpers ─────────────────────────────────────────────────────────────

function fitModelToContainer(
  model: InstanceType<typeof Live2DModel>,
  app: PIXI.Application,
  multiplier: number = 1,
): void {
  // ``getBounds`` reflects the *currently transformed* size, not the
  // model's natural size, so we have to reset the scale first or we'd
  // compound the previous fit on every resize.
  model.scale.set(1);
  const bounds = model.getBounds();
  if (!bounds.width || !bounds.height) {
    return;
  }
  const margin = 0.92;
  const fit = Math.min(
    (app.screen.width * margin) / bounds.width,
    (app.screen.height * margin) / bounds.height,
  );
  const finalScale = fit * multiplier;
  model.scale.set(finalScale);

  // Anchor strategy:
  //
  //   • Whole model fits inside the canvas → bottom-anchor so the
  //     character "stands on the floor" of the panel, classic
  //     feet-on-floor framing. This is always the case at
  //     ``multiplier <= 1`` (auto-fit guarantees it) and may also
  //     hold at slight upscale if the panel is taller than wide.
  //   • Rendered height overflows the viewport → top-anchor so the
  //     head stays pinned to the top of the panel and the legs are
  //     the part that gets cropped. Without this the previous
  //     "slide anchor up gradually" formula could still leave the
  //     face above the visible area at high zoom levels (the user
  //     would see only the torso/hands).
  //
  // Both branches keep the model horizontally centered.
  const renderedHeight = bounds.height * finalScale;
  const overflowsVertically = renderedHeight > app.screen.height;
  model.x = app.screen.width / 2;
  if (overflowsVertically) {
    model.y = 0;
    model.anchor.set(0.5, 0);
  } else {
    model.y = app.screen.height;
    model.anchor.set(0.5, 1);
  }
}

// CSS-only soft glow overlay that signals listening / thinking /
// speaking. No per-frame rAF cost.
function stateAura(
  voiceMode: VoiceMode,
  turnInProgress: boolean,
  ttsState: "idle" | "speaking",
): React.CSSProperties | null {
  if (ttsState === "speaking") {
    return {
      background:
        "radial-gradient(circle at 50% 70%, rgba(255,210,160,0.18), transparent 65%)",
      opacity: 0.8,
    };
  }
  if (voiceMode === "thinking" || turnInProgress) {
    return {
      background:
        "radial-gradient(circle at 50% 60%, rgba(180,200,255,0.18), transparent 60%)",
      animation: "aikoThinkingPulse 2.4s ease-in-out infinite",
      opacity: 0.85,
    };
  }
  if (voiceMode === "listening" || voiceMode === "transcribing") {
    return {
      background:
        "radial-gradient(circle at 50% 60%, rgba(160,255,200,0.16), transparent 60%)",
      opacity: 0.7,
    };
  }
  return null;
}

// Idle motion cadence biased by mood arousal.
function idleCadenceMs(
  label: string,
  arousal: number,
): { min: number; max: number } {
  const a = Math.max(0, Math.min(1, arousal || 0.4));
  if (label === "tired" || label === "melancholy") {
    return { min: 12000, max: 22000 };
  }
  if (label === "restless" || label === "playful" || label === "curious") {
    return { min: 4500, max: 9500 };
  }
  // Neutral baseline tilted by arousal: more arousal -> quicker idle.
  const base = 8000 + (1 - a) * 4000; // 8000..12000 at low arousal
  return { min: base, max: base + 6000 };
}

// Very subtle CSS filter tinting based on mood label + intensity.
// Kept gentle (max 12% saturation/brightness shift) so the model still
// looks like itself; meant as background atmosphere, not a costume change.
function moodToFilter(label: string, intensity: number): string {
  const i = Math.max(0, Math.min(1, intensity));
  const sat = Math.round(100 + i * 12); // 100..112
  const dim = Math.round(100 - i * 6); // 100..94
  const warm: string = `saturate(${sat}%) brightness(${100 + Math.round(i * 4)}%)`;
  const cool: string = `saturate(${sat}%) brightness(${dim}%)`;
  switch (label) {
    case "playful":
    case "warm":
    case "tender":
    case "curious":
      return warm;
    case "melancholy":
    case "tired":
    case "concerned":
      return cool;
    case "restless":
    case "focused":
      return `saturate(${sat}%)`;
    default:
      return "none";
  }
}

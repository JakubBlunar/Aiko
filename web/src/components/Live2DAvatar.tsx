import { useEffect, useRef } from "react";
import * as PIXI from "pixi.js";
import { Live2DModel, MotionPriority } from "pixi-live2d-display";
import { useAssistantStore } from "../store";
import type { AvatarProfile, BackchannelHint, VoiceMode } from "../types";

// Make pixi-live2d-display drive its own ticker via the standard PIXI ticker.
// (The library expects this to be registered exactly once before any model is
// created -- the call is idempotent.)
Live2DModel.registerTicker(PIXI.Ticker);

interface Live2DAvatarProps {
  manifest: AvatarProfile;
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
    // The bundled avatar (Alexia by default) is served by FastAPI at
    // ``/avatar/``. ``manifest.entry_filename`` is the file name
    // relative to that mount (e.g. ``"Alexia.model3.json"``).
    const url = "/avatar/" + manifest.entry_filename.replace(/^\/+/, "");

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
      fitModelToContainer(
        modelRef.current,
        appRef.current,
        manifest.settings.scale_multiplier ?? 1,
      );
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

  // ── 2. Lip-sync: write ParamMouthOpenY in the ``beforeModelUpdate``
  //    hook so motion / expression / breath can't override us.
  //
  //    Cubism4InternalModel.update() runs in this order each frame
  //    (see pixi-live2d-display source, ``Cubism4InternalModel#update``):
  //
  //        1. emit("beforeMotionUpdate")
  //        2. motionManager.update()        <-- talk/idle motions write
  //                                              ParamMouthOpenY HERE if
  //                                              the .motion3.json has
  //                                              mouth keyframes.
  //        3. emit("afterMotionUpdate")
  //        4. coreModel.saveParameters()    <-- per-frame snapshot
  //        5. expressionManager.update()
  //        6. eyeBlink / focus / breath / physics / pose
  //        7. emit("beforeModelUpdate")     <-- WE HOOK HERE
  //        8. coreModel.update()            <-- renders this frame
  //        9. coreModel.loadParameters()    <-- restores from snapshot
  //
  //    Writing the mouth at "beforeModelUpdate" means our amplitude is
  //    the value rendered in step 8 regardless of what motion or
  //    expression wrote earlier in the pipeline. The previous design
  //    used a standalone rAF that fired BEFORE step 1, so any talk
  //    motion with mouth keyframes silently overwrote the lip-sync at
  //    step 2 -- visible as the mouth freezing during TTS.
  //
  //    The internal model is created asynchronously by ``Live2DModel
  //    .from(...)`` (see the model-load effect above). We poll one rAF
  //    at a time until ``modelRef.current.internalModel`` exists, then
  //    attach the listener. Cleanup detaches it.
  useEffect(() => {
    let pollRaf = 0;
    type InternalModelEmitter = {
      on: (event: string, fn: () => void) => void;
      off: (event: string, fn: () => void) => void;
    };
    let registered:
      | { emitter: InternalModelEmitter; handler: () => void }
      | null = null;

    const handleBeforeModelUpdate = () => {
      const model = modelRef.current;
      if (!model) {
        return;
      }
      const target = useAssistantStore.getState().audioAmplitude || 0;
      // Critically-damped easing toward target — avoids the step-look
      // that raw 30 Hz amplitude updates would produce on a 60 Hz
      // canvas. The smoothing factor matches the previous rAF version.
      const prev = mouthSmoothRef.current;
      const next = prev + (target - prev) * 0.35;
      mouthSmoothRef.current = next < 0 ? 0 : next > 1 ? 1 : next;
      applyMouthOpen(model, manifest, mouthSmoothRef.current);
    };

    const tryRegister = () => {
      const model = modelRef.current as unknown as {
        internalModel?: InternalModelEmitter;
      } | null;
      const emitter = model?.internalModel;
      if (
        emitter &&
        typeof emitter.on === "function" &&
        typeof emitter.off === "function"
      ) {
        emitter.on("beforeModelUpdate", handleBeforeModelUpdate);
        registered = { emitter, handler: handleBeforeModelUpdate };
        return;
      }
      pollRaf = window.requestAnimationFrame(tryRegister);
    };
    pollRaf = window.requestAnimationFrame(tryRegister);

    return () => {
      if (pollRaf) {
        window.cancelAnimationFrame(pollRaf);
        pollRaf = 0;
      }
      if (registered) {
        try {
          registered.emitter.off(
            "beforeModelUpdate",
            registered.handler,
          );
        } catch (err) {
          console.debug("lipsync listener detach failed", err);
        }
        registered = null;
      }
      mouthSmoothRef.current = 0;
    };
  }, [manifest.cubism_version]);

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
            (
              model as unknown as {
                motion: (
                  group: string,
                  index?: number,
                  priority?: number,
                ) => void;
              }
            ).motion(
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
  }, [manifest.reaction_mapping, manifest.talk_motion_group]);

  // ── 4. Backchannel overlay: transient expression while user is speaking ──
  //    A regex classifier on the backend tags STT partials with hints
  //    (agreement, surprise, etc.). When a hint fires, briefly overlay the
  //    matching expression so Aiko looks like she's actively listening.
  useEffect(() => {
    let restoreTimeout: number | null = null;
    let lastBackchannelAt = 0;

    const unsub = useAssistantStore.subscribe((state, prev) => {
      const model = modelRef.current;
      if (!model) {
        return;
      }
      // Only act when a *fresh* hint arrived.
      if (state.backchannelAt === prev.backchannelAt) {
        return;
      }
      if (!state.backchannelHint) {
        return;
      }
      lastBackchannelAt = state.backchannelAt;
      const expressionName = pickBackchannelExpression(
        manifest,
        state.backchannelHint,
      );
      if (!expressionName) {
        return;
      }
      try {
        (
          model as unknown as {
            expression: (name?: string) => void;
          }
        ).expression(expressionName);
      } catch (err) {
        console.debug("backchannel expression failed", err);
      }
      // Restore the persistent reaction expression after a short window
      // (the user finishes speaking soon — we don't want to leave the
      // overlay stuck if the next backchannel doesn't arrive).
      if (restoreTimeout !== null) {
        window.clearTimeout(restoreTimeout);
      }
      restoreTimeout = window.setTimeout(() => {
        const fresh = useAssistantStore.getState();
        if (fresh.backchannelAt !== lastBackchannelAt) {
          // Newer backchannel landed; let that one finish its window.
          return;
        }
        const m = modelRef.current;
        if (!m) {
          return;
        }
        applyReaction(m, manifest, fresh.reaction);
      }, 1800);
    });
    return () => {
      unsub();
      if (restoreTimeout !== null) {
        window.clearTimeout(restoreTimeout);
      }
    };
  }, [manifest.reaction_mapping, manifest.expressions]);

  // ── 5. Idle motion loop -- only fires when not speaking. Cadence is
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

  // ── 5b. Live2D gaze (Phase 3a — Aiko human-like upgrades) ─────────────
  //   Drives ``model.focus(x, y)`` every animation frame so the avatar's
  //   eyes / head track a target point. Priority (highest first):
  //     - **listening (user mid-utterance)** OR **speaking (TTS playing)**:
  //       lock eye contact — centre X, slight upward bias so the user
  //       (typically below the screen) reads as being looked at.
  //     - **thinking (LLM streaming, no TTS yet)**: drift gaze off-axis
  //       with a slow random wander.
  //     - **idle break** (window unfocused OR cursor stopped >2.5 s):
  //       ease target back to centre. She breaks attention gently
  //       without freezing the model — saccades + idle motion + blinks
  //       keep playing.
  //     - **cursor follow (default)**: track the mouse cursor anywhere
  //       in the viewport (NOT just inside the canvas), normalised
  //       against the viewport halves so the screen edge saturates
  //       her gaze naturally.
  //   Tiny micro-saccades (±5 px equivalent in normalised space) are
  //   layered on so the gaze never feels frozen.
  useEffect(() => {
    const container = containerRef.current;
    const app = appRef.current;
    if (!container || !app) {
      return;
    }
    // We drive the model's internal ``focusController`` directly
    // because the public ``model.focus(x, y)`` expects SCREEN-PIXEL
    // coordinates (it runs ``toModelPosition`` + ``atan2`` and only
    // keeps the direction, discarding magnitude). The inner
    // controller takes normalised ``[-1, 1]`` values and has its own
    // velocity-based smoothing — exactly the convention we want.
    // ``(0, 0)`` is straight ahead; positive Y is up.
    const target = { x: 0, y: 0 };
    const microSaccade = { x: 0, y: 0 };
    let lastSaccadeAt = 0;
    const mouseNorm = { x: 0, y: 0 };
    let lastMouseMoveAt = 0;
    // Initialise from ``document.hasFocus()`` so the first frame after
    // mounting in an already-blurred tab is correct (we'd otherwise
    // start chasing the cursor for a moment until ``blur`` fired).
    let windowFocused =
      typeof document !== "undefined" ? document.hasFocus() : true;
    let raf = 0;

    const onPointerMove = (e: PointerEvent) => {
      // The canvas is one small region in a much larger viewport;
      // normalise the cursor offset against half-viewport so the
      // screen edge gives roughly ±1 (then the tick clamps to a
      // comfortable range). Y is flipped because Live2D's focus
      // space uses up = +1 (screen Y grows downward).
      const rect = container.getBoundingClientRect();
      const cx = rect.left + rect.width / 2;
      const cy = rect.top + rect.height / 2;
      const halfW = Math.max(1, window.innerWidth / 2);
      const halfH = Math.max(1, window.innerHeight / 2);
      mouseNorm.x = (e.clientX - cx) / halfW;
      mouseNorm.y = -((e.clientY - cy) / halfH);
      lastMouseMoveAt = performance.now();
    };
    const onWindowFocus = () => {
      windowFocused = true;
    };
    const onWindowBlur = () => {
      windowFocused = false;
    };

    window.addEventListener("pointermove", onPointerMove, { passive: true });
    window.addEventListener("focus", onWindowFocus);
    window.addEventListener("blur", onWindowBlur);

    // Idle-break threshold: after this much time without the cursor
    // moving, treat the user as "not engaging right now" and ease
    // gaze to centre so she stops staring at a stale cursor position.
    const IDLE_BREAK_MS = 1500;

    const tick = () => {
      const model = modelRef.current;
      if (model) {
        const state = useAssistantStore.getState();
        const ts = state.ttsState;
        const vm = state.voiceMode;
        const turnInProgress = state.turnInProgress;
        const isListening = vm === "listening" || vm === "transcribing";
        const isSpeaking = ts === "speaking";
        const isThinking =
          vm === "thinking" || (turnInProgress && ts !== "speaking");
        const now = performance.now();
        const isIdle = !windowFocused || now - lastMouseMoveAt > IDLE_BREAK_MS;

        if (isListening || isSpeaking) {
          // Conversation has the floor — lock eye contact regardless
          // of where the cursor is. Centred X with slight upward bias
          // because the user is typically sitting just below the
          // screen; this reads as "looking at you".
          target.x = 0;
          target.y = 0.2;
        } else if (isThinking) {
          // Slow wander off-axis. Phase it by epoch so multiple
          // mounts don't end up in lockstep.
          const t = now / 1000;
          target.x = 0.35 * Math.sin(t * 0.6);
          target.y = 0.18 * Math.cos(t * 0.43) + 0.05;
        } else if (isIdle) {
          // Window unfocused, OR cursor hasn't moved for a while —
          // break attention. Ease back to centre; saccades + idle
          // motion still play so she stays alive.
          target.x *= 0.92;
          target.y *= 0.92;
        } else {
          // Cursor follow: clamp to a comfortable range. Live2D's
          // eye travel saturates near the bounds anyway; staying
          // in [-0.7, 0.7] keeps the look natural.
          target.x = Math.max(-0.7, Math.min(0.7, mouseNorm.x));
          target.y = Math.max(-0.5, Math.min(0.7, mouseNorm.y));
        }

        // Micro-saccades every 1.5-3 s so the gaze never freezes.
        if (now - lastSaccadeAt > 1500 + Math.random() * 1500) {
          lastSaccadeAt = now;
          microSaccade.x = (Math.random() - 0.5) * 0.1;
          microSaccade.y = (Math.random() - 0.5) * 0.06;
        }
        // Decay the saccade so it's a brief flick, not a sustained offset.
        microSaccade.x *= 0.92;
        microSaccade.y *= 0.92;

        // Push the target straight at the focus controller — it does
        // its own velocity-based smoothing, so layering ours on top
        // would double-smooth and feel sluggish.
        const fx = target.x + microSaccade.x;
        const fy = target.y + microSaccade.y;
        try {
          const fc = (
            model as unknown as {
              internalModel?: {
                focusController?: {
                  focus: (x: number, y: number, instant?: boolean) => void;
                };
              };
            }
          ).internalModel?.focusController;
          fc?.focus(fx, fy);
        } catch {
          // Minimal models without ParamAngle/EyeBall params still
          // accept the call; the focusController just drives nothing.
          // Defensive swallow in case the library shape changes.
        }
      }
      raf = window.requestAnimationFrame(tick);
    };
    raf = window.requestAnimationFrame(tick);

    return () => {
      window.cancelAnimationFrame(raf);
      window.removeEventListener("pointermove", onPointerMove);
      window.removeEventListener("focus", onWindowFocus);
      window.removeEventListener("blur", onWindowBlur);
    };
  }, []);

  // ── 6. Voice-mode driven listening / thinking expressions (Phase 5a) ──
  //   When the user is talking ("listening" / "transcribing") we softly
  //   apply a thoughtful expression so Aiko looks engaged. When the LLM
  //   is composing a reply ("thinking") we apply a 'thoughtful' pose
  //   while the avatar waits. State transitions out of these modes
  //   restore the persistent reaction.
  useEffect(() => {
    const unsub = useAssistantStore.subscribe((state, prev) => {
      const model = modelRef.current;
      if (!model) {
        return;
      }
      if (state.voiceMode === prev.voiceMode) {
        return;
      }
      const next = state.voiceMode;
      if (next === "listening" || next === "transcribing") {
        const expr = pickModeExpression(manifest, "listening");
        applyExpressionByName(model, expr);
        return;
      }
      if (next === "thinking") {
        const expr = pickModeExpression(manifest, "thinking");
        applyExpressionByName(model, expr);
        return;
      }
      // Restore the persistent reaction when leaving listening/thinking.
      applyReaction(model, manifest, state.reaction);
    });
    return () => unsub();
  }, [manifest.reaction_mapping, manifest.expressions]);

  // ── 7. Tier-3 auto-driven effects (Alexia avatar) ───────────────────
  //   A single rAF loop maintains a handful of "envelopes" the
  //   renderer pokes at the model's parameters every frame:
  //     - outfit cross-fade (pajamas at night, day during the day)
  //     - sticky blush  (mood label tender / warm)
  //     - sticky sweat  (mood label concerned / confused / frustrated
  //       OR latest reaction matching same)
  //     - cat-tail sine (any number of ArtMesh202 rotation params,
  //       freq tied to ``mood.arousal``, segment count from manifest)
  //     - body language on ``ParamBodyAngleY`` / ``Z`` (lean-in,
  //       slump, excited bounce, breathing sway, sass tilt)
  //     - transient ``[[overlay:X]]`` pulses fired by the LLM
  //   Every effect is gated on ``manifest.capabilities.has_*`` so a
  //   future model with fewer parameters silently no-ops the relevant
  //   slice.
  useEffect(() => {
    const caps = manifest.capabilities || {};
    const overlays = manifest.overlays || {};
    const outfits = manifest.outfits || {};
    const catTailIds = manifest.cat_tail_param_ids ?? [];
    const catEarIds = manifest.cat_ear_param_ids ?? [];
    const hasAnyEffect =
      caps.has_pajamas ||
      caps.has_pajamas_hooded ||
      caps.has_day_clothes ||
      caps.has_blush ||
      caps.has_sweat ||
      caps.has_cat_tail ||
      caps.has_body_angle_y ||
      caps.has_body_angle_z ||
      caps.has_wink ||
      caps.has_tail_wag ||
      caps.has_ear_wiggle ||
      Object.keys(overlays).length > 0;
    if (!hasAnyEffect) {
      return;
    }

    // Per-effect envelope state (smoothed value in [0, on_value]).
    // Three mutually-exclusive outfit envelopes — exactly one ramps to
    // 1 at a time, the others decay to 0. ``pajamas`` and
    // ``pajamas_hooded`` BOTH contribute to ``Param16`` (the alternate-
    // outfit toggle) on rigs like Alexia where pajamas-with-cap is
    // pajamas + an extra hood param. The render loop sums each
    // binding's contribution per-frame instead of overwriting, so the
    // shared param stays at on_value during a pajamas <-> hooded
    // crossfade and smoothly fades to 0 only when ``day`` wins.
    const outfitEnvelope: Record<string, number> = {
      pajamas: 0,
      pajamas_hooded: 0,
      day: 0,
    };
    const blushEnvelope = { value: 0 };
    const sweatEnvelope = { value: 0 };
    // Body-language envelopes (eased toward 0..1 targets so the
    // posture changes feel like a real lean rather than a snap).
    const leanInEnvelope = { value: 0 };
    const slumpEnvelope = { value: 0 };
    let lastReaction = "";
    let sassTriggeredAt = -Infinity;
    // Transient overlay pulses keyed by capability name with their
    // wall-clock expiry time + the binding to drive.
    const pulses: Record<
      string,
      { until: number; binding: (typeof overlays)[string] }
    > = {};
    let lastOverlayKey: { name: string; expiresAt: number } | null = null;

    // LLM-driven gestures use the same ``[[overlay:X]]`` grammar but
    // dispatch to bespoke per-frame handlers instead of a simple
    // param-on-decay pulse. Each entry stores the wall-clock ``until``
    // time. When the entry expires the handler resets its target
    // params back to 0 (or to whatever the auto layer wants).
    type GestureKind = "wink_left" | "wink_right" | "tail_wag" | "ear_wiggle";
    const gestures: Partial<Record<GestureKind, { until: number }>> = {};
    const GESTURE_NAMES = new Set<GestureKind>([
      "wink_left",
      "wink_right",
      "tail_wag",
      "ear_wiggle",
    ]);

    const sweatMoodLabels = new Set(["concerned", "confused", "frustrated"]);
    const blushMoodLabels = new Set(["tender", "warm"]);
    const sweatReactions = new Set(["concerned", "confused", "frustrated"]);
    const sassReactions = new Set(["amused", "playful"]);
    const slumpMoodLabels = new Set(["tired", "exhausted"]);
    const listeningVoiceModes = new Set(["listening", "transcribing"]);

    const setParam = (paramId: string | undefined, value: number): void => {
      if (!paramId || paramId.startsWith("expr:")) {
        return; // expression-only bindings are handled via model.expression()
      }
      const model = modelRef.current;
      if (!model) {
        return;
      }
      const core = (
        model as unknown as { internalModel: { coreModel: unknown } }
      ).internalModel?.coreModel;
      if (!core) {
        return;
      }
      const cm4 = (
        core as {
          setParameterValueById?: (id: string, v: number) => void;
        }
      ).setParameterValueById;
      if (typeof cm4 === "function") {
        try {
          cm4.call(core, paramId, value);
          return;
        } catch {
          /* fall through */
        }
      }
      const cm2 = (
        core as {
          setParamFloat?: (id: string, v: number) => void;
        }
      ).setParamFloat;
      if (typeof cm2 === "function") {
        try {
          cm2.call(core, paramId, value);
        } catch {
          /* swallow */
        }
      }
    };

    let lastTick = performance.now();
    let raf = 0;

    const tick = () => {
      const now = performance.now();
      const dt = Math.min(0.1, (now - lastTick) / 1000);
      lastTick = now;

      const state = useAssistantStore.getState();
      const moodLabel = (state.mood?.label || "").toLowerCase();
      const moodIntensity = state.mood?.intensity ?? 0;
      const arousal = Math.max(0, Math.min(1, state.mood?.arousal ?? 0.4));
      const reaction = (state.reaction || "").toLowerCase();
      const profile = state.avatar;
      const resolvedOutfit = profile?.resolved_outfit ?? "";
      const overlay = state.avatarOverlay;
      if (overlay && overlay !== lastOverlayKey) {
        // New overlay event from the WS. Gesture-named overlays
        // (wink, tail_wag, ear_wiggle) take a bespoke handler; the
        // rest fall through to the param-on-decay pulse model.
        const name = overlay.name as GestureKind;
        if (GESTURE_NAMES.has(name)) {
          // Gate the gesture on its capability flag — a poorly-prompted
          // model can still emit ``[[overlay:wink_left]]`` on a rig
          // without independent eyes; we silently drop it.
          const capKey =
            name === "tail_wag"
              ? "has_tail_wag"
              : name === "ear_wiggle"
                ? "has_ear_wiggle"
                : "has_wink";
          if (caps[capKey]) {
            gestures[name] = { until: overlay.expiresAt };
          }
        } else {
          const binding = overlays[overlay.name];
          if (binding) {
            pulses[overlay.name] = {
              until: overlay.expiresAt,
              binding,
            };
          }
        }
        lastOverlayKey = overlay;
      }

      // Auto-outfit cross-fade. ~800ms ease so a circadian flip
      // doesn't visibly snap. The outfit selection is mutually
      // exclusive — at most one of ``pajamas`` / ``pajamas_hooded``
      // / ``day`` envelopes ramps to 1, the others decay to 0.
      //
      // Multiple bindings can legitimately reference the same
      // Live2D param (e.g. on Alexia ``pajamas`` and
      // ``pajamas_hooded`` BOTH set Param16=30; only
      // ``pajamas_hooded`` adds Param17=30). Sequential
      // ``setParam(p.param_id, env*on_value)`` writes would have the
      // last binding stomp the first, and the inactive envelope
      // (=0) silently zeros out the active one's contribution.
      // Instead we accumulate contributions per param-id and write
      // the sum once at the end — during a pajamas <-> hooded
      // crossfade Param16 stays at 30 (= 0.5*30 + 0.5*30) while
      // Param17 smoothly fades from 0 to 30.
      const fadePerSec = 1 / 0.8;
      const hasAnyOutfitCap =
        caps.has_pajamas ||
        caps.has_pajamas_hooded ||
        caps.has_day_clothes;
      if (hasAnyOutfitCap) {
        const pajamasTarget = resolvedOutfit === "pajamas" ? 1 : 0;
        const pajamasHoodedTarget =
          resolvedOutfit === "pajamas_hooded" ? 1 : 0;
        const dayTarget = resolvedOutfit === "day" ? 1 : 0;
        outfitEnvelope.pajamas = approach(
          outfitEnvelope.pajamas,
          pajamasTarget,
          dt * fadePerSec,
        );
        outfitEnvelope.pajamas_hooded = approach(
          outfitEnvelope.pajamas_hooded,
          pajamasHoodedTarget,
          dt * fadePerSec,
        );
        outfitEnvelope.day = approach(
          outfitEnvelope.day,
          dayTarget,
          dt * fadePerSec,
        );
        const outfitParamSums: Record<string, number> = {};
        const accumulate = (
          binding: typeof outfits.pajamas | undefined,
          envelope: number,
        ) => {
          if (!binding) {
            return;
          }
          for (const p of binding.params) {
            outfitParamSums[p.param_id] =
              (outfitParamSums[p.param_id] ?? 0) + envelope * p.on_value;
          }
        };
        if (caps.has_pajamas) {
          accumulate(outfits.pajamas, outfitEnvelope.pajamas);
        }
        if (caps.has_pajamas_hooded) {
          accumulate(
            outfits.pajamas_hooded,
            outfitEnvelope.pajamas_hooded,
          );
        }
        if (caps.has_day_clothes) {
          accumulate(outfits.day_clothes, outfitEnvelope.day);
        }
        for (const [paramId, value] of Object.entries(outfitParamSums)) {
          setParam(paramId, value);
        }
      }

      // Auto-blush. Fades in over 600ms when mood is tender/warm with
      // reasonable intensity; fades out otherwise.
      if (caps.has_blush && overlays.blush) {
        const blushTarget =
          blushMoodLabels.has(moodLabel) && moodIntensity > 0.4 ? 1 : 0;
        blushEnvelope.value = approach(
          blushEnvelope.value,
          blushTarget,
          dt * (1 / 0.6),
        );
        setParam(
          overlays.blush.param_id,
          blushEnvelope.value * overlays.blush.on_value,
        );
      }

      // Auto-sweat. Triggered by either the latest reaction OR the
      // current mood label. Decays itself after 1.5s if the trigger
      // disappears so it doesn't stick.
      if (caps.has_sweat && overlays.sweat) {
        const sweatTrigger =
          sweatMoodLabels.has(moodLabel) || sweatReactions.has(reaction);
        const sweatTarget = sweatTrigger ? 1 : 0;
        // 1.5s decay -> ~0.66 per second.
        sweatEnvelope.value = approach(
          sweatEnvelope.value,
          sweatTarget,
          dt * (1 / 1.5),
        );
        setParam(
          overlays.sweat.param_id,
          sweatEnvelope.value * overlays.sweat.on_value,
        );
      }

      // Cat-tail wag. Iterates whatever segments the manifest
      // discovered (Alexia ships 5 numbered ``ArtMesh202`` segments
      // but the loader is rig-agnostic). Pure additive — the
      // physics3.json still drives the base tail motion. The
      // ``tail_wag`` gesture multiplies freq+amp for its lifetime
      // so a one-off ``[[overlay:tail_wag]]`` reads as a happy burst
      // riding on top of the steady arousal baseline.
      if (caps.has_cat_tail && catTailIds.length > 0) {
        const tailGesture = gestures.tail_wag;
        const tailBoost = tailGesture && now < tailGesture.until;
        if (tailGesture && now >= tailGesture.until) {
          delete gestures.tail_wag;
        }
        const freq = (0.3 + 1.1 * arousal) * (tailBoost ? 1.8 : 1);
        const amp = (4 + 12 * arousal) * (tailBoost ? 1.5 : 1);
        const t = now / 1000;
        for (let i = 0; i < catTailIds.length; i += 1) {
          const phase = i * 0.7;
          const value = Math.sin(2 * Math.PI * freq * t + phase) * amp;
          setParam(catTailIds[i], value);
        }
      }

      // Ear-wiggle gesture: 4 Hz sine on every ear segment for the
      // gesture's lifetime, then params snap back to 0 (the rig's
      // physics + idle take over).
      if (caps.has_ear_wiggle && catEarIds.length > 0) {
        const earGesture = gestures.ear_wiggle;
        if (earGesture) {
          if (now < earGesture.until) {
            const t = now / 1000;
            const val = Math.sin(2 * Math.PI * 4 * t) * 15;
            for (const id of catEarIds) {
              setParam(id, val);
            }
          } else {
            for (const id of catEarIds) {
              setParam(id, 0);
            }
            delete gestures.ear_wiggle;
          }
        }
      }

      // Wink gestures: drive the matching eye-open param to 0 for
      // the gesture's lifetime; on expiry release back to 1 so the
      // EyeBlink driver can take over again.
      if (caps.has_wink) {
        for (const side of ["wink_left", "wink_right"] as const) {
          const g = gestures[side];
          if (!g) {
            continue;
          }
          const paramId =
            side === "wink_left" ? "ParamEyeLOpen" : "ParamEyeROpen";
          if (now < g.until) {
            setParam(paramId, 0);
          } else {
            setParam(paramId, 1);
            delete gestures[side];
          }
        }
      }

      // Body language on the unused BodyAngleY/Z channels. Five
      // contributions sum into a per-axis total, then we emit one
      // ``setParam`` per axis. Each effect is gated on its own
      // trigger so a steady mood doesn't pile contributions.
      if (caps.has_body_angle_y || caps.has_body_angle_z) {
        let bodyY = 0;
        let bodyZ = 0;

        // (a) Listening lean-in: torso tips forward while the user
        // is mid-utterance, eased over ~400ms in/out.
        const isListeningNow = listeningVoiceModes.has(state.voiceMode);
        leanInEnvelope.value = approach(
          leanInEnvelope.value,
          isListeningNow ? 1 : 0,
          dt / 0.4,
        );
        bodyY += leanInEnvelope.value * 6;

        // (b) Tired slump: slight backward lean when the mood is
        // tired/exhausted OR it's late at night with low arousal.
        const slumpTrigger =
          slumpMoodLabels.has(moodLabel) ||
          (profile?.circadian_period === "late_night" && arousal < 0.3);
        slumpEnvelope.value = approach(
          slumpEnvelope.value,
          slumpTrigger ? 1 : 0,
          dt / 0.8,
        );
        bodyY += slumpEnvelope.value * -3;

        // (c) Excited bounce: small Y oscillation while arousal
        // is high. Amplitude scales with arousal so a barely-over
        // threshold mood produces a calm bob.
        if (arousal > 0.6) {
          bodyY +=
            Math.sin((now / 1000) * 2 * Math.PI * 1.4) * (1 + arousal * 2);
        }

        // (d) Idle breathing sway: slow continuous Z sine, always
        // on. Adds a "she's alive" baseline regardless of state.
        bodyZ += Math.sin(((now / 1000) * 2 * Math.PI) / 6) * 1.5;

        // (e) Sass tilt: short Z lean burst on the rising edge of
        // an amused/playful reaction; decays to zero over ~0.8s.
        const reactionChanged = reaction !== lastReaction;
        if (reactionChanged && sassReactions.has(reaction)) {
          sassTriggeredAt = now;
        }
        const sassAge = (now - sassTriggeredAt) / 1000;
        if (sassAge < 0.8) {
          bodyZ += 5 * (1 - sassAge / 0.8);
        }
        lastReaction = reaction;

        if (caps.has_body_angle_y) {
          setParam("ParamBodyAngleY", bodyY);
        }
        if (caps.has_body_angle_z) {
          setParam("ParamBodyAngleZ", bodyZ);
        }
      }

      // LLM-driven overlay pulses. Each pulse holds the param at
      // on_value while alive, then snaps back. Independent from the
      // sticky envelopes above (so a manual ``[[overlay:blush]]``
      // pulse doesn't fight the auto-blush envelope: we simply MAX
      // them together when the same capability is bound).
      const expiredKeys: string[] = [];
      for (const [name, pulse] of Object.entries(pulses)) {
        if (now >= pulse.until) {
          // Decay back to zero.
          setParam(pulse.binding.param_id, 0);
          expiredKeys.push(name);
          continue;
        }
        // Auto-blush + manual blush pulse → max so we don't collapse.
        let value = pulse.binding.on_value;
        if (name === "blush") {
          value = Math.max(value, blushEnvelope.value * pulse.binding.on_value);
        } else if (name === "sweat") {
          value = Math.max(value, sweatEnvelope.value * pulse.binding.on_value);
        }
        setParam(pulse.binding.param_id, value);
        // For expression-style bindings, fire ``model.expression()``
        // once when the pulse first lands (the binding param_id
        // starts with ``expr:`` for those).
        if (
          pulse.binding.param_id.startsWith("expr:") &&
          !pulse.binding.param_id.endsWith(":fired")
        ) {
          const exprName = pulse.binding.param_id.slice("expr:".length);
          const model = modelRef.current;
          if (model) {
            try {
              (
                model as unknown as {
                  expression: (n?: string) => void;
                }
              ).expression(exprName);
            } catch {
              /* swallow */
            }
          }
          // Mark fired by mutating a copy so we don't spam expression()
          // on every frame for the duration of the pulse.
          pulse.binding = {
            ...pulse.binding,
            param_id: pulse.binding.param_id + ":fired",
          };
        }
      }
      for (const k of expiredKeys) {
        delete pulses[k];
      }

      raf = window.requestAnimationFrame(tick);
    };
    raf = window.requestAnimationFrame(tick);

    return () => {
      window.cancelAnimationFrame(raf);
      // Reset overlay/outfit/body-language params so a remount
      // doesn't inherit half-applied state. (Fine to call setParam
      // without a model; it just no-ops.)
      if (caps.has_blush && overlays.blush) {
        setParam(overlays.blush.param_id, 0);
      }
      if (caps.has_sweat && overlays.sweat) {
        setParam(overlays.sweat.param_id, 0);
      }
      if (caps.has_body_angle_y) {
        setParam("ParamBodyAngleY", 0);
      }
      if (caps.has_body_angle_z) {
        setParam("ParamBodyAngleZ", 0);
      }
      // Release any in-flight gesture params so a remount doesn't
      // inherit a half-winked eye or hyperactive ears.
      if (caps.has_wink) {
        setParam("ParamEyeLOpen", 1);
        setParam("ParamEyeROpen", 1);
      }
      if (caps.has_ear_wiggle) {
        for (const id of catEarIds) {
          setParam(id, 0);
        }
      }
    };
  }, [
    manifest.capabilities,
    manifest.overlays,
    manifest.outfits,
    manifest.cat_tail_param_ids,
    manifest.cat_ear_param_ids,
  ]);

  // ── Motion playback (LLM ``[[motion:X]]``) ──────────────────────
  //   Each fresh ``avatarMotion`` reference (a new ``firedAt`` ms)
  //   is forwarded to ``model.motion(group, index)``. The library
  //   handles keyframe playback against the rig automatically.
  const avatarMotion = useAssistantStore((s) => s.avatarMotion);
  useEffect(() => {
    if (!avatarMotion) {
      return;
    }
    const model = modelRef.current;
    if (!model) {
      return;
    }
    try {
      (
        model as unknown as {
          motion: (group: string, index?: number) => void;
        }
      ).motion(avatarMotion.group, avatarMotion.index);
    } catch (err) {
      console.debug("LLM motion playback failed", err);
    }
  }, [avatarMotion]);

  // Phase 2b: subtle mood tinting on the avatar container. Subscribed via
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

function applyMouthOpen(
  model: InstanceType<typeof Live2DModel>,
  manifest: AvatarProfile,
  level: number,
): void {
  const core = (
    model as unknown as {
      internalModel: { coreModel: unknown };
    }
  ).internalModel?.coreModel;
  if (!core) {
    return;
  }
  // Prefer parameter IDs declared in the model's ``Groups[LipSync]`` (the
  // canonical source of truth). Fall back to the convention default for
  // each Cubism version when the model didn't declare any. Parameter
  // names vary widely: modern Cubism 4 uses ``ParamMouthOpenY``,
  // Cubism-3-ported-from-Cubism-2 models keep the legacy
  // ``PARAM_MOUTH_OPEN_Y``, custom rigs may use anything else.
  const declared = manifest.lip_sync_ids;
  const params: string[] =
    declared && declared.length > 0
      ? declared
      : [
          manifest.cubism_version === 2
            ? MOUTH_PARAM_CUBISM_2
            : MOUTH_PARAM_CUBISM_4,
        ];
  const cm4 = (
    core as {
      setParameterValueById?: (id: string, value: number) => void;
    }
  ).setParameterValueById;
  const cm2 = (
    core as {
      setParamFloat?: (id: string, value: number) => void;
    }
  ).setParamFloat;
  for (const id of params) {
    if (typeof cm4 === "function") {
      try {
        cm4.call(core, id, level);
        continue;
      } catch {
        /* fall through to cm2 */
      }
    }
    if (typeof cm2 === "function") {
      try {
        cm2.call(core, id, level);
      } catch {
        /* swallow */
      }
    }
  }
}

function applyReaction(
  model: InstanceType<typeof Live2DModel>,
  manifest: AvatarProfile,
  reaction: string,
): void {
  const expressionName = resolveReactionExpression(manifest, reaction);
  if (!expressionName) {
    // Empty mapping = "neutral" / unmapped reaction. Previously this
    // early-returned, which left the PREVIOUS expression stuck on the
    // face — Aiko ended TTS with cheerful eyes and never went back to
    // resting. Switch to the empty default expression on the
    // ExpressionManager so any active overlay is dropped immediately.
    // pixi-live2d-display's ExpressionManager.resetExpression() runs
    // ``_setExpression(this.defaultExpression)``, which is a created-
    // empty motion that sets no params (see node_modules/
    // pixi-live2d-display/dist/cubism4.es.js, ``ExpressionManager``).
    try {
      const exprMgr = (
        model as unknown as {
          internalModel?: {
            motionManager?: {
              expressionManager?: {
                resetExpression?: () => void;
              };
            };
          };
        }
      ).internalModel?.motionManager?.expressionManager;
      exprMgr?.resetExpression?.();
    } catch (err) {
      console.debug("expression reset failed", err);
    }
    return;
  }
  try {
    (
      model as unknown as {
        expression: (name?: string) => void;
      }
    ).expression(expressionName);
  } catch (err) {
    console.debug("expression() failed", expressionName, err);
  }
}

// Server already builds a per-model ``reaction_mapping`` that includes
// authoritative + fuzzy fallbacks (see ``app/core/avatar_profile``).
// This local neighbour table only kicks in for reactions the server
// didn't map at all (e.g. the LLM emitted a brand-new label that
// post-dates the model load). Keeps a minimal subset of the canonical
// chain so unknown reactions still produce *some* visual change.
const _REACTION_NEIGHBOURS: Record<string, string[]> = {
  amused: ["cheerful", "playful", "friendly", "warm", "neutral"],
  playful: ["amused", "cheerful", "excited", "friendly", "warm"],
  enthusiastic: ["excited", "cheerful", "playful", "friendly"],
  curious: ["thoughtful", "surprised", "friendly", "neutral"],
  tender: ["warm", "gentle", "friendly", "calm", "neutral"],
  warm: ["friendly", "gentle", "tender", "cheerful", "neutral"],
  thoughtful: ["serious", "calm", "concerned", "neutral"],
  wistful: ["sad", "melancholy", "thoughtful", "calm", "gentle"],
  concerned: ["serious", "sad", "thoughtful", "neutral"],
  melancholy: ["sad", "wistful", "tired", "calm", "neutral"],
  tired: ["calm", "melancholy", "neutral", "sad"],
  frustrated: ["angry", "concerned", "serious", "neutral"],
  gentle: ["warm", "calm", "friendly", "tender", "neutral"],
  friendly: ["warm", "cheerful", "neutral", "calm"],
  calm: ["neutral", "thoughtful", "gentle", "warm"],
  serious: ["thoughtful", "concerned", "neutral"],
  surprised: ["excited", "curious", "amused", "neutral"],
  cheerful: ["amused", "friendly", "warm", "playful", "neutral"],
  excited: ["enthusiastic", "cheerful", "playful", "surprised", "neutral"],
  sad: ["melancholy", "wistful", "concerned", "neutral"],
  angry: ["frustrated", "serious", "concerned", "neutral"],
  neutral: ["calm", "friendly", "warm"],
};

function resolveReactionExpression(
  manifest: AvatarProfile,
  reaction: string,
): string | undefined {
  if (!reaction) {
    return undefined;
  }
  const direct = manifest.reaction_mapping[reaction];
  if (direct) {
    return direct;
  }
  const neighbours = _REACTION_NEIGHBOURS[reaction] || [];
  for (const fallback of neighbours) {
    const expr = manifest.reaction_mapping[fallback];
    if (expr) {
      return expr;
    }
  }
  return undefined;
}

// Phase 1a: map a backchannel hint to an expression name in the persona.
// Falls back to the persona's nearest matching reaction so even sparse
// reaction_mapping configs produce a visible response.
const _BACKCHANNEL_TO_REACTION: Record<BackchannelHint, string[]> = {
  agreement: ["cheerful", "friendly", "warm"],
  disagreement: ["serious", "concerned", "thoughtful"],
  surprise: ["surprised", "excited", "amazed"],
  amusement: ["cheerful", "amused", "playful"],
  concern: ["concerned", "sad", "gentle"],
  confused: ["confused", "thoughtful", "curious"],
  thinking: ["thoughtful", "calm", "neutral"],
};

function pickBackchannelExpression(
  manifest: AvatarProfile,
  hint: BackchannelHint,
): string | undefined {
  const candidates = _BACKCHANNEL_TO_REACTION[hint] || [];
  for (const reaction of candidates) {
    const expr = manifest.reaction_mapping[reaction];
    if (expr) {
      return expr;
    }
  }
  // Last resort: any expression whose name contains the hint keyword.
  for (const expr of manifest.expressions) {
    if (expr.name.toLowerCase().includes(hint)) {
      return expr.name;
    }
  }
  return undefined;
}

// Phase 5a: persona-mapped expression for Voice/listening/thinking states.
const _MODE_TO_REACTION: Record<"listening" | "thinking", string[]> = {
  listening: ["thoughtful", "calm", "neutral", "friendly", "attentive"],
  thinking: ["thoughtful", "concerned", "calm", "serious", "neutral"],
};

function pickModeExpression(
  manifest: AvatarProfile,
  mode: "listening" | "thinking",
): string | undefined {
  const candidates = _MODE_TO_REACTION[mode];
  for (const reaction of candidates) {
    const expr = manifest.reaction_mapping[reaction];
    if (expr) {
      return expr;
    }
  }
  for (const expr of manifest.expressions) {
    if (expr.name.toLowerCase().includes(mode)) {
      return expr.name;
    }
  }
  return undefined;
}

function applyExpressionByName(
  model: InstanceType<typeof Live2DModel>,
  expressionName: string | undefined,
): void {
  if (!expressionName) {
    return;
  }
  try {
    (
      model as unknown as {
        expression: (name?: string) => void;
      }
    ).expression(expressionName);
  } catch (err) {
    console.debug("expression() failed", expressionName, err);
  }
}

// Phase 5a: a soft glow overlay that signals listening / thinking /
// speaking. CSS-only — no per-frame rAF cost.
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

// Tier-3: critically-damped one-pole approach toward ``target`` at
// the given rate (per second). Used by the auto-effect rAF loop so
// the envelopes ease cleanly without overshoot.
function approach(current: number, target: number, rate: number): number {
  if (rate <= 0) {
    return current;
  }
  const factor = 1 - Math.exp(-rate);
  return current + (target - current) * factor;
}

// Phase 5a: idle motion cadence biased by mood arousal.
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

// Phase 2b: very subtle CSS filter tinting based on mood label + intensity.
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

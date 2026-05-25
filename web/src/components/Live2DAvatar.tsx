import { useEffect, useRef } from "react";
import * as PIXI from "pixi.js";
import { Live2DModel, MotionPriority } from "pixi-live2d-display";
import { useAssistantStore } from "../store";
import type { BackchannelHint, Persona, VoiceMode } from "../types";

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
    // Backend extracts uploads into ``data/personas/active/`` and serves
    // the parent directory at ``/personas``. ``manifest.entry_filename``
    // is zip-relative (e.g. ``"a02/a02.model3.json"``) so we prepend
    // ``active/`` to land on the actual file.
    const url =
      "/personas/active/" + manifest.entry_filename.replace(/^\/+/, "");

    Live2DModel.from(url, { autoInteract: false })
      .then((model) => {
        if (cancelled) {
          model.destroy({ children: true });
          return;
        }
        modelRef.current = model;
        app.stage.addChild(model);
        fitModelToContainer(model, app, manifest.scale_multiplier ?? 1);

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
        manifest.scale_multiplier ?? 1,
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
  }, [manifest.id, manifest.entry_filename, manifest.cubism_version]);

  // ── 1b. React to scale_multiplier changes without rebuilding the model.
  //    The user can drag a slider in Persona settings; refit live so the
  //    canvas stays smooth instead of remounting (which flashes).
  useEffect(() => {
    if (!modelRef.current || !appRef.current) {
      return;
    }
    fitModelToContainer(
      modelRef.current,
      appRef.current,
      manifest.scale_multiplier ?? 1,
    );
  }, [manifest.scale_multiplier]);

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
        manifest, state.backchannelHint,
      );
      if (!expressionName) {
        return;
      }
      try {
        (model as unknown as {
          expression: (name?: string) => void;
        }).expression(expressionName);
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
  }, [manifest.id, manifest.reaction_mapping]);

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
            (model as unknown as {
              motion: (group: string, index?: number, priority?: number) => void;
            }).motion(idleGroup, undefined, MotionPriority.IDLE);
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
  }, [manifest.id, manifest.idle_motion_group]);

  // ── 5b. Live2D gaze (Phase 3a — Aiko human-like upgrades) ─────────────
  //   Drives ``model.focus(x, y)`` every animation frame so the avatar's
  //   eyes / head track a target point. Three modes selected by current
  //   state:
  //     - **idle / speaking**: track the mouse cursor (eased toward
  //       the centre when the cursor leaves the canvas).
  //     - **listening (user mid-utterance)**: lock on a "looking at you"
  //       point biased slightly upward so it reads as eye contact.
  //     - **thinking (LLM streaming, no TTS yet)**: drift gaze off-axis
  //       with a slow random wander.
  //   Tiny micro-saccades (±5 px equivalent in normalised space) are
  //   layered on so the gaze never feels frozen.
  useEffect(() => {
    const container = containerRef.current;
    const app = appRef.current;
    if (!container || !app) {
      return;
    }
    // Gaze coordinates in pixi-live2d-display's ``focus`` space are in
    // [-1, 1] for both axes; (0, 0) is straight ahead.
    const target = { x: 0, y: 0 };
    const smoothed = { x: 0, y: 0 };
    const microSaccade = { x: 0, y: 0 };
    let lastSaccadeAt = 0;
    let mouseInside = false;
    const mouseNorm = { x: 0, y: 0 };
    let raf = 0;

    const setMouseFromEvent = (e: PointerEvent) => {
      const rect = container.getBoundingClientRect();
      if (rect.width <= 0 || rect.height <= 0) {
        return;
      }
      // Normalise to [-1, 1]. Y is *flipped* because the Live2D focus
      // space uses up = +1 (screen Y grows downward).
      mouseNorm.x = ((e.clientX - rect.left) / rect.width) * 2 - 1;
      mouseNorm.y = -(((e.clientY - rect.top) / rect.height) * 2 - 1);
      mouseInside = true;
    };
    const handleLeave = () => {
      mouseInside = false;
    };

    container.addEventListener("pointermove", setMouseFromEvent);
    container.addEventListener("pointerenter", setMouseFromEvent);
    container.addEventListener("pointerleave", handleLeave);

    const tick = () => {
      const model = modelRef.current;
      if (model) {
        const state = useAssistantStore.getState();
        const ts = state.ttsState;
        const vm = state.voiceMode;
        const turnInProgress = state.turnInProgress;
        const isListening = vm === "listening" || vm === "transcribing";
        const isThinking =
          (vm === "thinking" || (turnInProgress && ts !== "speaking"));

        if (isListening) {
          // Lock toward an "attention point": centred X, slight upward
          // bias so the avatar reads as making eye contact with the
          // user (typically sitting just below the screen).
          target.x = 0;
          target.y = 0.20;
        } else if (isThinking) {
          // Slow wander off-axis. Phase it by epoch so multiple
          // mounts don't end up in lockstep.
          const t = performance.now() / 1000;
          target.x = 0.35 * Math.sin(t * 0.6);
          target.y = 0.18 * Math.cos(t * 0.43) + 0.05;
        } else if (mouseInside) {
          // Mouse-follow: clamp to a comfortable range. Live2D's eye
          // travel saturates near the bounds anyway; staying in
          // [-0.7, 0.7] keeps the look natural.
          target.x = Math.max(-0.7, Math.min(0.7, mouseNorm.x));
          target.y = Math.max(-0.5, Math.min(0.7, mouseNorm.y));
        } else {
          // Mouse outside the canvas → ease back toward centre.
          target.x *= 0.92;
          target.y *= 0.92;
        }

        // Micro-saccades every 1.5-3 s so the gaze never freezes.
        const now = performance.now();
        if (now - lastSaccadeAt > 1500 + Math.random() * 1500) {
          lastSaccadeAt = now;
          microSaccade.x = (Math.random() - 0.5) * 0.10;
          microSaccade.y = (Math.random() - 0.5) * 0.06;
        }
        // Decay the saccade so it's a brief flick, not a sustained offset.
        microSaccade.x *= 0.92;
        microSaccade.y *= 0.92;

        // Critically-damped easing toward target + saccade.
        const fx = target.x + microSaccade.x;
        const fy = target.y + microSaccade.y;
        smoothed.x += (fx - smoothed.x) * 0.12;
        smoothed.y += (fy - smoothed.y) * 0.12;
        try {
          (model as unknown as {
            focus: (x: number, y: number) => void;
          }).focus(smoothed.x, smoothed.y);
        } catch {
          // Some minimal models lack ``ParamAngleX/Y/Z`` parameters; the
          // library handles that internally but defensively swallow.
        }
      }
      raf = window.requestAnimationFrame(tick);
    };
    raf = window.requestAnimationFrame(tick);

    return () => {
      window.cancelAnimationFrame(raf);
      container.removeEventListener("pointermove", setMouseFromEvent);
      container.removeEventListener("pointerenter", setMouseFromEvent);
      container.removeEventListener("pointerleave", handleLeave);
    };
  }, [manifest.id]);

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
  }, [manifest.id, manifest.reaction_mapping]);

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
  model.scale.set(fit * multiplier);

  // The PIXI canvas is the viewport — at multiplier 1.0 the auto-fit
  // makes the whole model render inside it, but at higher zoom levels
  // the model is *larger* than the canvas and parts will be clipped.
  // We slide the anchor up the model so the visible window centers
  // on the face/torso region as zoom grows, instead of staying locked
  // to the feet (which would crop the head off-screen). At
  // ``multiplier <= 1`` we keep the classic feet-on-floor framing.
  //   1.0x → anchor.y 1.0  (bottom — see whole character)
  //   1.5x → anchor.y 0.67 (lower-third anchor — torso framed)
  //   2.0x → anchor.y 0.5  (center — head and chest in frame)
  //   3.0x → anchor.y 0.33 (upper-third — face fills frame)
  //   4.0x → anchor.y 0.3  (clamped, prevents anchoring above hairline)
  const zoom = Math.max(1, multiplier);
  const anchorY = Math.max(0.3, 1 / zoom);
  model.x = app.screen.width / 2;
  model.y = app.screen.height * anchorY;
  model.anchor.set(0.5, anchorY);
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
  const cm4 = (core as {
    setParameterValueById?: (id: string, value: number) => void;
  }).setParameterValueById;
  const cm2 = (core as {
    setParamFloat?: (id: string, value: number) => void;
  }).setParamFloat;
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
  manifest: Persona,
  reaction: string,
): void {
  const expressionName = resolveReactionExpression(manifest, reaction);
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

// Phase 3b: semantic-neighbour fallback for reactions the persona
// hasn't mapped explicitly. Mirrors the table in
// ``app/core/persona_manager.py``. Per-persona mappings stay
// authoritative; this only fires when the reaction was emitted but
// has no direct entry.
const _REACTION_NEIGHBOURS: Record<string, string[]> = {
  amused:       ["cheerful", "playful", "friendly", "warm", "neutral"],
  playful:      ["amused", "cheerful", "excited", "friendly", "warm"],
  enthusiastic: ["excited", "cheerful", "playful", "friendly"],
  curious:      ["thoughtful", "surprised", "friendly", "neutral"],
  tender:       ["warm", "gentle", "friendly", "calm", "neutral"],
  warm:         ["friendly", "gentle", "tender", "cheerful", "neutral"],
  thoughtful:   ["serious", "calm", "concerned", "neutral"],
  wistful:      ["sad", "melancholy", "thoughtful", "calm", "gentle"],
  concerned:    ["serious", "sad", "thoughtful", "neutral"],
  melancholy:   ["sad", "wistful", "tired", "calm", "neutral"],
  tired:        ["calm", "melancholy", "neutral", "sad"],
  frustrated:   ["angry", "concerned", "serious", "neutral"],
  gentle:       ["warm", "calm", "friendly", "tender", "neutral"],
  friendly:     ["warm", "cheerful", "neutral", "calm"],
  calm:         ["neutral", "thoughtful", "gentle", "warm"],
  serious:      ["thoughtful", "concerned", "neutral"],
  surprised:    ["excited", "curious", "amused", "neutral"],
  cheerful:     ["amused", "friendly", "warm", "playful", "neutral"],
  excited:      ["enthusiastic", "cheerful", "playful", "surprised", "neutral"],
  sad:          ["melancholy", "wistful", "concerned", "neutral"],
  angry:        ["frustrated", "serious", "concerned", "neutral"],
  neutral:      ["calm", "friendly", "warm"],
};

function resolveReactionExpression(
  manifest: Persona,
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
  manifest: Persona,
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
  manifest: Persona,
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
    (model as unknown as {
      expression: (name?: string) => void;
    }).expression(expressionName);
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

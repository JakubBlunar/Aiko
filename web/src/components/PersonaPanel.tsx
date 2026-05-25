import { useEffect, useRef, useState } from "react";
import { Live2DAvatar } from "./Live2DAvatar";
import { useAssistantStore } from "../store";

/**
 * Persona panel.
 *
 * When a Live2D model has been uploaded via Settings, renders the live
 * canvas with audio-amplitude lip-sync and reaction-driven expressions.
 * Otherwise falls back to the stylised SVG portrait so the app still feels
 * alive on a fresh checkout.
 */
export function PersonaPanel() {
  const ttsState = useAssistantStore((s) => s.ttsState);
  const reaction = useAssistantStore((s) => s.reaction);
  const voiceMode = useAssistantStore((s) => s.voiceMode);
  const persona = useAssistantStore((s) => s.persona);

  const [mouthOpen, setMouthOpen] = useState(0); // 0..1
  const animRef = useRef<number | null>(null);
  const lastSpeakingRef = useRef(false);

  // Drive the mouth when speaking. Uses random easing to fake phoneme cadence.
  useEffect(() => {
    if (ttsState !== "speaking") {
      lastSpeakingRef.current = false;
      setMouthOpen(0);
      if (animRef.current !== null) {
        window.cancelAnimationFrame(animRef.current);
        animRef.current = null;
      }
      return;
    }
    lastSpeakingRef.current = true;

    let target = Math.random();
    let last = performance.now();
    const tick = (now: number) => {
      const dt = Math.min(80, now - last) / 80;
      last = now;
      setMouthOpen((prev) => {
        const next = prev + (target - prev) * 0.35 * dt;
        if (Math.abs(next - target) < 0.05) {
          target = Math.random() * 0.9 + 0.1;
        }
        return next;
      });
      animRef.current = window.requestAnimationFrame(tick);
    };
    animRef.current = window.requestAnimationFrame(tick);

    return () => {
      if (animRef.current !== null) {
        window.cancelAnimationFrame(animRef.current);
        animRef.current = null;
      }
    };
  }, [ttsState]);

  const palette = REACTION_PALETTE[reaction] ?? REACTION_PALETTE.neutral;
  const eyeShape = REACTION_EYES[reaction] ?? REACTION_EYES.neutral;

  return (
    <aside className="hidden h-full w-[340px] shrink-0 flex-col items-center border-l border-white/5 bg-gradient-to-b from-white/[0.04] to-transparent px-4 py-6 lg:flex">
      <div className="text-xs uppercase tracking-[0.2em] text-ink-100/40">
        Aiko
      </div>

      {/*
        For Live2D personas, let the avatar fill the entire vertical
        space between the header label and the name footer so the canvas
        gets ~340 × ~900 px instead of a 320 × 320 square in the middle
        of the rail. ``Live2DAvatar`` itself handles fitting the model
        within whatever box it's given. The decorative SVG fallback was
        designed against a square viewBox so we keep it constrained.
      */}
      <div
        className={
          persona
            ? "relative my-4 flex w-full flex-1 items-center justify-center overflow-hidden"
            : "relative my-auto flex aspect-square w-full max-w-xs items-center justify-center"
        }
      >
        <div
          className="pointer-events-none absolute inset-0 rounded-full opacity-40 blur-3xl transition-colors duration-700"
          style={{ background: palette.glow }}
        />
        {persona ? (
          <Live2DAvatar manifest={persona} />
        ) : (
          <svg
            viewBox="0 0 200 220"
            className="relative h-full w-full drop-shadow-2xl"
            aria-hidden="true"
          >
            <ellipse cx="100" cy="125" rx="78" ry="95" fill={palette.hairBack} />
            <path
              d="M40 220 Q40 180 100 175 Q160 180 160 220 Z"
              fill={palette.body}
              opacity={0.85}
            />
            <ellipse cx="100" cy="115" rx="48" ry="58" fill={palette.skin} />
            <path
              d="M55 95 Q60 50 100 55 Q140 50 145 95 Q130 75 100 80 Q70 75 55 95 Z"
              fill={palette.hairFront}
            />
            <path
              d="M52 100 Q40 140 58 170 Q50 130 60 110 Z"
              fill={palette.hairFront}
            />
            <path
              d="M148 100 Q160 140 142 170 Q150 130 140 110 Z"
              fill={palette.hairFront}
            />
            <g>
              <ellipse cx="80" cy="118" rx="7" ry={eyeShape.height} fill="#1f1235" />
              <ellipse cx="120" cy="118" rx="7" ry={eyeShape.height} fill="#1f1235" />
              <circle cx="82" cy="116" r="2" fill="#fff" opacity={0.9} />
              <circle cx="122" cy="116" r="2" fill="#fff" opacity={0.9} />
            </g>
            <circle cx="73" cy="135" r="6" fill={palette.blush} opacity={0.55} />
            <circle cx="127" cy="135" r="6" fill={palette.blush} opacity={0.55} />
            <ellipse
              cx="100"
              cy={148 + mouthOpen * 2}
              rx={6 + mouthOpen * 4}
              ry={1 + mouthOpen * 7}
              fill="#3b1d44"
            />
          </svg>
        )}
      </div>

      <div className="w-full max-w-xs text-center">
        <div className="text-sm font-medium text-ink-100">
          {persona ? persona.display_name : LABEL_FOR_REACTION[reaction] ?? "Neutral"}
        </div>
        <div className="mt-1 text-[10px] uppercase tracking-[0.2em] text-ink-100/40">
          {ttsState === "speaking"
            ? "speaking"
            : voiceMode !== "off"
              ? voiceMode
              : "idle"}
          {persona ? ` · cubism v${persona.cubism_version}` : ""}
        </div>
        {!persona && (
          <p className="mt-4 text-[11px] text-ink-100/40">
            Open Settings → Persona avatar to upload a Live2D model zip.
          </p>
        )}
      </div>
    </aside>
  );
}

const REACTION_PALETTE: Record<
  string,
  {
    skin: string;
    hairFront: string;
    hairBack: string;
    body: string;
    glow: string;
    blush: string;
  }
> = {
  neutral: {
    skin: "#fde6d3",
    hairFront: "#5b3d8c",
    hairBack: "#3a236a",
    body: "#1f1438",
    glow: "radial-gradient(circle, rgba(139,92,246,0.45), transparent 70%)",
    blush: "#f3a3b8",
  },
  cheerful: {
    skin: "#ffe6d2",
    hairFront: "#c084fc",
    hairBack: "#7c3aed",
    body: "#3a205c",
    glow: "radial-gradient(circle, rgba(244,114,182,0.45), transparent 70%)",
    blush: "#ff9eb8",
  },
  excited: {
    skin: "#ffe1c4",
    hairFront: "#f472b6",
    hairBack: "#be185d",
    body: "#5b1d3d",
    glow: "radial-gradient(circle, rgba(244,114,182,0.65), transparent 70%)",
    blush: "#ff7aa1",
  },
  enthusiastic: {
    skin: "#ffe2c8",
    hairFront: "#fb7185",
    hairBack: "#9f1239",
    body: "#4a1a36",
    glow: "radial-gradient(circle, rgba(251,113,133,0.6), transparent 70%)",
    blush: "#ff80a8",
  },
  friendly: {
    skin: "#ffe6d3",
    hairFront: "#a78bfa",
    hairBack: "#5b21b6",
    body: "#2a1a4a",
    glow: "radial-gradient(circle, rgba(167,139,250,0.55), transparent 70%)",
    blush: "#f3a3b8",
  },
  calm: {
    skin: "#fbe5d6",
    hairFront: "#7dd3fc",
    hairBack: "#0369a1",
    body: "#1e2a4a",
    glow: "radial-gradient(circle, rgba(125,211,252,0.45), transparent 70%)",
    blush: "#f6b1c1",
  },
  serious: {
    skin: "#f7dec5",
    hairFront: "#475569",
    hairBack: "#1e293b",
    body: "#1c2030",
    glow: "radial-gradient(circle, rgba(148,163,184,0.35), transparent 70%)",
    blush: "#e09bb0",
  },
  sad: {
    skin: "#f3dac4",
    hairFront: "#64748b",
    hairBack: "#334155",
    body: "#1f2436",
    glow: "radial-gradient(circle, rgba(96,165,250,0.4), transparent 70%)",
    blush: "#dba0b3",
  },
  gentle: {
    skin: "#ffe6d6",
    hairFront: "#f9a8d4",
    hairBack: "#be185d",
    body: "#2c1838",
    glow: "radial-gradient(circle, rgba(249,168,212,0.5), transparent 70%)",
    blush: "#ff9bb6",
  },
  angry: {
    skin: "#ffd9c1",
    hairFront: "#ef4444",
    hairBack: "#7f1d1d",
    body: "#3b1414",
    glow: "radial-gradient(circle, rgba(239,68,68,0.55), transparent 70%)",
    blush: "#ff7a7a",
  },
  surprised: {
    skin: "#ffe1c8",
    hairFront: "#facc15",
    hairBack: "#a16207",
    body: "#3b2a14",
    glow: "radial-gradient(circle, rgba(250,204,21,0.5), transparent 70%)",
    blush: "#ff9aab",
  },
};

const REACTION_EYES: Record<string, { height: number }> = {
  neutral: { height: 6 },
  cheerful: { height: 4 },
  excited: { height: 7 },
  enthusiastic: { height: 7 },
  friendly: { height: 5 },
  calm: { height: 4 },
  serious: { height: 3 },
  sad: { height: 3 },
  gentle: { height: 4 },
  angry: { height: 3 },
  surprised: { height: 8 },
};

const LABEL_FOR_REACTION: Record<string, string> = {
  neutral: "Neutral",
  cheerful: "Cheerful",
  excited: "Excited",
  enthusiastic: "Enthusiastic",
  friendly: "Friendly",
  calm: "Calm",
  serious: "Focused",
  sad: "A little sad",
  gentle: "Gentle",
  angry: "Frustrated",
  surprised: "Surprised",
};

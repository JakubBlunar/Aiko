import { readBool, writeBool } from "../persist";
import type { SliceCreator } from "../types";

/** Viewport-pixel geometry for the mobile floating persona window. */
export interface MobilePersonaRect {
  x: number;
  y: number;
  w: number;
  h: number;
}

export const MIN_PERSONA_PANEL_W = 320;
export const MAX_PERSONA_PANEL_W = 720;
export const DEFAULT_PERSONA_PANEL_W = 440;

const LS_LEFT_COLLAPSED = "aiko.layout.left_collapsed";
const LS_PERSONA_PANEL_W = "aiko.layout.persona_panel_w";
const LS_PERSONA_ALWAYS_ON_TOP = "aiko.persona.always_on_top";
const LS_MOBILE_PERSONA_VISIBLE = "aiko.mobile.persona_visible";
const LS_MOBILE_PERSONA_RECT = "aiko.mobile.persona_rect";

// Floating mobile persona window: minimum size so a stray drag can't
// shrink it to nothing, and a sensible default spot near the top so it
// sits below the mobile top bar without covering the composer.
export const MIN_MOBILE_PERSONA_W = 120;
export const MIN_MOBILE_PERSONA_H = 150;
export const DEFAULT_MOBILE_PERSONA_RECT: MobilePersonaRect = {
  x: 16,
  y: 72,
  w: 190,
  h: 250,
};

function clampMobilePersonaRect(rect: MobilePersonaRect): MobilePersonaRect {
  const w = Number.isFinite(rect.w)
    ? Math.max(MIN_MOBILE_PERSONA_W, rect.w)
    : DEFAULT_MOBILE_PERSONA_RECT.w;
  const h = Number.isFinite(rect.h)
    ? Math.max(MIN_MOBILE_PERSONA_H, rect.h)
    : DEFAULT_MOBILE_PERSONA_RECT.h;
  const x = Number.isFinite(rect.x) ? rect.x : DEFAULT_MOBILE_PERSONA_RECT.x;
  const y = Number.isFinite(rect.y) ? rect.y : DEFAULT_MOBILE_PERSONA_RECT.y;
  // Note: x/y are clamped to the live viewport at render time (we don't
  // know the window size at module load), so here we only guard against
  // non-finite garbage and undersized boxes.
  return { x, y, w, h };
}

function readMobilePersonaRect(): MobilePersonaRect {
  try {
    const raw = localStorage.getItem(LS_MOBILE_PERSONA_RECT);
    if (raw == null) return { ...DEFAULT_MOBILE_PERSONA_RECT };
    const parsed = JSON.parse(raw) as Partial<MobilePersonaRect>;
    return clampMobilePersonaRect({
      x: Number(parsed.x),
      y: Number(parsed.y),
      w: Number(parsed.w),
      h: Number(parsed.h),
    });
  } catch {
    return { ...DEFAULT_MOBILE_PERSONA_RECT };
  }
}

function writeMobilePersonaRect(rect: MobilePersonaRect): void {
  try {
    localStorage.setItem(LS_MOBILE_PERSONA_RECT, JSON.stringify(rect));
  } catch {
    // No-op; see ``writeBool``.
  }
}

function clampPanelWidth(value: number): number {
  if (!Number.isFinite(value)) return DEFAULT_PERSONA_PANEL_W;
  return Math.max(MIN_PERSONA_PANEL_W, Math.min(MAX_PERSONA_PANEL_W, value));
}

function readPersonaPanelWidth(): number {
  try {
    const raw = localStorage.getItem(LS_PERSONA_PANEL_W);
    if (raw == null) return DEFAULT_PERSONA_PANEL_W;
    const parsed = Number.parseFloat(raw);
    if (!Number.isFinite(parsed)) return DEFAULT_PERSONA_PANEL_W;
    return clampPanelWidth(parsed);
  } catch {
    return DEFAULT_PERSONA_PANEL_W;
  }
}

function writePersonaPanelWidth(value: number): void {
  try {
    localStorage.setItem(LS_PERSONA_PANEL_W, String(Math.round(value)));
  } catch {
    // No-op; see ``writeBool``.
  }
}

export interface LayoutSlice {
  /** Whether the detached persona window is currently visible (driven by
   * the ``persona-visibility`` Tauri event). Always ``false`` in a
   * regular browser. */
  personaWindowVisible: boolean;
  setPersonaWindowVisible: (visible: boolean) => void;

  /** P-layout: client-only chrome state, persisted to localStorage. */
  leftSidebarCollapsed: boolean;
  personaPanelWidth: number;
  /** Whether the detached persona window should stay above other
   * windows (persisted client-side, reapplied via Tauri command). */
  personaAlwaysOnTop: boolean;
  /** Mobile-only floating persona window visibility + geometry. */
  mobilePersonaVisible: boolean;
  mobilePersonaRect: MobilePersonaRect;
  toggleLeftSidebar: () => void;
  setLeftSidebarCollapsed: (collapsed: boolean) => void;
  setPersonaPanelWidth: (px: number) => void;
  setPersonaAlwaysOnTop: (on: boolean) => void;
  toggleMobilePersona: () => void;
  setMobilePersonaVisible: (visible: boolean) => void;
  setMobilePersonaRect: (rect: MobilePersonaRect) => void;
}

export const createLayoutSlice: SliceCreator<LayoutSlice> = (set) => ({
  personaWindowVisible: false,
  setPersonaWindowVisible: (visible) =>
    set({ personaWindowVisible: Boolean(visible) }),

  leftSidebarCollapsed: readBool(LS_LEFT_COLLAPSED, false),
  personaPanelWidth: readPersonaPanelWidth(),
  personaAlwaysOnTop: readBool(LS_PERSONA_ALWAYS_ON_TOP, false),
  toggleLeftSidebar: () =>
    set((state) => {
      const next = !state.leftSidebarCollapsed;
      writeBool(LS_LEFT_COLLAPSED, next);
      return { leftSidebarCollapsed: next };
    }),
  setLeftSidebarCollapsed: (collapsed) => {
    writeBool(LS_LEFT_COLLAPSED, collapsed);
    set({ leftSidebarCollapsed: collapsed });
  },
  setPersonaPanelWidth: (px) => {
    const clamped = clampPanelWidth(px);
    writePersonaPanelWidth(clamped);
    set({ personaPanelWidth: clamped });
  },
  setPersonaAlwaysOnTop: (on) => {
    writeBool(LS_PERSONA_ALWAYS_ON_TOP, on);
    set({ personaAlwaysOnTop: on });
  },

  mobilePersonaVisible: readBool(LS_MOBILE_PERSONA_VISIBLE, false),
  mobilePersonaRect: readMobilePersonaRect(),
  toggleMobilePersona: () =>
    set((state) => {
      const next = !state.mobilePersonaVisible;
      writeBool(LS_MOBILE_PERSONA_VISIBLE, next);
      return { mobilePersonaVisible: next };
    }),
  setMobilePersonaVisible: (visible) => {
    writeBool(LS_MOBILE_PERSONA_VISIBLE, visible);
    set({ mobilePersonaVisible: visible });
  },
  setMobilePersonaRect: (rect) => {
    const clamped = clampMobilePersonaRect(rect);
    writeMobilePersonaRect(clamped);
    set({ mobilePersonaRect: clamped });
  },
});

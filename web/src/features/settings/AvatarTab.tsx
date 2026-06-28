import { useCallback, useEffect, useState } from "react";
import { api } from "../../api";
import { Toggle } from "@/components/Toggle";
import { desktop as desktopCommands } from "../../desktop/commands";
import { useAssistantStore } from "../../store";
import type {
  AccessoryCatalogue,
  AvatarProfile,
  AvatarSettingsKnobs,
  CompanionSettings,
} from "../../types";
import { Section } from "./SettingsSection";

export interface AvatarTabProps {
  avatar: AvatarProfile | null;
  setAvatarSettings: (patch: Partial<AvatarSettingsKnobs>) => void;
  avatarBusy: boolean;
  avatarError: string | null;
  onPatchAvatarSettings: (patch: Partial<AvatarSettingsKnobs>) => Promise<void>;
  personaAlwaysOnTop: boolean;
  personaError: string | null;
  onPatchPersonaWindow: (alwaysOnTop: boolean) => Promise<void>;
  onResetPersonaWindow: () => Promise<void>;
  tauri: boolean;
  /** K31/K32 soft-physicality switches. ``null`` until settings load. */
  companion: CompanionSettings | null;
  onPatchCompanion: (patch: Partial<CompanionSettings>) => void;
}

function prettyAccessoryLabel(key: string): string {
  switch (key) {
    case "lollipop":
      return "Lollipop";
    case "eyeglasses":
      return "Eyeglasses (face)";
    case "head_sunglasses":
      return "Sunglasses (on head)";
    case "crossed_arms":
      return "Crossed-arms pose";
    case "eye_color":
      return "Eye color";
    default:
      return key.replace(/_/g, " ");
  }
}

function prettyEnumLabel(value: string): string {
  switch (value) {
    case "default":
      return "Default";
    case "both_purple":
      return "Both purple";
    case "left_purple":
      return "Left purple";
    case "right_purple":
      return "Right purple";
    default:
      return value.replace(/_/g, " ");
  }
}

function gateHint(allowedOutfits: string[]): string {
  if (allowedOutfits.length === 0) return "";
  const pretty = allowedOutfits
    .map((o) => (o === "day_clothes" ? "day clothes" : o.replace(/_/g, " ")))
    .join(" / ");
  return `only with ${pretty}`;
}

/**
 * Phase 4 (expression overhaul): persistent accessory toggles.
 *
 * Fetches ``GET /api/avatar/accessories`` on mount and re-fetches
 * whenever the WS pushes an ``avatar_settings_changed`` event (so a
 * PATCH from another window propagates here). Each catalogue entry
 * becomes either a toggle (lollipop / eyeglasses / head_sunglasses /
 * crossed_arms) or a radio group (``eye_color``).
 *
 * Outfit gating: rows whose ``allowed_outfits`` doesn't include the
 * current ``active_outfit`` render as disabled with a hint string,
 * so the user sees *why* crossed-arms is greyed out in pajamas
 * instead of just toggling it on and seeing nothing happen.
 *
 * The component is intentionally lightweight — no error toast, no
 * busy spinner. A failed PATCH refreshes the catalogue so the UI
 * snaps back to the server's authoritative state.
 */
function AccessoriesSubSection({ avatarLoaded }: { avatarLoaded: boolean }) {
  const [catalogue, setCatalogue] = useState<AccessoryCatalogue | null>(null);
  const [busy, setBusy] = useState(false);
  const refresh = useCallback(async () => {
    if (!avatarLoaded) {
      setCatalogue(null);
      return;
    }
    try {
      const next = await api.getAvatarAccessories();
      setCatalogue(next);
    } catch {
      setCatalogue(null);
    }
  }, [avatarLoaded]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  // WS bridge: re-fetch on any avatar_settings_changed broadcast so
  // a PATCH from another tab / window stays in sync.
  const lastAvatarSettings = useAssistantStore((s) => s.avatar?.settings);
  useEffect(() => {
    void refresh();
  }, [lastAvatarSettings, refresh]);

  const onPatch = async (patch: Record<string, string | boolean>) => {
    setBusy(true);
    try {
      const next = await api.patchAvatarAccessories(patch);
      setCatalogue(next);
    } catch {
      void refresh();
    } finally {
      setBusy(false);
    }
  };

  if (!avatarLoaded || !catalogue) {
    return null;
  }
  const entries = catalogue.accessories.filter((e) => e.available);
  if (entries.length === 0) {
    return null;
  }
  return (
    <div className="space-y-1.5 rounded-md border border-white/5 bg-white/[0.02] px-3 py-2">
      <p className="text-[11px] uppercase tracking-wide text-ink-100/50">
        Accessories
      </p>
      <div className="space-y-1.5">
        {entries.map((entry) => {
          const gated =
            entry.allowed_outfits.length > 0 &&
            !!catalogue.active_outfit &&
            !entry.allowed_outfits.includes(
              catalogue.active_outfit === "day" ? "day_clothes" : catalogue.active_outfit,
            );
          const disabled = busy || gated;
          if (entry.kind === "toggle") {
            return (
              <label
                key={entry.key}
                className={`flex items-center gap-2 text-xs ${
                  disabled ? "text-ink-100/30" : "text-ink-100/80"
                }`}
              >
                <input
                  type="checkbox"
                  checked={entry.value === true}
                  disabled={disabled}
                  onChange={(ev) =>
                    void onPatch({ [entry.key]: ev.currentTarget.checked })
                  }
                  className="accent-ink-400"
                />
                <span>{prettyAccessoryLabel(entry.key)}</span>
                {gated ? (
                  <span className="text-[11px] text-ink-100/40">
                    · {gateHint(entry.allowed_outfits)}
                  </span>
                ) : null}
              </label>
            );
          }
          // ``eye_color`` enum — render as a labelled radio group.
          return (
            <div key={entry.key} className="space-y-1">
              <p className="text-xs text-ink-100/80">
                {prettyAccessoryLabel(entry.key)}
              </p>
              <div className="flex flex-wrap gap-x-3 gap-y-1">
                {(entry.options ?? []).map((opt) => (
                  <label
                    key={opt}
                    className={`flex items-center gap-1.5 text-[11px] ${
                      disabled ? "text-ink-100/30" : "text-ink-100/70"
                    }`}
                  >
                    <input
                      type="radio"
                      name={`accessory-${entry.key}`}
                      value={opt}
                      checked={entry.value === opt}
                      disabled={disabled}
                      onChange={() =>
                        void onPatch({ [entry.key]: opt })
                      }
                      className="accent-ink-400"
                    />
                    <span>{prettyEnumLabel(opt)}</span>
                  </label>
                ))}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

export function AvatarTab({
  avatar,
  setAvatarSettings,
  avatarBusy,
  avatarError,
  onPatchAvatarSettings,
  personaAlwaysOnTop,
  personaError,
  onPatchPersonaWindow,
  onResetPersonaWindow,
  tauri,
  companion,
  onPatchCompanion,
}: AvatarTabProps) {
  return (
    <>
      <Section title="Avatar (Live2D)">
        {avatarError ? (
          <div className="rounded-md border border-rose-400/40 bg-rose-500/10 px-3 py-2 text-xs text-rose-200">
            {avatarError}
          </div>
        ) : null}
        <div className="flex items-center justify-between rounded-md bg-white/[0.02] px-3 py-2 text-[11px]">
          <span className="text-ink-100/60">Loaded</span>
          <span className="font-mono text-ink-100/80">
            {avatar?.loaded
              ? `${avatar.display_name} (Cubism v${avatar.cubism_version})`
              : "Files missing on disk"}
          </span>
        </div>
        <div className="mt-2 space-y-1.5">
          <p className="text-[11px] uppercase tracking-wide text-ink-100/50">
            Avatar size
          </p>
          <div className="flex items-center gap-3 rounded-md bg-white/[0.02] px-2 py-2">
            <input
              type="range"
              min={0.5}
              max={4}
              step={0.05}
              value={avatar?.settings.scale_multiplier ?? 1}
              onChange={(e) => {
                const v = Number(e.target.value);
                setAvatarSettings({ scale_multiplier: v });
              }}
              onPointerUp={(e) =>
                void onPatchAvatarSettings({
                  scale_multiplier: Number(
                    (e.target as HTMLInputElement).value,
                  ),
                })
              }
              onKeyUp={(e) => {
                if (
                  e.key === "ArrowLeft" ||
                  e.key === "ArrowRight" ||
                  e.key === "Home" ||
                  e.key === "End"
                ) {
                  void onPatchAvatarSettings({
                    scale_multiplier: Number(
                      (e.target as HTMLInputElement).value,
                    ),
                  });
                }
              }}
              disabled={avatarBusy || !avatar}
              className="flex-1 accent-ink-400"
              aria-label="Avatar scale multiplier"
            />
            <span className="w-10 text-right text-[11px] tabular-nums text-ink-100/70">
              {(avatar?.settings.scale_multiplier ?? 1).toFixed(2)}x
            </span>
            <button
              type="button"
              onClick={() =>
                void onPatchAvatarSettings({ scale_multiplier: 1 })
              }
              disabled={avatarBusy || !avatar}
              className="rounded border border-white/10 px-2 py-0.5 text-[10px] text-ink-100/60 hover:border-ink-400 hover:text-ink-100"
            >
              Reset
            </button>
          </div>
        </div>
        <div className="mt-2 space-y-1.5">
          <p className="text-[11px] uppercase tracking-wide text-ink-100/50">
            Body language intensity
          </p>
          <div className="flex items-center gap-3 rounded-md bg-white/[0.02] px-2 py-2">
            <input
              type="range"
              min={0}
              max={1.5}
              step={0.05}
              value={avatar?.settings.expressiveness ?? 1}
              onChange={(e) => {
                const v = Number(e.target.value);
                setAvatarSettings({ expressiveness: v });
              }}
              onPointerUp={(e) =>
                void onPatchAvatarSettings({
                  expressiveness: Number(
                    (e.target as HTMLInputElement).value,
                  ),
                })
              }
              onKeyUp={(e) => {
                if (
                  e.key === "ArrowLeft" ||
                  e.key === "ArrowRight" ||
                  e.key === "Home" ||
                  e.key === "End"
                ) {
                  void onPatchAvatarSettings({
                    expressiveness: Number(
                      (e.target as HTMLInputElement).value,
                    ),
                  });
                }
              }}
              disabled={avatarBusy || !avatar}
              className="flex-1 accent-ink-400"
              aria-label="Avatar body language intensity"
            />
            <span className="w-10 text-right text-[11px] tabular-nums text-ink-100/70">
              {(avatar?.settings.expressiveness ?? 1).toFixed(2)}x
            </span>
            <button
              type="button"
              onClick={() =>
                void onPatchAvatarSettings({ expressiveness: 1 })
              }
              disabled={avatarBusy || !avatar}
              className="rounded border border-white/10 px-2 py-0.5 text-[10px] text-ink-100/60 hover:border-ink-400 hover:text-ink-100"
            >
              Reset
            </button>
          </div>
          <p className="text-[10px] text-ink-100/40">
            0 mutes mood-driven body language; 1 is the default; up to 1.5 amplifies.
          </p>
        </div>
        <div className="mt-2 space-y-1.5">
          <p className="text-[11px] uppercase tracking-wide text-ink-100/50">
            Outfit
          </p>
          <div className="flex flex-col gap-1 rounded-md bg-white/[0.02] px-3 py-2 text-[11px]">
            {(
              [
                "auto",
                "day",
                "pajamas",
                "pajamas_hooded",
              ] as const
            ).map((mode) => {
              const supported =
                mode === "auto" ||
                mode === "day" ||
                (mode === "pajamas" &&
                  (avatar?.capabilities.has_pajamas ?? false)) ||
                (mode === "pajamas_hooded" &&
                  (avatar?.capabilities.has_pajamas_hooded ?? false));
              // Friendlier labels for snake_case modes.
              const label =
                mode === "pajamas_hooded"
                  ? "Pajamas (hooded)"
                  : mode.charAt(0).toUpperCase() + mode.slice(1);
              return (
                <label
                  key={mode}
                  className={`flex items-center gap-2 ${
                    supported ? "text-ink-100/80" : "text-ink-100/30"
                  }`}
                >
                  <input
                    type="radio"
                    name="auto_outfit"
                    value={mode}
                    checked={avatar?.settings.auto_outfit === mode}
                    onChange={() =>
                      void onPatchAvatarSettings({ auto_outfit: mode })
                    }
                    disabled={avatarBusy || !avatar || !supported}
                    className="accent-ink-400"
                  />
                  <span>{label}</span>
                  {mode === "auto" ? (
                    <span className="text-ink-100/40">
                      · circadian-driven
                    </span>
                  ) : null}
                  {(mode === "pajamas" || mode === "pajamas_hooded") &&
                  !supported ? (
                    <span className="text-ink-100/40">
                      · not supported by current avatar
                    </span>
                  ) : null}
                </label>
              );
            })}
          </div>
        </div>
        {avatar?.loaded ? (
          <p className="rounded-md border border-white/5 bg-white/[0.02] px-3 py-2 text-[11px] text-ink-100/50">
            Capabilities:{" "}
            {Object.entries(avatar.capabilities)
              .filter(([, v]) => v)
              .map(([k]) => k.replace(/^has_/, ""))
              .join(", ") || "(none detected)"}
          </p>
        ) : (
          <p className="rounded-md border border-white/5 bg-white/[0.02] px-3 py-2 text-[11px] text-ink-100/50">
            Place the Alexia model files at{" "}
            <code>live-2d-models/Alexia/</code>. The bundle is
            gitignored so each developer drops their own copy in.
          </p>
        )}
        <AccessoriesSubSection avatarLoaded={!!avatar?.loaded} />
      </Section>

      <Section title="Persona window (desktop)">
        {personaError ? (
          <div className="rounded-md border border-rose-400/40 bg-rose-500/10 px-3 py-2 text-xs text-rose-200">
            {personaError}
          </div>
        ) : null}
        <p className="text-[11px] text-ink-100/50">
          Floating, frameless window that shows just the avatar
          plus a mic toggle and one-line composer. Position and
          size are remembered automatically by the desktop
          shell -- drag and resize the window itself instead of
          using sliders here. The browser build ignores this
          section entirely (no floating window exists outside
          Tauri).
        </p>
        <label
          className={`flex items-center gap-2 text-[12px] ${
            tauri ? "text-ink-100/80" : "text-ink-100/40"
          }`}
          title={
            tauri
              ? "Keep the persona window above other apps"
              : "Only available in the Tauri desktop shell"
          }
        >
          <input
            type="checkbox"
            checked={personaAlwaysOnTop}
            onChange={(event) =>
              void onPatchPersonaWindow(event.target.checked)
            }
            disabled={!tauri}
            className="accent-ink-400 disabled:cursor-not-allowed"
          />
          Always on top
        </label>
        <div className="flex flex-wrap items-center gap-2">
          <button
            type="button"
            onClick={() => void desktopCommands.openPersona()}
            disabled={!tauri}
            title={
              tauri
                ? "Open the floating persona window"
                : "Persona window is only available in the Tauri desktop shell"
            }
            className="rounded-md border border-white/10 px-3 py-1.5 text-xs text-ink-100/80 hover:border-pink-400 hover:text-pink-100 disabled:cursor-not-allowed disabled:opacity-40"
          >
            Open persona window
          </button>
          <button
            type="button"
            onClick={() => void onResetPersonaWindow()}
            disabled={!tauri}
            title={
              tauri
                ? "Snap the persona window back to the default size, centered on this monitor"
                : "Only available in the Tauri desktop shell"
            }
            className="rounded-md border border-white/10 px-3 py-1.5 text-xs text-ink-100/80 hover:border-amber-400 hover:text-amber-100 disabled:cursor-not-allowed disabled:opacity-40"
          >
            Reset window position
          </button>
        </div>
      </Section>

      {companion ? (
        <Section title="Touch & reactions">
          <p className="text-[11px] text-ink-100/50">
            Soft-physicality round-trip: Aiko can send little gestures
            (a wave, a hug) and you can react to her messages with
            emoji. The persona overlay shows gestures as a banner.
          </p>
          <Toggle
            checked={companion.touch_enabled}
            inputClassName="accent-ink-400"
            onChange={(checked) =>
              onPatchCompanion({ touch_enabled: checked })
            }
          >
            Aiko can send touch gestures
          </Toggle>
          <Toggle
            checked={companion.user_reactions_enabled}
            inputClassName="accent-ink-400"
            onChange={(checked) =>
              onPatchCompanion({ user_reactions_enabled: checked })
            }
          >
            Emoji reactions on Aiko's messages
          </Toggle>
          <Toggle
            checked={companion.persona_touch_banner_enabled}
            inputClassName="accent-ink-400"
            onChange={(checked) =>
              onPatchCompanion({ persona_touch_banner_enabled: checked })
            }
          >
            Show gesture banner in the persona window
          </Toggle>
          <label className="ml-6 flex items-center justify-between gap-2 rounded-md bg-white/[0.02] px-3 py-1.5 text-[11px] text-ink-100/60">
            <span>Banner duration (s)</span>
            <input
              type="number"
              min={1}
              max={120}
              value={companion.persona_touch_banner_duration_seconds}
              disabled={!companion.persona_touch_banner_enabled}
              onChange={(e) =>
                onPatchCompanion({
                  persona_touch_banner_duration_seconds: Math.max(
                    1,
                    Math.min(120, Number(e.target.value) || 20),
                  ),
                })
              }
              className="w-16 rounded border border-white/10 bg-black/30 px-2 py-1 text-right text-ink-100/80 disabled:opacity-40"
            />
          </label>
          {/* K60 tsundere expression-mask dial. A strong flavour
           * choice, so it ships off by default; the mask only
           * changes how warm feelings are *expressed* (denial with
           * a visible tell), never what Aiko actually feels. */}
          <label className="mt-3 flex items-center justify-between gap-2 text-xs text-ink-100/70">
            <span>
              Tsundere mask
              <span className="ml-1 text-[10px] text-ink-100/40">
                warmth expressed through denial
              </span>
            </span>
            <select
              value={companion.expression_mask ?? "off"}
              onChange={(e) =>
                onPatchCompanion({
                  expression_mask: e.target
                    .value as CompanionSettings["expression_mask"],
                })
              }
              className="rounded border border-white/10 bg-black/30 px-2 py-1 text-xs text-ink-100/80"
            >
              <option value="off">Off</option>
              <option value="tsundere_light">Light</option>
              <option value="tsundere_full">Full</option>
            </select>
          </label>
        </Section>
      ) : null}
    </>
  );
}

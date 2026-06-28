import { useEffect, useRef, useState } from "react";
import { AvatarPanel } from "./components/AvatarPanel";
import { ChatView } from "./components/ChatView";
import { FirstRunOnboarding } from "./components/FirstRunOnboarding";
import { FloatingPersona } from "./components/FloatingPersona";
import { MobileNavDrawer } from "./components/MobileNavDrawer";
import { MobileTopBar } from "./components/MobileTopBar";
import { PanelResizeHandle } from "./components/PanelResizeHandle";
import { PersonaWindow } from "./components/PersonaWindow";
import { SessionSidebar } from "./components/SessionSidebar";
import { SettingsDrawer } from "./components/SettingsDrawer";
import { Toasts } from "./components/Toasts";
import { NotificationDrawer } from "./components/NotificationDrawer";
import { useIsMobile } from "./hooks/useIsMobile";
import { desktop } from "./desktop/commands";
import { listenPersonaVisibility } from "./desktop/events";
import { isTauri } from "./desktop/runtime";
import { api } from "./api";
import { useActivityReporter } from "./hooks/useActivityReporter";
import { useAssistantSocket } from "./hooks/useAssistantSocket";
import { usePresenceReporter } from "./hooks/usePresenceReporter";
import { debugLog } from "./log";
import { useAssistantStore } from "./store";

/** Tiny hash-router. ``location.hash === "#/persona"`` -> the persona
 * HUD. Anything else -> the full chat layout. We avoid pulling in a
 * routing library because the surface is a single switch that never
 * grows beyond this. */
function useRoute(): "main" | "persona" {
  const [route, setRoute] = useState<"main" | "persona">(() =>
    typeof window !== "undefined" && window.location.hash.startsWith("#/persona")
      ? "persona"
      : "main",
  );
  useEffect(() => {
    const onHashChange = () => {
      setRoute(window.location.hash.startsWith("#/persona") ? "persona" : "main");
    };
    window.addEventListener("hashchange", onHashChange);
    return () => window.removeEventListener("hashchange", onHashChange);
  }, []);
  return route;
}

/** Main-window-only side effect: keep the ``personaWindowVisible``
 * store slice in lockstep with the OS-level visibility of the persona
 * window. Seeds the initial value via ``invoke("is_persona_visible")``
 * and then subscribes to the ``persona-visibility`` Tauri event so any
 * trigger (top-bar button, tray menu, X button) flips the flag. The
 * event subscription is a no-op outside of Tauri so the browser layout
 * is untouched.
 *
 * Also re-applies the user's ``personaAlwaysOnTop`` preference every
 * time the persona window becomes visible. We can't trust the OS to
 * remember the flag across window recreations (Tauri tears the window
 * down on close in some configurations), and the
 * ``tauri-plugin-window-state`` plugin only persists position + size,
 * not always-on-top -- so we own this bit explicitly via
 * ``localStorage`` round-trip. */
function usePersonaVisibilitySync() {
  const setPersonaWindowVisible = useAssistantStore(
    (s) => s.setPersonaWindowVisible,
  );
  const personaAlwaysOnTop = useAssistantStore((s) => s.personaAlwaysOnTop);
  // Pull through a ref so the listener callback below always sees the
  // current preference without re-subscribing whenever the user
  // toggles the checkbox in settings.
  const personaAlwaysOnTopRef = useRef(personaAlwaysOnTop);
  useEffect(() => {
    personaAlwaysOnTopRef.current = personaAlwaysOnTop;
  }, [personaAlwaysOnTop]);

  useEffect(() => {
    let cancelled = false;
    let unlisten: (() => void) | null = null;

    const applyAlwaysOnTopIfVisible = (visible: boolean) => {
      if (!visible) return;
      void desktop.setPersonaAlwaysOnTop(personaAlwaysOnTopRef.current);
    };

    void desktop.isPersonaVisible().then((value) => {
      if (cancelled) return;
      const visible = Boolean(value);
      setPersonaWindowVisible(visible);
      applyAlwaysOnTopIfVisible(visible);
    });

    void listenPersonaVisibility((visible) => {
      if (cancelled) return;
      setPersonaWindowVisible(visible);
      applyAlwaysOnTopIfVisible(visible);
    }).then((teardown) => {
      if (cancelled) {
        teardown();
      } else {
        unlisten = teardown;
      }
    });

    return () => {
      cancelled = true;
      if (unlisten) {
        unlisten();
      }
    };
  }, [setPersonaWindowVisible]);
}

export default function App() {
  const { send, sendBytes } = useAssistantSocket();
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [mobileNavOpen, setMobileNavOpen] = useState(false);
  const route = useRoute();
  const tauri = isTauri();
  const isMobile = useIsMobile();
  const mobilePersonaVisible = useAssistantStore(
    (s) => s.mobilePersonaVisible,
  );
  const toggleMobilePersona = useAssistantStore((s) => s.toggleMobilePersona);
  const personaVisible = useAssistantStore((s) => s.personaWindowVisible);
  const personaPanelWidth = useAssistantStore((s) => s.personaPanelWidth);
  const setPersonaPanelWidth = useAssistantStore(
    (s) => s.setPersonaPanelWidth,
  );
  const leftSidebarCollapsed = useAssistantStore(
    (s) => s.leftSidebarCollapsed,
  );
  const toggleLeftSidebar = useAssistantStore((s) => s.toggleLeftSidebar);

  // Subscribe only on the main window. The persona window doesn't need
  // to know its own visibility (it can ask the OS / DOM directly) and
  // shouldn't double-subscribe to the event bus.
  usePersonaVisibilitySync();

  // Forward browser tab visibility + Tauri window focus to the backend
  // as a single boolean so the typed-mode proactive timer can pause
  // while the user is heads-down in another app. Wired in the main
  // window only — the persona-window route renders its own component
  // tree and doesn't need to double-report.
  usePresenceReporter({ send });

  // Activity awareness (desktop opt-in, default off). Polls the Tauri
  // shell for the foreground app name when the toggle is on; complete
  // no-op on the browser AND when the toggle is off.
  const activityEnabled = useAssistantStore(
    (s) => s.activityAwarenessEnabled,
  );
  const setActivityAwarenessEnabled = useAssistantStore(
    (s) => s.setActivityAwarenessEnabled,
  );
  const setLoggingSettings = useAssistantStore((s) => s.setLoggingSettings);
  const setWorld = useAssistantStore((s) => s.setWorld);
  // Seed the toggle from /api/settings on mount so the activity
  // reporter picks up a previously-saved opt-in without waiting for
  // the user to open the settings drawer. Failure is non-fatal:
  // default ``false`` already gives the privacy-respecting behaviour.
  // We also hydrate ``loggingSettings`` from the same payload so
  // ``debugLog`` honours the persisted "Debug logging" toggle from
  // boot — without this the batcher would start in the disabled state
  // and only flip on once the user opens the drawer.
  useEffect(() => {
    let cancelled = false;
    void api
      .getSettings()
      .then((settings) => {
        if (cancelled) return;
        const flag = Boolean(settings.activity?.awareness_enabled);
        setActivityAwarenessEnabled(flag);
        if (settings.logging) {
          setLoggingSettings({
            ui_log_enabled: Boolean(settings.logging.ui_log_enabled),
            ui_log_categories: Array.isArray(settings.logging.ui_log_categories)
              ? settings.logging.ui_log_categories.map((token) => String(token))
              : [],
            ui_log_max_batch: Number(settings.logging.ui_log_max_batch) || 50,
            ui_log_max_payload_bytes:
              Number(settings.logging.ui_log_max_payload_bytes) || 2048,
          });
          debugLog.setEnabled(Boolean(settings.logging.ui_log_enabled));
        }
      })
      .catch(() => {
        /* offline or stale backend — leave toggle at default */
      });
    return () => {
      cancelled = true;
    };
  }, [setActivityAwarenessEnabled, setLoggingSettings]);

  // Seed Aiko's room snapshot on mount so the avatar-panel caption can
  // show what she's doing ("at the garden, standing, stretching")
  // without waiting for the user to open the World settings tab. After
  // this initial GET the ``world_updated`` WS event keeps it live.
  // Failure is non-fatal: the caption falls back to the plain "idle"
  // line when the world is disabled or the backend is unreachable.
  useEffect(() => {
    let cancelled = false;
    void api
      .getWorld()
      .then((snapshot) => {
        if (!cancelled) setWorld(snapshot);
      })
      .catch(() => {
        /* world disabled or offline — caption stays at the idle fallback */
      });
    return () => {
      cancelled = true;
    };
  }, [setWorld]);
  useActivityReporter({ send, enabled: activityEnabled });

  if (route === "persona") {
    return <PersonaWindow send={send} sendBytes={sendBytes} />;
  }

  const togglePersona = () => {
    if (personaVisible) {
      void desktop.closePersona();
    } else {
      void desktop.openPersona();
    }
  };

  // ── Phone layout ──────────────────────────────────────────────────
  // Top bar + slide-in nav drawer + fullscreen settings + an in-page
  // floating persona window. The desktop tree below is left completely
  // untouched at >= 768px (see ``useIsMobile``).
  if (isMobile) {
    return (
      <div className="relative flex h-full w-full flex-col overflow-hidden">
        <MobileTopBar
          onOpenNav={() => setMobileNavOpen(true)}
          onTogglePersona={toggleMobilePersona}
          personaVisible={mobilePersonaVisible}
        />
        <main className="flex min-h-0 min-w-0 flex-1 overflow-hidden">
          <ChatView send={send} sendBytes={sendBytes} />
        </main>
        {mobilePersonaVisible ? <FloatingPersona /> : null}
        <MobileNavDrawer
          open={mobileNavOpen}
          onClose={() => setMobileNavOpen(false)}
          send={send}
          onOpenSettings={() => setSettingsOpen(true)}
        />
        <SettingsDrawer
          open={settingsOpen}
          onClose={() => setSettingsOpen(false)}
        />
        {/* Phones: no corner popups (they cover the composer). Toasts
            are archived only and read from the top-bar bell drawer. */}
        <NotificationDrawer />
        <FirstRunOnboarding />
      </div>
    );
  }

  return (
    <div className="flex h-full w-full overflow-hidden">
      <SessionSidebar
        send={send}
        onOpenSettings={() => setSettingsOpen(true)}
        onTogglePersona={tauri ? togglePersona : undefined}
        personaWindowVisible={personaVisible}
        collapsed={leftSidebarCollapsed}
        onToggleCollapsed={toggleLeftSidebar}
      />
      <main className="flex h-full min-h-0 min-w-0 flex-1 overflow-hidden">
        <ChatView send={send} sendBytes={sendBytes} />
      </main>
      {/* The avatar rail in the main window is redundant when the
          floating persona window is showing — Aiko is already on screen
          there. Hide it cleanly so the chat column gets the space back.
          When inline, a draggable separator lives between chat and
          avatar so the user can rebalance the columns; the handle reads
          + writes ``personaPanelWidth`` (clamped + persisted in the
          store). */}
      {personaVisible ? null : (
        <>
          <PanelResizeHandle
            ariaLabel="Resize avatar panel"
            onResize={(deltaX) =>
              setPersonaPanelWidth(personaPanelWidth - deltaX)
            }
          />
          <AvatarPanel />
        </>
      )}
      <SettingsDrawer open={settingsOpen} onClose={() => setSettingsOpen(false)} />
      {/* Desktop keeps the corner popups AND the archive drawer. */}
      <Toasts />
      <NotificationDrawer />
      <FirstRunOnboarding />
    </div>
  );
}

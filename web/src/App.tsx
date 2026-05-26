import { useEffect, useState } from "react";
import { AvatarPanel } from "./components/AvatarPanel";
import { ChatView } from "./components/ChatView";
import { PersonaWindow } from "./components/PersonaWindow";
import { SessionSidebar } from "./components/SessionSidebar";
import { SettingsDrawer } from "./components/SettingsDrawer";
import { Toasts } from "./components/Toasts";
import { desktop } from "./desktop/commands";
import { listenPersonaVisibility } from "./desktop/events";
import { isTauri } from "./desktop/runtime";
import { useAssistantSocket } from "./hooks/useAssistantSocket";
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
 * is untouched. */
function usePersonaVisibilitySync() {
  const setPersonaWindowVisible = useAssistantStore(
    (s) => s.setPersonaWindowVisible,
  );
  useEffect(() => {
    let cancelled = false;
    let unlisten: (() => void) | null = null;

    void desktop.isPersonaVisible().then((value) => {
      if (!cancelled) {
        setPersonaWindowVisible(Boolean(value));
      }
    });

    void listenPersonaVisibility((visible) => {
      if (!cancelled) {
        setPersonaWindowVisible(visible);
      }
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
  const { send } = useAssistantSocket();
  const [settingsOpen, setSettingsOpen] = useState(false);
  const route = useRoute();
  const tauri = isTauri();
  const personaVisible = useAssistantStore((s) => s.personaWindowVisible);

  // Subscribe only on the main window. The persona window doesn't need
  // to know its own visibility (it can ask the OS / DOM directly) and
  // shouldn't double-subscribe to the event bus.
  usePersonaVisibilitySync();

  if (route === "persona") {
    return <PersonaWindow send={send} />;
  }

  const togglePersona = () => {
    if (personaVisible) {
      void desktop.closePersona();
    } else {
      void desktop.openPersona();
    }
  };

  return (
    <div className="flex h-full w-full overflow-hidden">
      <SessionSidebar
        send={send}
        onOpenSettings={() => setSettingsOpen(true)}
        onTogglePersona={tauri ? togglePersona : undefined}
        personaWindowVisible={personaVisible}
      />
      <main className="flex h-full min-w-0 flex-1">
        <ChatView send={send} />
      </main>
      {/* The avatar rail in the main window is redundant when the
          floating persona window is showing — Aiko is already on screen
          there. Hide it cleanly so the chat column gets the space back. */}
      {personaVisible ? null : <AvatarPanel />}
      <SettingsDrawer open={settingsOpen} onClose={() => setSettingsOpen(false)} />
      <Toasts />
    </div>
  );
}

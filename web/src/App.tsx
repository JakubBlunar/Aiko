import { useState } from "react";
import { ChatView } from "./components/ChatView";
import { PersonaPanel } from "./components/PersonaPanel";
import { SessionSidebar } from "./components/SessionSidebar";
import { SettingsDrawer } from "./components/SettingsDrawer";
import { Toasts } from "./components/Toasts";
import { useAssistantSocket } from "./hooks/useAssistantSocket";

export default function App() {
  const { send } = useAssistantSocket();
  const [settingsOpen, setSettingsOpen] = useState(false);

  return (
    <div className="flex h-full w-full overflow-hidden">
      <SessionSidebar
        send={send}
        onOpenSettings={() => setSettingsOpen(true)}
      />
      <main className="flex h-full min-w-0 flex-1">
        <ChatView send={send} />
      </main>
      <PersonaPanel />
      <SettingsDrawer open={settingsOpen} onClose={() => setSettingsOpen(false)} />
      <Toasts />
    </div>
  );
}

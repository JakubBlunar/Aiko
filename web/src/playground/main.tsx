import { StrictMode, useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import { AvatarPlayground } from "./AvatarPlayground";
import type { PlaygroundProfile } from "./presets";
import "../index.css";

function PlaygroundLoader() {
  const [profile, setProfile] = useState<PlaygroundProfile | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    const controller = new AbortController();
    void fetch("/playground/profile.json", { signal: controller.signal }).then(async (response) => {
      if (!response.ok) throw new Error("Could not load the selected model profile.");
      const data = await response.json() as PlaygroundProfile;
      if (!data.loaded || !data.entry_filename || !data.playground_expressions) {
        throw new Error("Launch this page with npm run avatar:playground.");
      }
      setProfile(data);
    }).catch((err: unknown) => {
      if (!controller.signal.aborted) setError(err instanceof Error ? err.message : String(err));
    });
    return () => controller.abort();
  }, []);
  if (error) return <div className="playground-loading" role="alert">{error}</div>;
  return profile ? <AvatarPlayground profile={profile} />
    : <div className="playground-loading">Loading your model...</div>;
}

const root = document.getElementById("root");
if (!root) throw new Error("Missing #root element");
createRoot(root).render(<StrictMode><PlaygroundLoader /></StrictMode>);

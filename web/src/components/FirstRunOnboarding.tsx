import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api";
import { useAssistantStore } from "../store";

/**
 * Blocking first-run modal that asks the user for the name Aiko should
 * use when referring to them. Shown exactly when ``identity.needs_onboarding``
 * is true; closes when the backend confirms persistence via the
 * ``identity_changed`` WS broadcast (which flips the gate to false).
 *
 * Intentionally not dismissable -- every prompt block, transcript
 * formatter, and worker LLM call routes through ``user_display_name``,
 * so letting the modal be skipped would leak the ``"friend"`` fallback
 * into long-term memory rows.
 *
 * A re-opener for renames lives in the General tab of
 * :file:`SettingsDrawer.tsx`; this component only handles the empty-state
 * onboarding path.
 */
export function FirstRunOnboarding() {
  const identity = useAssistantStore((s) => s.identity);
  const setIdentity = useAssistantStore((s) => s.setIdentity);
  const pushToast = useAssistantStore((s) => s.pushToast);

  const [name, setName] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement | null>(null);

  // Autofocus the input the first time the modal appears so the user
  // can just start typing.
  useEffect(() => {
    if (identity?.needs_onboarding) {
      inputRef.current?.focus();
    }
  }, [identity?.needs_onboarding]);

  const submit = useCallback(
    async (event?: React.FormEvent) => {
      event?.preventDefault();
      const cleaned = name.trim();
      if (!cleaned) {
        setError("Please tell Aiko what to call you.");
        inputRef.current?.focus();
        return;
      }
      if (cleaned.length > 32) {
        setError("Keep it under 32 characters.");
        return;
      }
      setSubmitting(true);
      setError(null);
      try {
        const next = await api.setIdentity(cleaned);
        // The WS broadcast usually beats this response, but mirror it
        // locally so we don't depend on the network round-trip order
        // for the gate flip.
        setIdentity(next);
        pushToast("info", `Aiko will call you ${next.user_display_name}.`);
      } catch (err) {
        const message =
          err instanceof Error ? err.message : "Couldn't save the name.";
        setError(message);
        setSubmitting(false);
      }
    },
    [name, setIdentity, pushToast],
  );

  if (!identity || !identity.needs_onboarding) {
    return null;
  }

  return (
    <div
      className="fixed inset-0 z-[60] flex items-center justify-center bg-black/70 backdrop-blur-sm"
      role="dialog"
      aria-modal="true"
      aria-labelledby="first-run-title"
    >
      <form
        onSubmit={submit}
        className="w-[min(420px,calc(100vw-2rem))] rounded-2xl border border-white/10 bg-neutral-900 p-6 shadow-2xl"
      >
        <h2
          id="first-run-title"
          className="text-lg font-semibold text-neutral-100"
        >
          Hi! What should Aiko call you?
        </h2>
        <p className="mt-2 text-sm text-neutral-400">
          Aiko will use this in chat, in her inner thoughts, and when she
          tells stories about your time together. You can change it later
          in Settings.
        </p>
        <label className="mt-5 block">
          <span className="sr-only">Your name</span>
          <input
            ref={inputRef}
            type="text"
            value={name}
            onChange={(e) => {
              setName(e.target.value);
              if (error) setError(null);
            }}
            maxLength={32}
            autoComplete="off"
            spellCheck={false}
            placeholder="Your name"
            disabled={submitting}
            className="block w-full rounded-lg border border-neutral-700 bg-neutral-800 px-3 py-2 text-base text-neutral-100 placeholder:text-neutral-500 focus:border-sky-500 focus:outline-none focus:ring-1 focus:ring-sky-500 disabled:opacity-60"
          />
        </label>
        {error ? (
          <p className="mt-2 text-sm text-rose-400" role="alert">
            {error}
          </p>
        ) : null}
        <div className="mt-6 flex justify-end">
          <button
            type="submit"
            disabled={submitting || name.trim().length === 0}
            className="rounded-lg bg-sky-600 px-4 py-2 text-sm font-medium text-white shadow hover:bg-sky-500 focus:outline-none focus:ring-2 focus:ring-sky-400 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {submitting ? "Saving…" : "Meet Aiko"}
          </button>
        </div>
      </form>
    </div>
  );
}

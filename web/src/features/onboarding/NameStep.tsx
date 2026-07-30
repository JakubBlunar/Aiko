/**
 * First-run step 1: the name Aiko uses for the user.
 *
 * Split out of :file:`FirstRunOnboarding.tsx`; the parent owns the
 * state and the submit handler because the name is the one step whose
 * result has to round-trip through the backend before the modal's
 * ``needs_onboarding`` gate flips.
 */
export function NameStep({
  name,
  setName,
  submitting,
  error,
  setError,
  submit,
  inputRef,
}: {
  name: string;
  setName: (next: string) => void;
  submitting: boolean;
  error: string | null;
  setError: (next: string | null) => void;
  submit: (event?: React.FormEvent) => void | Promise<void>;
  inputRef: React.MutableRefObject<HTMLInputElement | null>;
}) {
  return (
    <form
      onSubmit={submit}
      className="w-[min(420px,calc(100vw-2rem))] rounded-2xl border border-white/10 bg-neutral-900 p-6 shadow-2xl"
    >
      <h2 id="first-run-title" className="text-lg font-semibold text-neutral-100">
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
  );
}

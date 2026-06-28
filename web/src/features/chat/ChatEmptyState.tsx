export function ChatEmptyState({ booting = false }: { booting?: boolean }) {
  // While the WS hasn't opened yet we show a "still connecting" hint
  // instead of the cheerful greeting. The greeting promises that the
  // user can type and get a reply, which isn't true until the backend
  // answers — see ``useAssistantSocket`` for the matching state.
  if (booting) {
    return (
      <div
        className="mx-auto mt-24 max-w-md text-center"
        role="status"
        aria-live="polite"
      >
        <div
          className="mx-auto mb-4 h-8 w-8 animate-spin rounded-full border-2 border-ink-100/20 border-t-ink-100/70"
          aria-hidden="true"
        />
        <h2 className="text-lg font-semibold text-ink-100">
          Waiting for Aiko…
        </h2>
        <p className="mt-2 text-sm text-ink-100/60">
          The desktop runtime is still starting the backend. This usually
          takes a few seconds; the chat will unlock as soon as the server
          answers.
        </p>
      </div>
    );
  }

  return (
    <div className="mx-auto mt-24 max-w-md text-center">
      <div className="text-5xl">🌸</div>
      <h2 className="mt-4 text-lg font-semibold text-ink-100">
        Hi, I'm Aiko.
      </h2>
      <p className="mt-2 text-sm text-ink-100/60">
        I'm here to chat about whatever's on your mind. Random thoughts,
        what you're working on, something you saw earlier today — drop a
        line and I'll pick up the thread. Speech in and speech out are
        wired through the desktop runtime, so I'll talk back through your
        speakers.
      </p>
    </div>
  );
}

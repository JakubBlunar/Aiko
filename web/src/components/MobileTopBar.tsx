import { useAssistantStore } from "../store";

interface MobileTopBarProps {
  /** Open the left navigation drawer (chat history + new + settings). */
  onOpenNav: () => void;
  /** Toggle the in-page floating persona window. */
  onTogglePersona: () => void;
  /** Whether the floating persona window is currently shown — drives the
   * pressed styling + aria-pressed on the persona button. */
  personaVisible: boolean;
}

function MenuIcon() {
  return (
    <svg
      viewBox="0 0 20 20"
      className="h-5 w-5"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.6}
      strokeLinecap="round"
      aria-hidden="true"
    >
      <path d="M3 5 H17 M3 10 H17 M3 15 H17" />
    </svg>
  );
}

function PersonaGlyph() {
  return (
    <svg
      viewBox="0 0 20 20"
      className="h-5 w-5"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.5}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <circle cx="10" cy="7" r="3.2" />
      <path d="M3.8 17 C4.4 13.4 6.8 12.2 10 12.2 C13.2 12.2 15.6 13.4 16.2 17" />
    </svg>
  );
}

/**
 * Phone-only top bar. Left: hamburger that opens the navigation drawer
 * (chat history, new chat, settings). Center: title + connection state.
 * Right: a toggle for the floating persona window. Rendered only by the
 * mobile layout branch in ``App`` so the desktop chrome is untouched.
 */
export function MobileTopBar({
  onOpenNav,
  onTogglePersona,
  personaVisible,
}: MobileTopBarProps) {
  const status = useAssistantStore((s) => s.connection.status);
  const dotColor =
    status === "connected"
      ? "bg-emerald-400"
      : status === "connecting"
        ? "bg-amber-400"
        : "bg-rose-400";
  return (
    <header className="z-10 flex shrink-0 items-center gap-2 border-b border-white/5 bg-black/40 px-3 py-2 backdrop-blur">
      <button
        type="button"
        onClick={onOpenNav}
        aria-label="Open menu"
        className="flex h-9 w-9 items-center justify-center rounded-md border border-white/10 text-ink-100/80 transition hover:border-ink-400 hover:text-ink-100"
      >
        <MenuIcon />
      </button>

      <div className="flex min-w-0 flex-1 items-center gap-2">
        <span className="truncate text-sm font-semibold text-ink-100">
          Aiko
        </span>
        <span
          className={`h-2 w-2 shrink-0 rounded-full ${dotColor}`}
          title={status}
          aria-label={`Connection: ${status}`}
        />
      </div>

      <button
        type="button"
        onClick={onTogglePersona}
        aria-label={personaVisible ? "Hide persona" : "Show persona"}
        aria-pressed={personaVisible}
        className={`flex h-9 w-9 items-center justify-center rounded-md border transition ${
          personaVisible
            ? "border-pink-400/70 bg-pink-500/15 text-pink-100"
            : "border-white/10 text-ink-100/80 hover:border-pink-400 hover:text-pink-100"
        }`}
      >
        <PersonaGlyph />
      </button>
    </header>
  );
}

import { Component, type ErrorInfo, type ReactNode } from "react";

import { buildCrashReport, reportUiCrash } from "../crashReport";
import { debugLog } from "../log";

/**
 * Top-level React error boundary.
 *
 * A single render/lifecycle exception anywhere in the child tree
 * (Live2D, a settings panel, a malformed WS payload reaching a render)
 * would otherwise white-screen the whole window with no recovery path.
 * This boundary contains the blast radius: it catches the throw, reports
 * it to the backend (``/api/logs/ui-crash`` — always on, so the cause
 * lands in ``data/app.log`` + ``crashlog.txt`` the next time it
 * happens), and renders a legible fallback with **Reload**, **Try
 * again**, and **Copy details** affordances instead of a blank page.
 *
 * Error boundaries only catch errors in the React render/commit phase.
 * Event-handler throws and async/promise rejections are handled
 * separately by the global ``window`` listeners installed via
 * :func:`installGlobalCrashReporters` in ``main.tsx`` (report-only).
 */

interface Props {
  children: ReactNode;
  /** Tag for the crash source, e.g. ``"main"`` or ``"persona"``. Lets
   * the log line attribute which window tree crashed. Defaults to the
   * generic ``render`` source. */
  label?: string;
}

interface State {
  error: Error | null;
  componentStack: string | null;
}

export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null, componentStack: null };

  static getDerivedStateFromError(error: Error): Partial<State> {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    const componentStack = info?.componentStack ?? null;
    this.setState({ componentStack });

    // Always report — this is the crash we want to see next time it
    // happens, regardless of the opt-in debug-logging toggle.
    const report = buildCrashReport({
      error,
      componentStack: componentStack ?? undefined,
      source: "render",
      url: typeof location !== "undefined" ? location.href : undefined,
      userAgent:
        typeof navigator !== "undefined" ? navigator.userAgent : undefined,
    });
    reportUiCrash(report);

    // Also drop a marker into the in-memory debug ring so a manual
    // "Download UI log" from the Diagnostics panel includes it (no-op
    // when the ring is disabled, but the ring still freezes its state).
    debugLog.log({
      source: "error-boundary",
      kind: this.props.label ? `crash:${this.props.label}` : "crash",
      payload: { message: report.message },
    });

    if (typeof console !== "undefined") {
      console.error("[error-boundary] render crash:", error, componentStack);
    }
  }

  private handleReload = (): void => {
    if (typeof location !== "undefined") location.reload();
  };

  private handleReset = (): void => {
    this.setState({ error: null, componentStack: null });
  };

  private handleCopy = (): void => {
    const { error, componentStack } = this.state;
    const details = [
      error?.message ?? "(no message)",
      "",
      error?.stack ?? "",
      "",
      "Component stack:",
      componentStack ?? "(none)",
    ].join("\n");
    try {
      void navigator.clipboard?.writeText(details);
    } catch {
      /* clipboard blocked — the text is visible in the panel anyway */
    }
  };

  render(): ReactNode {
    const { error, componentStack } = this.state;
    if (error === null) {
      return this.props.children;
    }

    return (
      <div
        role="alert"
        className="flex h-full w-full items-center justify-center overflow-auto bg-slate-950 p-6 text-slate-100"
        style={{ minHeight: "100vh" }}
      >
        <div className="w-full max-w-2xl rounded-xl border border-rose-500/40 bg-slate-900/90 p-6 shadow-2xl">
          <div className="flex items-start gap-3">
            <span className="select-none text-2xl text-rose-400" aria-hidden>
              ⚠
            </span>
            <div className="min-w-0 flex-1">
              <h1 className="text-lg font-semibold text-rose-200">
                Something went wrong
              </h1>
              <p className="mt-1 text-sm text-slate-300">
                The interface hit an error and stopped rendering. The crash
                has been logged so the cause can be tracked down. You can try
                recovering, or reload the app.
              </p>

              <p className="mt-4 break-words rounded-md border border-rose-500/30 bg-rose-950/40 px-3 py-2 font-mono text-sm text-rose-100">
                {error.message || "(no message)"}
              </p>

              {(error.stack || componentStack) && (
                <details className="mt-3 text-xs text-slate-400">
                  <summary className="cursor-pointer select-none text-slate-300 hover:text-slate-100">
                    Technical details
                  </summary>
                  <pre className="mt-2 max-h-64 overflow-auto whitespace-pre-wrap break-words rounded-md bg-slate-950/80 p-3 leading-snug">
                    {error.stack || ""}
                    {componentStack
                      ? `\n\nComponent stack:${componentStack}`
                      : ""}
                  </pre>
                </details>
              )}

              <div className="mt-5 flex flex-wrap gap-2">
                <button
                  type="button"
                  onClick={this.handleReload}
                  className="rounded-md bg-rose-600 px-4 py-2 text-sm font-medium text-white transition-colors hover:bg-rose-500"
                >
                  Reload app
                </button>
                <button
                  type="button"
                  onClick={this.handleReset}
                  className="rounded-md border border-slate-600 px-4 py-2 text-sm font-medium text-slate-200 transition-colors hover:bg-slate-800"
                >
                  Try again
                </button>
                <button
                  type="button"
                  onClick={this.handleCopy}
                  className="rounded-md border border-slate-700 px-4 py-2 text-sm font-medium text-slate-400 transition-colors hover:bg-slate-800 hover:text-slate-200"
                >
                  Copy details
                </button>
              </div>
            </div>
          </div>
        </div>
      </div>
    );
  }
}

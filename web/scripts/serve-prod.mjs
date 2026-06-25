#!/usr/bin/env node
/**
 * Production-serve orchestrator for the Aiko web UI.
 *
 * Builds the React bundle into ``web/dist`` and then starts the Python
 * backend (``python -m app.web``), which serves that bundle at the same
 * origin as ``/api`` and ``/ws`` on ``127.0.0.1:6275``. Optionally wires
 * Tailscale Serve so the app is reachable over HTTPS from your phone
 * (the secure context the microphone requires).
 *
 * Usage (from web/):
 *   npm run serve:prod:web              # build + run backend
 *   npm run serve:prod:web:tailscale    # build + tailscale serve + backend
 *   npm run serve:web                   # run backend only (skip build)
 *
 * Flags (passed through to this script):
 *   --no-build        Skip the vite build (serve whatever is in dist/).
 *   --tailscale       Run ``tailscale serve --bg <port>`` before booting.
 *   --port=NNNN       Backend port (default 6275 / $AIKO_WEB_PORT).
 *
 * Environment overrides:
 *   AIKO_PYTHON   Python interpreter to use (default "python"). Point
 *                 this at your venv's python if you normally activate
 *                 one before running ``python -m app.web``.
 *   AIKO_WEB_PORT Backend port (default 6275).
 */
import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";
import fs from "node:fs";
import path from "node:path";

const here = path.dirname(fileURLToPath(import.meta.url));
const webDir = path.resolve(here, ".."); // web/
const repoRoot = path.resolve(webDir, ".."); // repository root

const argv = process.argv.slice(2);
const hasFlag = (name) => argv.includes(name);
const getOpt = (name, fallback) => {
  const hit = argv.find((a) => a.startsWith(`${name}=`));
  return hit ? hit.slice(name.length + 1) : fallback;
};

const doBuild = !hasFlag("--no-build");
const useTailscale = hasFlag("--tailscale");
const port = getOpt("--port", process.env.AIKO_WEB_PORT || "6275");
const python = process.env.AIKO_PYTHON || "python";
const isWin = process.platform === "win32";

/** Run a command to completion.
 *
 * ``shell`` must be opt-in per command: ``npm`` is a ``.cmd`` shim on
 * Windows that only runs through a shell, but a real ``.exe`` with a
 * space in its path (``C:\Program Files\Tailscale\tailscale.exe``) is
 * mangled by the shell and must be spawned WITHOUT one (argv is passed
 * verbatim to CreateProcess). */
function run(cmd, args, opts = {}) {
  return new Promise((resolve, reject) => {
    const child = spawn(cmd, args, {
      stdio: "inherit",
      shell: false,
      ...opts,
    });
    child.on("error", reject);
    child.on("exit", (code) =>
      code === 0
        ? resolve()
        : reject(new Error(`${cmd} exited with code ${code}`)),
    );
  });
}

/** Locate the Tailscale CLI. The Windows installer ships the GUI +
 * service but frequently leaves ``tailscale.exe`` off PATH, so fall
 * back to the standard install locations before giving up. Override
 * with ``AIKO_TAILSCALE`` to point at a custom path. */
function resolveTailscale() {
  if (process.env.AIKO_TAILSCALE) return process.env.AIKO_TAILSCALE;
  if (isWin) {
    const candidates = [
      path.join(
        process.env.ProgramFiles || "C:\\Program Files",
        "Tailscale",
        "tailscale.exe",
      ),
      path.join(
        process.env["ProgramFiles(x86)"] || "C:\\Program Files (x86)",
        "Tailscale",
        "tailscale.exe",
      ),
    ];
    for (const candidate of candidates) {
      try {
        if (fs.existsSync(candidate)) return candidate;
      } catch {
        /* unreadable path — keep looking */
      }
    }
  }
  // Last resort: hope it's on PATH (spawn will search it).
  return "tailscale";
}

async function main() {
  if (doBuild) {
    console.log("[serve:prod] Building frontend (tsc + vite build)…");
    // npm is a .cmd shim on Windows -> needs a shell.
    await run("npm", ["run", "build"], { cwd: webDir, shell: isWin });
  } else {
    console.log("[serve:prod] Skipping build (--no-build).");
  }

  if (useTailscale) {
    const tailscale = resolveTailscale();
    console.log(
      `[serve:prod] Exposing 127.0.0.1:${port} over Tailscale HTTPS ` +
        `(${tailscale})…`,
    );
    try {
      await run(tailscale, ["serve", "--bg", String(port)]);
      console.log(
        `[serve:prod] Tailscale Serve active. Run '${tailscale} serve ` +
          "status' to see the https URL.",
      );
    } catch (err) {
      console.warn(
        `[serve:prod] tailscale serve failed (${err.message}).`,
      );
      console.warn(
        `[serve:prod] Run it manually:  "${tailscale}" serve --bg ${port}`,
      );
      console.warn(
        "[serve:prod] (If tailscale.exe lives elsewhere, set " +
          "AIKO_TAILSCALE to its full path.)",
      );
    }
  }

  console.log(
    `[serve:prod] Starting backend: ${python} -m app.web  (cwd=${repoRoot})`,
  );
  // ``python`` is a real .exe; spawn resolves it via PATH without a
  // shell, which also makes Ctrl+C kill Python directly (no cmd wrapper
  // swallowing the signal).
  const backend = spawn(python, ["-m", "app.web"], {
    cwd: repoRoot,
    stdio: "inherit",
    shell: false,
  });

  const forward = (sig) => {
    if (!backend.killed) {
      try {
        backend.kill(sig);
      } catch {
        /* already gone */
      }
    }
  };
  process.on("SIGINT", () => forward("SIGINT"));
  process.on("SIGTERM", () => forward("SIGTERM"));
  backend.on("error", (err) => {
    console.error(`[serve:prod] Failed to start backend: ${err.message}`);
    console.error(
      "[serve:prod] Is Python on PATH? Set AIKO_PYTHON to your venv python.",
    );
    process.exit(1);
  });
  backend.on("exit", (code) => process.exit(code ?? 0));
}

main().catch((err) => {
  console.error(`[serve:prod] ${err.message}`);
  process.exit(1);
});

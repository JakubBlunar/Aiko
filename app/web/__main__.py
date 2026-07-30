"""Headless entry point: ``python -m app.web``.

Boots the SessionController without Qt, starts the web server (and the
embedded MCP server, if enabled in config), and blocks until SIGINT.
Open http://localhost:6275 in a browser to use the React UI.
"""
from __future__ import annotations

import logging
import os
import signal
import sys
import threading
import time

from app.core.infra.crash_logging import (
    configure_logging_full,
    install_global_exception_hooks,
)
from app.core.session.session_controller import SessionController
from app.core.infra.settings import load_settings
from app.web.runner import WebServerRunner
from app.web.server import create_web_app


log = logging.getLogger("app.web")


def _apply_env_overrides(settings) -> None:
    """Let a few env vars override config so a container needs no file edits.

    Docker users would otherwise have to mount a ``config/user.json`` just
    to bind ``0.0.0.0`` and point at the host's Ollama. These three vars
    cover the only knobs a fresh containerised install actually needs:

      * ``AIKO_WEB_HOST``        -> web_server.host   (use 0.0.0.0 in Docker)
      * ``AIKO_WEB_PORT``        -> web_server.port
      * ``AIKO_OLLAMA_BASE_URL`` -> the ``base_url`` of every Ollama
        provider in the catalogue (e.g. http://host.docker.internal:11434,
        or http://ollama:11434 for an in-compose Ollama). Chat, workers
        and embeddings all resolve their endpoint through the catalogue,
        so this one value retargets them together. Remote
        (``openai_compatible``) providers are left alone.

    Every override is best-effort: a malformed value is logged and ignored
    rather than crashing boot.
    """
    web = getattr(settings, "web_server", None)
    if web is not None:
        host = os.environ.get("AIKO_WEB_HOST")
        if host:
            web.host = host.strip()
        port = os.environ.get("AIKO_WEB_PORT")
        if port:
            try:
                web.port = int(port)
            except (TypeError, ValueError):
                log.warning("ignoring invalid AIKO_WEB_PORT=%r", port)
    base_url = (os.environ.get("AIKO_OLLAMA_BASE_URL") or "").strip()
    llm = getattr(settings, "llm", None)
    if base_url and llm is not None:
        retargeted = [
            provider.id
            for provider in llm.providers
            if provider.kind == "ollama"
        ]
        for provider in llm.providers:
            if provider.kind == "ollama":
                provider.base_url = base_url
        # The transport template is derived at load time, so refresh it
        # too or the main-chat client would still dial the old host.
        if getattr(settings, "ollama", None) is not None:
            settings.ollama.base_url = base_url
        log.info(
            "AIKO_OLLAMA_BASE_URL=%s applied to provider(s): %s",
            base_url,
            ", ".join(retargeted) or "(none)",
        )


def main() -> int:
    install_global_exception_hooks()
    settings = load_settings()
    _apply_env_overrides(settings)
    logging_settings = getattr(settings, "logging", None)
    log_level = (
        os.environ.get("LOG_LEVEL")
        or getattr(logging_settings, "level", None)
        or "INFO"
    )
    configure_logging_full(
        level_name=log_level,
        module_levels=getattr(logging_settings, "module_levels", None) or {},
        file_enabled=bool(getattr(logging_settings, "file_enabled", True)),
        file_path=getattr(logging_settings, "file_path", None),
        file_max_bytes=int(getattr(logging_settings, "file_max_bytes", 5 * 1024 * 1024)),
        file_backup_count=int(getattr(logging_settings, "file_backup_count", 5)),
    )

    log.info("Booting Aiko (web mode)...")
    session = SessionController(settings)
    try:
        session.prewarm_runtime(on_status=lambda msg: log.info("[startup] %s", msg))
    except Exception as exc:
        log.warning("Prewarm failed: %s", exc)

    web_settings = getattr(settings, "web_server", None)
    web_port = int(getattr(web_settings, "port", 6275)) if web_settings is not None else 6275
    web_host = str(getattr(web_settings, "host", "127.0.0.1") or "127.0.0.1")

    if web_settings is not None and not getattr(web_settings, "enabled", True):
        log.warning("web_server.enabled is False in config; running in CLI-only mode.")
        runner: WebServerRunner | None = None
    else:
        app = create_web_app(session)
        runner = WebServerRunner(app, host=web_host, port=web_port)
        runner.start()
        log.info("Open http://%s:%d in your browser to chat with Aiko.", web_host, web_port)

    stop_event = threading.Event()

    def _shutdown(*_: object) -> None:
        log.info("Shutdown requested.")
        stop_event.set()

    try:
        signal.signal(signal.SIGINT, _shutdown)
        signal.signal(signal.SIGTERM, _shutdown)
    except (AttributeError, ValueError):
        pass

    try:
        while not stop_event.is_set():
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        if runner is not None:
            try:
                runner.stop()
            except Exception:
                log.debug("web runner stop failed", exc_info=True)
        try:
            session.shutdown()
        except Exception:
            log.debug("session shutdown failed", exc_info=True)

    log.info("Goodbye.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

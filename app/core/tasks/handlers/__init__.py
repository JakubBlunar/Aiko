"""Built-in task handlers shipped with phase 1 of the brain-orchestration refactor.

Each handler is a stateless implementation of one long-running
workflow that satisfies the :class:`TaskHandler` protocol from
:mod:`app.core.tasks.task_handler`.

Phase 1 ships:

* :class:`FileSearchHandler` — read-only filename substring search
  across the configured :data:`task_file_allowed_roots`. Demonstrates
  the ``running → done`` happy path with periodic ``TaskProgress``
  emits while walking the tree.
* :class:`FileReadHandler` — read-only single-file fetch.
  Demonstrates the ``running → awaiting_input → done`` path when a
  bare filename matches multiple roots (the canonical multi-root
  disambiguation case).

Handlers are registered with the orchestrator via
:meth:`TaskOrchestrator.register_handler`. See
:mod:`app.core.session.task_orchestration_mixin` for the boot-time
wiring.
"""
from __future__ import annotations

from app.core.tasks.handlers.file_read import FileReadHandler
from app.core.tasks.handlers.file_search import FileSearchHandler
from app.core.tasks.handlers.web_search import WebSearchHandler


__all__ = ["FileSearchHandler", "FileReadHandler", "WebSearchHandler"]

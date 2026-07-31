"""A session double for the web-route tests that keeps the real facade.

The route tests configure behaviour by setting the *private* attributes the
controller keeps (``_settings``, ``_task_store``, ``_earcons``, ...), while the
routes themselves go through the *public* surface in
:class:`~app.core.session.web_facade_mixin.WebFacadeMixin`. A bare
``MagicMock`` answers both and connects neither: ``session.tasks.store``
returns a freshly invented child mock rather than the real ``TaskStore`` the
fixture built, so the route quietly exercises nothing and the test still
passes -- or, when a test waits on a broadcast that can now never fire, hangs.

:class:`FakeSession` inherits the mixin, so the public surface is the real
code, and falls through to a ``MagicMock`` for the rest of the controller's
very large surface. That keeps the fixtures short, keeps
``session.some_method.assert_called_once()`` working, and means a rename or a
dropped subsystem sync fails these tests instead of sailing past them.

Private attributes fall through too, which is what lets a test assert on the
subsystem a facade method was supposed to poke -- ``session._earcons.enabled``
and ``session._proactive.update_runtime.assert_called_with(...)`` still work,
and now they are checking the controller's real behaviour rather than a route
body's.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from app.core.session.web_facade_mixin import WebFacadeMixin


class FakeSession(WebFacadeMixin):
    """Real web facade in front of a ``MagicMock`` for everything else."""

    def __init__(self, **attrs: Any) -> None:
        object.__setattr__(self, "mock", MagicMock())
        # Defaults matching the real controller, so payloads built from them
        # carry plausible values instead of a mock's repr. Overridable.
        self._user_id = "default"
        self._missing_chat_model = ""
        for key, value in attrs.items():
            setattr(self, key, value)

    def __getattr__(self, name: str) -> Any:
        # Only reached when normal lookup fails, so the mixin's members and
        # anything the test assigned take precedence over the mock.
        if name == "mock":
            raise AttributeError(name)
        return getattr(self.mock, name)

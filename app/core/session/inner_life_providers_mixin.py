"""Inner-life prompt-block providers mixin.

Extracted from :mod:`app.core.session.session_controller` to keep the controller
shell readable. Covers every per-turn ``_render_*`` block provider that
the prompt assembler asks for, plus the K16 grounding-context builder,
the small avatar-capability accessors used by the prompt grammar, and
the ``_cadence_context`` helper that feeds the cadence engine.

These are pure read methods that delegate to stores already on
``self`` (``self._affect_store``, ``self._memory_store``, etc.), so
they have no init-order risk: the mixin only ever runs after
``SessionController.__init__`` has finished wiring the host class.

State ownership stays in ``SessionController.__init__``; this mixin
just reads ``self.*``.

NB: tests that previously patched
``app.core.session.session_controller.<symbol>`` for any of the moved methods
must patch ``app.core.session.inner_life_providers_mixin.<symbol>``
instead. The patch must target the module where the symbol is
*looked up*.
"""
from __future__ import annotations

from app.core.session.inner_life_shared import (  # noqa: F401  (re-exported)
    _APPRECIATION_VIBES,
    _KV_APPRECIATION_ANCHOR,
    _KV_APPRECIATION_AT,
    _KV_RECIP_VULN_AT,
    _MILESTONE_PHRASES,
    _circadian,
    _format_running_task_line,
)
from app.core.session.inner_life_part1 import InnerLifePart1Mixin
from app.core.session.inner_life_part2 import InnerLifePart2Mixin
from app.core.session.inner_life_part3 import InnerLifePart3Mixin
from app.core.session.inner_life_part4 import InnerLifePart4Mixin


class InnerLifeProvidersMixin(
    InnerLifePart1Mixin,
    InnerLifePart2Mixin,
    InnerLifePart3Mixin,
    InnerLifePart4Mixin,
):
    """Per-turn prompt-block providers, grounding builder, avatar accessors."""


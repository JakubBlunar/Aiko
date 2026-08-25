"""One-shot debug overrides: armed from outside, consumed once by a provider.

The MCP debug tools steer the next turn by arming a flag the inner-life
providers check — "surface the turning-over cue even though the gap gate says
no", "make today's colour amber", "pin the response mode". Before this module
each of those was an ad-hoc attribute on :class:`SessionController`: armed with
``session._turning_over_force_next = True``, consumed with a ``getattr`` and a
manual reset, and cleaned up by two hand-maintained lists in
``lifecycle_mixin``.

Those lists were the problem. Of the 43 flags the tools arm, a session switch
cleared 11 and a memory wipe cleared 14 — and the two disagreed with each other
about three more. So an override armed and never fired stayed armed across a
session switch and went off later in an unrelated conversation, which is a
genuinely confusing thing to debug and had bitten us. There was no way to ask
what was armed, and a typo in a flag name was indistinguishable from a cue that
simply chose not to fire.

Collecting them here fixes all three at once: :meth:`clear` drops everything by
construction so no list can drift, :meth:`snapshot` answers what is armed, and
arming an unregistered name raises instead of writing a dead attribute nobody
reads.

Names are registered in :data:`KNOWN_OVERRIDES`, which doubles as the
description the ``list_debug_overrides`` MCP tool reports.

Out of scope: ``TurnRunner._tool_gate_force_next``. It is the same kind of
one-shot flag but it lives on the runner rather than the controller, so it
would need the runner to hold a reference to this registry. Worth doing; not
worth widening this change for.

Thread safety: providers consume on the brain-loop and worker threads while the
MCP server arms from its own. :meth:`take` has to be read-and-disarm as one
step or "one-shot" is a lie under concurrency, so every operation takes a lock.
"""
from __future__ import annotations

import logging
import threading
from types import MappingProxyType
from typing import Any, Mapping


log = logging.getLogger("app.session.debug_overrides")


# name -> what arming it does. Every name an MCP tool can arm must appear here;
# ``tests/test_debug_overrides.py`` checks that the two stay in step.
KNOWN_OVERRIDES: Mapping[str, str] = MappingProxyType({
    # ── inner-life cue gates (arm True, provider surfaces once) ──────
    "aspiration_momentum_force_next": "Bypass the aspiration-momentum cue watermark once.",
    "associative_wander_force_next": "K64a - bypass the associative-wander gates once.",
    "away_activities_force_next": "K36 - bypass the away-activities gates once.",
    "concept_learning_force_next": "L17e - bypass the learning-reflection gates once.",
    "conduct_notice_force_next": "L42 - bypass conduct-notice trust and cooldown gates once.",
    "curiosity_gradient_force_next": "K64c - bypass the curiosity-gradient gates once.",
    "dormant_interest_force_next": "K67 - bypass the dormant-interest gates once.",
    "earned_familiarity_force_next": "K66 - bypass the earned-familiarity gates once.",
    "follow_up_force_next": "Bypass the follow-up cue watermark once.",
    "forward_curiosity_force_next": "K34 - bypass the forward-curiosity gates once.",
    "growth_witness_force_next": "Bypass the growth-witness cue watermark once.",
    "idle_seed_force_next": "H17 - bypass the idle-seed surfacing gates once.",
    "initiative_force_next": "K53 - arm a one-shot initiative directive.",
    "interest_drift_force_next": "K64b - bypass the interest-drift gates once.",
    "knowledge_gap_notice_force_next": "F10f - bypass the knowledge-gap-notice gates once.",
    "long_arc_callback_force_next": "K63 - bypass the long-arc cap, cooldown and min-words once.",
    "misattunement_force_next": "K23 - bypass the misattunement cooldown once.",
    "mood_drift_force_surface": "H3 - bypass the mood-drift cooldown and signature gates once.",
    "mood_inertia_force": "K45 - force a mood-inertia cue once.",
    "opinion_injection_force_next": "K29 - bypass the opinion-injection cooldown and cap once.",
    "second_thought_force_next": "K96 - bypass the second-thought surfacing cadence once.",
    "self_callback_force_next": "Bypass the self-callback cue watermark once.",
    "session_clock_force_next": "K-time4 - bypass the session-clock provider gates once.",
    "shared_ritual_force_next": "Bypass the shared-ritual surfacing gates once.",
    "sleep_return_force_next": "H21 - bypass the sleep-return gates once.",
    "stance_persistence_force_next": "K46 - bypass the warm-stance window once.",
    "tease_collection_force_next": "K59 - bypass the humor, cooldown and age gates once.",
    "taste_lean_force_next": "K81 - arm a one-shot 'lean toward what you love' steer.",
    "topic_appetite_force_next": "K54 - arm a one-shot 'tapped out' negotiation slip.",
    "topic_confidence_force_next": "F10i - bypass the topic-confidence provider gates once.",
    "topic_temperature_force_next": "F10h - bypass the topic-temperature provider gates once.",
    "turning_over_force_next": "K28 - bypass the turning-over gap gate once.",
    "upcoming_horizon_force_next": "K-time3 - bypass the upcoming-horizon provider gates once.",
    "user_expertise_force_next": "K75 - bypass the user-expertise provider gates once.",
    "wants_force_imperative": "K52 - bypass the want imperative band once.",
    "wellbeing_concern_force_next": "Bypass the wellbeing-concern cue watermark once.",
    # ── self-noticing (K30) ──────────────────────────────────────────
    "self_noticing_force_agreement": "K30 - bypass the agreement-streak cooldown once.",
    "self_noticing_force_flat_affect": "K30 - bypass the flat-affect cooldown once.",
    "self_noticing_force_repeated_thought": "K30 - bypass the repeated-thought flag once.",
    # ── persona / affect ─────────────────────────────────────────────
    "mask_force_slip_next": "K60 - make the next masked episode slip once.",
    "vulnerability_budget_force_reset": "Reset the vulnerability budget before the next check.",
    "day_color_force_reroll": "K27 - reroll today's colour once.",
    # ── consumed by a provider but, until now, unreachable ───────────
    # Each of these is read and cleared by a provider and described in its
    # docstring as an MCP bypass -- ``_tension_force_next`` even names the
    # tool -- but no tool ever armed one, so the documented behaviour could
    # only be reached from a test. Registering them is what lets the arm
    # tools below exist.
    "appreciation_force_next": "J10 - bypass the appreciation-beat cooldown once.",
    "reciprocal_vulnerability_force_next": "J9 - bypass the reciprocal-vulnerability gates once.",
    "tension_force_next": "Bypass the tension-cue watermark once (ring must still be non-empty).",
    "vulnerability_budget_force_spent": "K15 - render the budget cue as if this much were spent.",
    # ── payload-carrying: the value is the override ──────────────────
    "day_color_force_next": "K27 - render this palette name instead of today's roll.",
    "implicit_need_force_mode": "K69 - pin the next turn's response-mode steer to this mode.",
    "question_balance_suppress_remaining": "K47 - suppress Aiko's questions for this many turns.",
    "tease_rhythm_force": "K48 - arm this tease-rhythm band for the next turn.",
    "vitality_force_energy": "K68 - override body energy with this value.",
})


class UnknownOverride(KeyError):
    """An override name that is not in :data:`KNOWN_OVERRIDES`.

    Raised at the arm site on purpose. The failure it replaces was silent: a
    misspelled flag became an attribute nothing ever read, so the tool reported
    success and the cue simply never fired.
    """

    def __init__(self, name: str) -> None:
        super().__init__(
            f"unknown debug override {name!r}; add it to KNOWN_OVERRIDES in "
            f"app/core/session/debug_overrides.py",
        )
        self.name = name


class DebugOverrides:
    """A registry of armed one-shot overrides for the current session."""

    __slots__ = ("_armed", "_lock")

    def __init__(self) -> None:
        self._armed: dict[str, Any] = {}
        self._lock = threading.Lock()

    # ── arming ───────────────────────────────────────────────────────

    def arm(self, name: str, payload: Any = True) -> None:
        """Arm ``name``, optionally carrying a value the consumer reads.

        ``payload`` defaults to ``True`` for the common "bypass your gates
        once" flag. The payload-carrying overrides pass the value itself -- a
        palette name, a response mode, a turn count.

        Arming with a falsy payload still counts as armed: ``suppress for 0
        turns`` is a meaningful instruction, and consumers ask for the value
        rather than the flag's truthiness.
        """
        if name not in KNOWN_OVERRIDES:
            raise UnknownOverride(name)
        with self._lock:
            self._armed[name] = payload
        log.debug("debug override armed: %s=%r", name, payload)

    def disarm(self, name: str) -> None:
        """Drop ``name`` if armed. No error when it is not."""
        with self._lock:
            self._armed.pop(name, None)

    # ── consuming ────────────────────────────────────────────────────

    def take(self, name: str, default: Any = None) -> Any:
        """Return the payload and disarm, or ``default`` when not armed.

        The one call a provider should use. Read-and-disarm is atomic, so two
        threads reaching the same cue cannot both fire it.
        """
        with self._lock:
            if name not in self._armed:
                return default
            payload = self._armed.pop(name)
        log.debug("debug override fired: %s=%r", name, payload)
        return payload

    def peek(self, name: str, default: Any = None) -> Any:
        """Return the payload without disarming.

        For status dumps. A provider that peeks instead of taking turns a
        one-shot override into a permanent one.
        """
        with self._lock:
            return self._armed.get(name, default)

    def is_armed(self, name: str) -> bool:
        with self._lock:
            return name in self._armed

    # ── lifecycle ────────────────────────────────────────────────────

    def clear(self) -> int:
        """Disarm everything and return how many were dropped.

        Called on a session switch and a memory wipe. Dropping the whole dict
        is the point: the hand-written lists this replaces covered 11 and 14 of
        43 flags respectively, so overrides leaked into unrelated sessions.
        """
        with self._lock:
            count = len(self._armed)
            self._armed.clear()
        if count:
            log.debug("debug overrides cleared: %d armed", count)
        return count

    def snapshot(self) -> dict[str, Any]:
        """What is armed right now, for the debug tools."""
        with self._lock:
            return dict(self._armed)

    def __len__(self) -> int:
        with self._lock:
            return len(self._armed)

    def __contains__(self, name: object) -> bool:
        with self._lock:
            return name in self._armed

    def __repr__(self) -> str:
        return f"DebugOverrides({sorted(self.snapshot())!r})"


class DebugOverridesHostMixin:
    """Gives a class a :class:`DebugOverrides`, created on first use.

    Mixed into every provider mixin that consumes an override. The laziness
    is for the tests: they exercise providers through small hand-built hosts
    that inherit one mixin and set up only the handful of attributes the
    provider under test reads. Requiring each of those ~40 hosts to also
    construct a registry would be pure ceremony, and forgetting one would
    surface as an ``AttributeError`` in an unrelated assertion.

    :class:`SessionController` still builds its registry explicitly in
    ``__init__`` -- hence the setter -- so the real object's state is not
    conjured by a property read.
    """

    @property
    def debug_overrides(self) -> DebugOverrides:
        """The armed one-shot debug overrides. Public for the MCP tools."""
        return self._debug_overrides

    @property
    def _debug_overrides(self) -> DebugOverrides:
        registry = self.__dict__.get("_debug_overrides_registry")
        if registry is None:
            registry = DebugOverrides()
            self.__dict__["_debug_overrides_registry"] = registry
        return registry

    @_debug_overrides.setter
    def _debug_overrides(self, registry: DebugOverrides) -> None:
        self.__dict__["_debug_overrides_registry"] = registry

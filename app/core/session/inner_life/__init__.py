"""Inner-life pickers for session-boundary cues (K28 etc.).

This package holds picker / scorer utilities that select one piece
of stored content (a reflection, a goal note, a callback) to
surface as a one-shot inner-life cue on a session boundary. They
are pure functions over already-loaded data so each picker stays
trivially testable; the providers in
:mod:`app.core.session.inner_life_providers_mixin` wire them to
live :class:`MemoryStore` / :class:`GoalStore` / :class:`RagStore`
state.
"""

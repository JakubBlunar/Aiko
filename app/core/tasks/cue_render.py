"""Pure render functions for the task-cue T6 prompt block.

Called from ``PromptAssembler.assemble_with_budget`` (chunk 5
wiring) to convert a list of :class:`TaskCue` rows into the
T6 inner-life system-prompt block Aiko reads on her next turn.
Lives in its own module — separate from
:mod:`app.core.tasks.task_cue_store` — so the render layer is
trivially unit-testable without any state.

The block has two sub-headers, both optional:

* **Successes** — cues with ``kind=task_result`` AND
  ``status in {"done", "cancelled"}`` (cancellations are surfaced
  as "the X task was cancelled" so Aiko knows the work stopped on
  purpose, not from a bug).
* **Failures** — cues with ``kind=task_result`` AND
  ``status == "failed"``, PLUS cues with
  ``kind=task_input_needed`` (which surface as questions Aiko needs
  to ask). The persona block teaches a slightly apologetic +
  curious tone for failures, vs the breezy success tone.

Empty cue list → empty string (the assembler's ``if block`` cascade
naturally skips empty providers, so the T6 cluster doesn't grow a
no-op header).

The renderer applies the aggregation cap as a SAFETY check — the
cue store has usually already enforced it via
:meth:`TaskCueStore.drain_for_render`, but render is the last line
of defence so a buggy caller can't blow up the prompt.
"""
from __future__ import annotations

from typing import Iterable

from app.core.tasks.task_cue_store import (
    CUE_KIND_INPUT_NEEDED,
    CUE_KIND_RESULT,
    TaskCue,
)


# Header text matches the design doc's "Aggregation and the failure
# sub-header" section. Keep stable — the persona block in
# ``data/persona/aiko_companion.txt`` will key off these exact
# phrases for the success-vs-failure tone discipline.
_SUCCESS_HEADER = "Tasks that finished since your last message:"
_FAILURE_HEADER = "Tasks that ran into trouble since your last message:"
_QUESTION_HEADER = "Tasks waiting on your call since your last message:"


def render_cue_block(
    cues: Iterable[TaskCue],
    *,
    max_aggregated: int = 5,
) -> str:
    """Render parked cues into a T6 system-prompt block.

    Returns an empty string when there are no cues to render —
    callers can safely concat the result into ``system_parts``
    without a guard.

    ``max_aggregated`` is the hard cap on bullets across all three
    sub-sections combined. Defaults to ``5`` to match
    ``agent.task_cue_max_aggregated``; callers SHOULD pass the
    live setting, but the default is the safe choice when missing.

    Cues are bucketed into three lanes:

    * **questions** — ``kind=task_input_needed`` (regardless of
      status), surfaced under the "waiting on your call" header
      so Aiko opens with the question naturally.
    * **failures** — ``kind=task_result`` + ``status=failed``,
      surfaced under the "ran into trouble" header.
    * **successes** — every other result cue
      (``done`` / ``cancelled``).

    Within each lane, cues render in the order received (FIFO from
    the store). Bullets are formatted as ``- <title> — <body>``.
    Empty title falls back to ``the task``, empty body falls back
    to the status word.
    """
    cap = max(1, int(max_aggregated))
    questions: list[TaskCue] = []
    failures: list[TaskCue] = []
    successes: list[TaskCue] = []

    # First pass: bucket. Walk in order so the FIFO-ness from the
    # store is preserved inside each lane.
    for cue in cues:
        if cue.kind == CUE_KIND_INPUT_NEEDED:
            questions.append(cue)
        elif cue.kind == CUE_KIND_RESULT and cue.status == "failed":
            failures.append(cue)
        elif cue.kind == CUE_KIND_RESULT:
            successes.append(cue)
        # Unknown kinds drop silently — the cue store rejects
        # unknown kinds at park time, so this is dead code, but
        # the safety belt is cheap.

    # Apply the global cap across all three lanes combined.
    # Priority order: questions > failures > successes (questions
    # are the most pressing — a blocked task needs an answer
    # before further work can happen; failures next because they
    # carry an "I tried and it didn't work" tone Aiko needs to
    # acknowledge; successes last because they're the most
    # naturally folded-into-conversation).
    remaining = cap
    qs = questions[:remaining]
    remaining -= len(qs)
    fs = failures[:remaining]
    remaining -= len(fs)
    ss = successes[:remaining]

    if not qs and not fs and not ss:
        return ""

    lines: list[str] = []
    if qs:
        lines.append(_QUESTION_HEADER)
        for cue in qs:
            lines.append(_format_question_bullet(cue))
    if fs:
        if lines:
            lines.append("")  # blank line between sections
        lines.append(_FAILURE_HEADER)
        for cue in fs:
            lines.append(_format_failure_bullet(cue))
    if ss:
        if lines:
            lines.append("")
        lines.append(_SUCCESS_HEADER)
        for cue in ss:
            lines.append(_format_success_bullet(cue))
    return "\n".join(lines)


# ── per-cue bullet formatters ───────────────────────────────────────


def _format_success_bullet(cue: TaskCue) -> str:
    """``- file_search "Q4 report" — found 3 documents``"""
    title = cue.title.strip() or "the task"
    summary = cue.summary.strip()
    if not summary:
        status_word = cue.status.strip() or "done"
        summary = status_word
    return f"- {title} — {summary}"


def _format_failure_bullet(cue: TaskCue) -> str:
    """``- file_read "huge_log.txt" — file too large (max 256 KB)``"""
    title = cue.title.strip() or "the task"
    err = (cue.error or "").strip()
    if not err:
        # Fall back to the summary if the handler didn't set
        # ``error`` explicitly (rare, but defensive — failure
        # cues should always carry an error string).
        err = cue.summary.strip() or "failed (no error reported)"
    return f"- {title} — {err}"


def _format_question_bullet(cue: TaskCue) -> str:
    """``- file_search "meetings" — found a lot of matches; should I focus on recent ones? [recent / oldest / all]``"""
    title = cue.title.strip() or "the task"
    question = cue.summary.strip() or "waiting for your input"
    if cue.options:
        # Render up to a handful of options; truncate with an
        # ellipsis if the handler emitted a huge list. The cap
        # keeps the T6 block tight on the most volatile tier.
        shown = list(cue.options)[:6]
        opts_str = " / ".join(shown)
        if len(cue.options) > len(shown):
            opts_str = f"{opts_str} / …"
        return f"- {title} — {question} [{opts_str}]"
    return f"- {title} — {question}"


__all__ = ["render_cue_block"]

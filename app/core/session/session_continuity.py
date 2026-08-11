"""Bridge the seam between two conversations.

A "conversation" is a UI affordance. Jacob starts a new one when he wants
a visual divider in his own sidebar — it is filing, and it says nothing
about whether the relationship paused. Aiko's side of it was the
opposite: every session-scoped thing resets at that boundary and nothing
crosses it. The transcript is empty, the rolling summary and the K21
thread note are keyed by ``session_id`` so both come back blank, and
*every* gap cue — J5 reconnection, K14 absence curiosity, K28 turning
over, H21 sleep return, K36 away activities, K34 forward curiosity —
measures from the previous assistant message **in the same session** and
therefore stays silent, because there isn't one.

So the one moment where she most needs to know "we were talking about X,
about three hours ago" is precisely the moment she knows least: she wakes
with long-term memory and relationship state intact but no idea that a
conversation just ended, and greets him accordingly. That is the whole
bug. This module renders the bridge.

Pure and side-effect free: the provider in
:mod:`app.core.session.prompt_assembler_helpers_mixin` does the reads and
hands the values in, so the phrasing and the elapsed-time branch can be
tested without a database.
"""

from __future__ import annotations

from datetime import datetime

from app.core.infra import timephrase


#: Under this gap the two conversations are really one sitting split by a
#: click, so any acknowledgement of absence would be a false note — he
#: was here minutes ago. Above it, noticing the gap is the *natural*
#: thing and pretending otherwise is the false note. Six hours is chosen
#: to sit under J5's reconnection floor so the two never both speak: J5
#: covers gaps inside a session, this covers gaps across the seam.
CONTINUOUS_WINDOW_SECONDS = 6 * 3600

#: Notes run 142-396 characters in practice. The cap is a guard against a
#: pathological one, not a routine trim.
MAX_NOTE_CHARS = 420


def trim_note(note: str, *, max_chars: int = MAX_NOTE_CHARS) -> str:
    """Clip an over-long thread note, preferring a sentence boundary.

    Cutting mid-word reads as corruption in the prompt and invites her to
    complete the sentence herself, so fall back to a hard clip only when
    there is no sentence end in the usable range.
    """
    text = " ".join((note or "").split())
    if len(text) <= max_chars:
        return text
    window = text[:max_chars]
    cut = max(window.rfind(". "), window.rfind("! "), window.rfind("? "))
    if cut >= max_chars // 2:
        return window[: cut + 1]
    return window.rstrip() + "..."


def render_continuity_block(
    *,
    last_message_iso: str,
    note: str,
    now: datetime,
    user_name: str,
) -> str:
    """The bridge, or ``""`` when there is nothing to bridge from.

    ``note`` is the previous conversation's K21 thread note and may be
    empty — only 27 of 45 sessions have one, since short threads never
    earn a re-summary. The elapsed time is still worth saying on its own:
    without it she cannot tell a new window from a first meeting, which
    is the failure this block exists to prevent.

    The elapsed phrase is computed here from message timestamps and never
    read out of the note prose. K21 notes carry their own dates and those
    dates are not reliable — the live store has one that opens "Jacob
    fell asleep on June 29, 2026" on a thread whose messages are all from
    August.
    """
    ago = timephrase.humanize_past(last_message_iso, now)
    if ago == "in the past":
        # Unparseable timestamp. "How long ago" is half the point of the
        # block and a vague stand-in would be worse than the silence.
        return ""

    # ``_resolve_user_display_name`` falls back to the literal "the user",
    # which reads as a stage direction in a sentence about him.
    who = (user_name or "").strip()
    if not who or who == "the user":
        who = "he"
    lines = [
        "Continuing an ongoing conversation. A new window is how %s keeps "
        "his own notes tidy; it is not a new relationship and not a first "
        "meeting." % who,
        "You two last spoke %s." % ago,
    ]

    clean_note = trim_note(note)
    if clean_note:
        lines.append("Where that thread stood: " + clean_note)

    parsed = timephrase.parse_iso(last_message_iso)
    elapsed = (
        (timephrase.to_aware(now) - parsed).total_seconds()
        if parsed is not None
        else 0.0
    )
    if elapsed < CONTINUOUS_WINDOW_SECONDS:
        lines.append(
            "That is close enough to be the same sitting. Carry on from it: "
            "no greeting him as though you have not spoken, no "
            "re-introducing yourself, and no recap unless he asks for one."
        )
    else:
        lines.append(
            "Enough time has passed that noticing it is natural, the way "
            "you would with someone you know well, not as a reunion. Even "
            "so, the thread above is where you left off, so don't start "
            "from zero."
        )
    return "\n".join(lines)

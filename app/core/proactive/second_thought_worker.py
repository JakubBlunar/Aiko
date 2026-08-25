"""K96 -- the post-reply think pass ("second thought").

Every reply Aiko gives is one forward pass. Whatever deliberation the
model does inside it is stripped by ``strip_thinking_blocks_with_signal``
and thrown away, so a thought she was half-way through when the reply
went out cannot survive to the next turn. That is the ceiling this lifts:
the moments that read as intelligence are usually *second* thoughts --
catching the implication, noticing the question behind the question,
realising you had the answer in front of you and talked past it.

**Why this runs after the reply rather than before it.** Both shapes were
measured before either was built (``data/prompt-cache.jsonl``, 1,162
turns). A pre-stream pass costs time-to-first-token, which is the one
latency a companion cannot hide -- 54% of turns currently reach first
token inside 1.5s, and a capped think call adds ~0.5-1.1s at the measured
p50 of 112 tok/s. Running it *after* the reply has already been delivered
costs the user nothing: it lands in the gap while they are reading and
typing, so the thought is waiting before their next message arrives.

The reply pass is also already at ``reasoning_effort: "low"``, so she is
not thinking zero times per turn today. Raising that knob buys hidden
thinking on the *same* input and competes with the visible reply for one
combined output budget. A separate call does not compete, gets its own
budget, and -- because the system prefix is byte-identical to the one the
turn just sent -- pays a cache *read* for its ~74k characters of input
rather than sending them again. That is what made this affordable, and it
is why the pass must reuse ``PromptTelemetry.system_prompt`` verbatim
instead of assembling a prompt of its own.

**What it is not.** Three neighbours are close enough to be worth naming,
because minting a fourth near-duplicate is the failure mode here:

- ``pre_thought`` guesses what the user will ask *next* and pre-drafts an
  answer, on the local worker model.
- ``turning_over`` surfaces a between-sessions reflection when the user
  comes *back* from an absence.
- ``reflection`` journals the exchange into a memory.

This is the only one that reads *her own reply* against *the context she
was actually handed* and asks what she failed to do with it -- which is
also why it has to run on the chat model, where that context is cached.

Output goes to the cue pool as a ``second_thought`` cue, so it inherits
retries, expiry and real consumption tracking: "she was shown this and
did not take it" is recorded rather than assumed. It passes the pool's
lexical-trace test (see ``docs/cue-pool.md``) because the cue names a
subject she would have to say out loud to act on it.
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable

from app.core.infra import timephrase
from app.llm.chat_client import CACHE_BREAKPOINT_KEY

if TYPE_CHECKING:
    from app.llm.chat_client import ChatClient


log = logging.getLogger("app.second_thought_worker")


# The pass declines by saying this and nothing else. Most turns should:
# a reply that already said what needed saying does not need a second
# thought, and a worker that invents one on every turn produces slop that
# the pool then dutifully retries.
_DECLINE = "NONE"

_SUBJECT_RE = re.compile(r"^\s*SUBJECT\s*:\s*(.+)$", re.IGNORECASE | re.MULTILINE)
_THOUGHT_RE = re.compile(r"^\s*THOUGHT\s*:\s*(.+)$", re.IGNORECASE | re.MULTILINE)

# Bounds on what may reach the pool. The subject is a matching key, so a
# sentence there would poison consumption (one shared word between a
# sentence and a reply means nothing -- the reasoning behind
# ``min_overlap`` on the policy). The thought is prompt text and pays
# rent in T6 every turn it surfaces.
_MAX_SUBJECT_CHARS = 80
_MAX_THOUGHT_CHARS = 400
# Guards against a degenerate one-word subject ("work", "it") that would
# match almost any reply and mark itself used immediately.
_MIN_SUBJECT_CHARS = 6

_INSTRUCTION = "\n".join((
    "[Private note to yourself. This is not a message to {name} -- it is "
    "never sent, never spoken, and nobody but you will read it.]",
    "",
    "{anchor}",
    "",
    "You have just sent the reply above. Before this turn closes, read it "
    "back once and ask yourself two things:",
    "",
    "1. Were you still chewing on something? A thread you under-answered, "
    "an implication you noticed but did not follow, the question behind "
    "{name}'s question that you never actually got to.",
    "2. Was something already in front of you -- in your memories, in what "
    "you know about {name}, in the concepts you hold about him -- that was "
    "relevant here, and that you talked straight past?",
    "",
    "If the answer to both is no, reply with exactly:",
    "{decline}",
    "",
    "Most turns are {decline}, and that is the correct answer. A reply that "
    "already said what needed saying does not need a second thought, and "
    "inventing one is worse than not having one.",
    "",
    "Otherwise reply with exactly two lines and nothing else:",
    "SUBJECT: <the specific thing to come back to -- 2 to 6 words, in the "
    "words you would actually use out loud>",
    "THOUGHT: <one or two sentences, first person, saying what you want to "
    "pick up and why it matters. If you missed something you already had, "
    "say plainly what it was.>",
    "",
    "{time_rule}",
))


@dataclass(slots=True)
class SecondThought:
    """One drafted second thought, before it becomes a cue row."""

    subject: str = ""
    thought: str = ""

    def is_empty(self) -> bool:
        return not (self.subject and self.thought)


def build_instruction(user_display_name: str = "the user") -> str:
    """The private closing instruction appended after the exchange.

    Split out so a test can assert the declining path is offered without
    standing up an LLM, and so the wording is reviewable in one place.
    """
    return _INSTRUCTION.format(
        name=user_display_name or "the user",
        anchor=timephrase.today_anchor(),
        decline=_DECLINE,
        time_rule=timephrase.STORED_TEXT_TIME_RULE,
    )


def parse_second_thought(raw: str) -> SecondThought:
    """Best-effort parse of the pass's reply.

    Anything unparseable is an empty :class:`SecondThought` rather than an
    exception: this runs in a background job whose failure must be silent,
    and a malformed draft is exactly as useful as a decline.
    """
    text = (raw or "").strip()
    if not text:
        return SecondThought()
    # Checked before the field regexes rather than after, because a model
    # that declines sometimes explains itself for a line or two and the
    # explanation must not be mistaken for a thought.
    if text.upper().startswith(_DECLINE) or text.upper() == _DECLINE:
        return SecondThought()
    subject_hit = _SUBJECT_RE.search(text)
    thought_hit = _THOUGHT_RE.search(text)
    if subject_hit is None or thought_hit is None:
        return SecondThought()
    subject = " ".join(subject_hit.group(1).split()).strip(" .\"'*")
    thought = " ".join(thought_hit.group(1).split()).strip()
    if len(subject) < _MIN_SUBJECT_CHARS or not thought:
        return SecondThought()
    return SecondThought(
        subject=subject[:_MAX_SUBJECT_CHARS],
        thought=thought[:_MAX_THOUGHT_CHARS],
    )


def render_cue_text(thought: str) -> str:
    """The cue line stored for the prompt.

    Written at production time, like every pooled cue, so the provider
    never re-renders. The framing is deliberately thin -- the handling
    note in ``conditional_handling.txt`` carries the instructions, and
    duplicating them here would pay for them on every surfacing.
    """
    return f"Still on your mind from earlier: {thought}"


class SecondThoughtWorker:
    """Draft one second thought about the turn that just ended.

    Turn-triggered rather than idle-scheduled: the material is the
    exchange that just happened, so there is no useful sense in which
    this could run "when convenient". :class:`SessionController` submits
    it into the speaking window after the reply is out, which is where the
    latency hides.
    """

    def __init__(
        self,
        *,
        ollama: "ChatClient",
        model: str,
        queue_cue: Callable[[str, str, dict[str, Any]], bool],
        pending_count: Callable[[], int] | None = None,
        settings_provider: Callable[[], Any] | None = None,
        user_display_name_provider: Callable[[], str] | None = None,
        inventory_target: int = 2,
    ) -> None:
        self._ollama = ollama
        self._model = model
        self._queue_cue = queue_cue
        self._pending_count = pending_count
        self._settings_provider = settings_provider
        self._user_display_name_provider = user_display_name_provider
        self._inventory_target = max(0, int(inventory_target))
        self._last_run_at = 0.0
        self._stats: dict[str, int] = {
            "scheduled": 0,
            "skipped_disabled": 0,
            "skipped_recent": 0,
            "skipped_thin": 0,
            "skipped_stocked": 0,
            "declined": 0,
            "unparsed": 0,
            "queued": 0,
            "failed": 0,
        }
        self._last_thought: SecondThought | None = None
        self._last_ms = 0.0

    # ── public ──────────────────────────────────────────────────────────

    def stats(self) -> dict[str, Any]:
        out: dict[str, Any] = dict(self._stats)
        last = self._last_thought
        out["last_subject"] = last.subject if last is not None else ""
        out["last_thought"] = last.thought if last is not None else ""
        out["last_ms"] = round(self._last_ms, 1)
        return out

    def update_runtime(self, *, model: str | None = None) -> None:
        if model:
            self._model = str(model)

    def maybe_run(
        self,
        *,
        system_prompt: str,
        user_text: str,
        assistant_text: str,
        cache_breakpoints: tuple[int, ...] = (),
        session_key: str = "",
        stop_flag: Any = None,
        force: bool = False,
    ) -> SecondThought | None:
        """Draft and queue one second thought, or return ``None`` on skip.

        Every gate here is cheap and local, and they are ordered cheapest
        first: the point of the clock and the length floors is to decide
        against spending a call, so they must not cost one.

        ``force`` (the MCP debug path) skips the clock, the length floors
        and the stock check, but deliberately NOT ``second_thought_enabled``
        -- a master switch a debug tool can talk past is not a master
        switch, and this one exists to keep the feature dark by default.
        """
        agent = self._agent_settings()
        if not bool(getattr(agent, "second_thought_enabled", False)):
            self._stats["skipped_disabled"] += 1
            return None
        if not system_prompt or not assistant_text:
            self._stats["skipped_thin"] += 1
            return None

        now = time.monotonic()
        if not force:
            min_gap = float(
                getattr(agent, "second_thought_min_gap_seconds", 180),
            )
            if self._last_run_at and now - self._last_run_at < min_gap:
                self._stats["skipped_recent"] += 1
                return None

            min_user = int(
                getattr(agent, "second_thought_min_user_chars", 80),
            )
            min_reply = int(
                getattr(agent, "second_thought_min_reply_chars", 120),
            )
            if (
                len(user_text.strip()) < min_user
                or len(assistant_text.strip()) < min_reply
            ):
                self._stats["skipped_thin"] += 1
                return None

            # A stocked shelf means she is already holding thoughts she has
            # not used. Drafting past that is how a target of 2 becomes a
            # shelf of 14 (see ``ForwardCuriosityWorker`` and
            # docs/cue-pool.md) -- and this producer is turn-triggered, so
            # it would fill far faster than an idle one.
            if self._stocked():
                self._stats["skipped_stocked"] += 1
                return None

        if stop_flag is not None and getattr(stop_flag, "is_set", None) is not None:
            if stop_flag.is_set():
                return None

        # Reserve the slot before the call, so a failing model throttles
        # itself rather than retrying every turn.
        self._last_run_at = now
        self._stats["scheduled"] += 1
        return self._run(
            system_prompt=system_prompt,
            user_text=user_text,
            assistant_text=assistant_text,
            cache_breakpoints=cache_breakpoints,
            session_key=session_key,
        )

    # ── internals ───────────────────────────────────────────────────────

    def _agent_settings(self) -> Any:
        if self._settings_provider is None:
            return None
        try:
            settings = self._settings_provider()
        except Exception:
            return None
        return getattr(settings, "agent", settings)

    def _stocked(self) -> bool:
        if self._pending_count is None or self._inventory_target <= 0:
            return False
        try:
            return int(self._pending_count()) >= self._inventory_target
        except Exception:
            return False

    def _user_name(self) -> str:
        if self._user_display_name_provider is None:
            return "the user"
        try:
            return self._user_display_name_provider() or "the user"
        except Exception:
            return "the user"

    def _messages(
        self,
        *,
        system_prompt: str,
        user_text: str,
        assistant_text: str,
        cache_breakpoints: tuple[int, ...],
    ) -> list[dict[str, Any]]:
        """The request, shaped so its prefix is a cache read.

        ``system_prompt`` is reused byte-for-byte and the same breakpoint
        offsets are re-attached, because every marked prefix lies *inside*
        the system message -- so whatever follows it, the service can still
        read from the longest entry the turn just wrote. Rebuilding or
        trimming this string would silently turn a cache read back into a
        ~74k-character send.
        """
        system_msg: dict[str, Any] = {
            "role": "system", "content": system_prompt,
        }
        if cache_breakpoints:
            system_msg[CACHE_BREAKPOINT_KEY] = tuple(cache_breakpoints)
        return [
            system_msg,
            {"role": "user", "content": user_text},
            {"role": "assistant", "content": assistant_text},
            {"role": "user", "content": build_instruction(self._user_name())},
        ]

    def _run(
        self,
        *,
        system_prompt: str,
        user_text: str,
        assistant_text: str,
        cache_breakpoints: tuple[int, ...],
        session_key: str,
    ) -> SecondThought | None:
        agent = self._agent_settings()
        max_tokens = max(32, int(getattr(agent, "second_thought_max_tokens", 160)))
        started = time.monotonic()
        try:
            raw = self._ollama.chat(
                self._messages(
                    system_prompt=system_prompt,
                    user_text=user_text,
                    assistant_text=assistant_text,
                    cache_breakpoints=cache_breakpoints,
                ),
                options={
                    "num_predict": max_tokens,
                    # Shares the turn's cache affinity key on purpose: the
                    # prefix is the same prefix, so routing it to the same
                    # place is what makes the read land.
                    "prompt_cache_key": session_key,
                },
                model=self._model,
                surface="second_thought",
            )
        except Exception:
            self._stats["failed"] += 1
            log.debug("second-thought call failed", exc_info=True)
            return None
        finally:
            self._last_ms = (time.monotonic() - started) * 1000.0

        thought = parse_second_thought(raw)
        if thought.is_empty():
            # Declining and failing to parse are counted apart because the
            # first is the designed majority case and the second is a bug
            # signal -- a diagnostic that merged them could not tell a
            # well-behaved pass from a broken one.
            if (raw or "").strip().upper().startswith(_DECLINE):
                self._stats["declined"] += 1
            else:
                self._stats["unparsed"] += 1
                log.debug("second-thought unparsed: %r", (raw or "")[:200])
            return None

        self._last_thought = thought
        try:
            queued = self._queue_cue(
                thought.subject,
                render_cue_text(thought.thought),
                {
                    "subject": thought.subject,
                    "thought": thought.thought,
                    "drafted_at": timephrase.utcnow().isoformat(
                        timespec="seconds",
                    ),
                },
            )
        except Exception:
            self._stats["failed"] += 1
            log.debug("second-thought queue failed", exc_info=True)
            return None
        if not queued:
            self._stats["failed"] += 1
            return None
        self._stats["queued"] += 1
        log.info(
            "second thought: subject=%r ms=%.0f",
            thought.subject, self._last_ms,
        )
        return thought

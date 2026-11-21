"""Cross-session support replays with the normal response-mode prompt provider."""

from types import SimpleNamespace

import pytest

from app.core.conversation.support_preferences import support_context
from app.core.infra.chat_database import ChatDatabase
from app.core.session.inner_life_part2 import InnerLifePart2Mixin
from app.core.session.prompt_assembler import PromptAssembler


_REPAIR = (
    ("user", "I only wanted company, not advice."),
    ("assistant", "I'm sorry I jumped in with advice. Next time you vent, I'll listen first."),
)
_WORK_VENT = "I need to vent about work. My manager moved the deadline again."
_PERSONAL_VENT = "I need to vent. My neighbor played music all night."
_AGREEMENT_QUESTION = "What did we agree about when I vent?"
_AFFIRMATION = "Yes, please."
_WORK_EXCEPTION = (
    ("assistant", "When you vent about work, do you want me to listen first?"),
    ("user", "For work things I actually want ideas."),
)


class _Host(InnerLifePart2Mixin):
    _user_id = "replay"
    session_key = "replay:new"
    user_display_name = "Sam"

    def __init__(self, db):
        self._chat_db = db
        self._settings = SimpleNamespace(agent=SimpleNamespace())
        self._memory_settings = SimpleNamespace()
        self._prompt_assembler = PromptAssembler(db, continuity_max_messages=0)
        self._prompt_assembler.set_inner_life_providers(
            implicit_need=self._render_implicit_need_block,
        )

    def prompt(self, text):
        message_id = self._chat_db.add_message(self.session_key, "user", text)
        _, telemetry = self._prompt_assembler.assemble_with_budget(
            self.session_key, text, current_user_message_id=message_id,
            context_window=32768, response_budget=512,
        )
        return telemetry.system_prompt


@pytest.fixture
def replay(tmp_path):
    db = ChatDatabase(tmp_path / "support-replay.db")
    try:
        yield db, _Host(db)
    finally:
        db._get_conn().close()


def _append(db, *exchange, session="replay:prior"):
    return [db.add_message(session, role, text) for role, text in exchange]


@pytest.mark.parametrize(("ending", "learned"), [
    (_AFFIRMATION, True),
    ("Thanks, I'm feeling much better now.", False),
    ("Yes, please. But I still want advice if I sound stressed.", False),
])
def test_identical_work_vent_uses_explicit_agreement_without_reopening_it(replay, ending, learned):
    db, host = replay
    _append(db, *_REPAIR, ("user", ending))
    prompt = host.prompt(_WORK_VENT)
    assert ("Response preference for this kind of vent" in prompt) == learned
    if learned:
        assert "Listen first and acknowledge" in prompt
        assert "Be a witness first" not in prompt
        assert "Bring up the earlier conversation only if the user asks" in prompt
    assert all(text not in prompt for _, text in _REPAIR)

    recall = host.prompt(_AGREEMENT_QUESTION)
    if learned:
        assert "Accepted offer for venting" in recall
        assert all(text in recall for _, text in _REPAIR)
        assert _AFFIRMATION in recall
        assert "User-reported outcome about listening" not in recall
    else:
        assert "found no explicit preference, agreement or reported outcome" in recall
        assert "Leave past behavior and usual habits unspecified" in recall
        assert ending not in recall


def test_work_exception_changes_only_work_vents_and_current_requests_win(replay):
    db, host = replay
    _append(db, *_REPAIR, ("user", _AFFIRMATION))
    _append(db, *_WORK_EXCEPTION, session="replay:work")

    work_prompt = host.prompt(_WORK_VENT)
    assert "offer one practical idea" in work_prompt
    assert "Be a witness first" not in work_prompt
    assert "Listen first and acknowledge" in host.prompt(_PERSONAL_VENT)
    assert "Response preference for this kind of vent" not in host.prompt("How was your day?")

    direct_advice = host.prompt(_PERSONAL_VENT + " Can you give me some advice?")
    assert "it's okay to get concrete here" in direct_advice
    assert "Response preference for this kind of vent" not in direct_advice
    direct_listen = host.prompt(_WORK_VENT + " Could you listen for a bit?")
    assert "Be a witness first" in direct_listen
    assert "Response preference for this kind of vent" not in direct_listen

    recall = host.prompt(_AGREEMENT_QUESTION)
    assert "Accepted offer for venting" in recall
    assert "User's stated preference for work vents" in recall
    assert _WORK_EXCEPTION[1][1] in recall


def test_explicit_preference_and_report_remain_separate_from_agreement(replay):
    db, host = replay
    stated = "When I vent, please listen first."
    result = "It helped when you listened first while I was venting."
    _append(db, ("user", stated))
    _append(db, ("user", result), session="replay:later")
    assert "Listen first and acknowledge" in host.prompt(_PERSONAL_VENT)
    recall = host.prompt(_AGREEMENT_QUESTION)
    assert "User's stated preference for venting" in recall
    assert "Accepted offer" not in recall
    assert "User-reported outcome about listening" in recall and result in recall
    assert "say it helped and leave the reason open" in recall


def test_outcome_question_uses_the_report_instead_of_the_plan(replay):
    db, host = replay
    _append(db, *_REPAIR, ("user", _AFFIRMATION))
    question = "Did listening first help when I was venting?"
    unknown = host.prompt(question)
    assert "The outcome of listening first is unconfirmed" in unknown
    assert _AFFIRMATION not in unknown and "Accepted offer" not in unknown
    report_id, = _append(db, ("user", "It helped when you listened first while I was venting."))
    known = host.prompt(question)
    assert f"[Message {report_id}]" in known
    assert "It helped when you listened first" in known
    assert "Accepted offer" not in known


@pytest.mark.parametrize("source", [0, 1, 2])
def test_editing_any_source_withdraws_an_inferred_agreement(replay, source):
    db, host = replay
    ids = _append(db, *_REPAIR, ("user", _AFFIRMATION))
    assert "Accepted offer for venting" in host.prompt(_AGREEMENT_QUESTION)
    assert db.update_message_content(ids[source], "I changed what this message said.")
    assert "Response preference for this kind of vent" not in host.prompt(_WORK_VENT)
    assert "Accepted offer for venting" not in host.prompt(_AGREEMENT_QUESTION)


def test_source_deletion_and_later_work_revision_affect_selected_scope(replay):
    db, host = replay
    _append(db, *_REPAIR, ("user", _AFFIRMATION))
    question, _ = _append(db, *_WORK_EXCEPTION, session="replay:work")
    assert "offer one practical idea" in host.prompt(_WORK_VENT)
    assert db.delete_message_rows("replay:work", [question]) == [question]
    assert "Listen first and acknowledge" in host.prompt(_WORK_VENT)
    _append(db, ("user", "When I vent about work, I want ideas."), session="replay:revision")
    assert "offer one practical idea" in host.prompt(_WORK_VENT)
    _append(db, ("user", "When I vent about work, please listen first."), session="replay:revision")
    assert "Listen first and acknowledge" in host.prompt(_WORK_VENT)


def test_rejects_unlinked_work_preference_quoted_requests_and_cross_owner_sources(replay):
    db, host = replay
    _append(db, *_REPAIR, ("user", _AFFIRMATION), session="someone-else:prior")
    _append(db, ("user", _WORK_EXCEPTION[1][1]))
    _append(db, ("user", 'She wrote "When I vent, please listen first."'))
    _append(db, ("user", "When I vent, please listen first unless I ask about travel."))
    assert "Response preference for this kind of vent" not in host.prompt(_WORK_VENT)

    _append(db, ("user", "When I vent, please listen first."))
    quoted = '"I need to vent about work" is dialogue in a script.'
    assert support_context(db, host.session_key, quoted) is None
    assert support_context(db, host.session_key, "Imagine I need to vent about work.") is None
    actual = 'I need to vent. They asked me, “Can you give me some advice?”'
    assert "Listen first and acknowledge" in host.prompt(actual)


def test_agreement_needs_venting_scope_and_keeps_any_expressed_work_limit(replay):
    db, host = replay
    offer = ("assistant", "Next time I'll listen first.")
    _append(db, _REPAIR[0], offer, ("user", _AFFIRMATION))
    assert "Response preference for this kind of vent" not in host.prompt(_WORK_VENT)

    _append(db, ("user", "I was just venting about work, not asking for advice."),
            offer, ("user", _AFFIRMATION), session="replay:work")
    assert "Listen first and acknowledge" in host.prompt(_WORK_VENT)
    assert "Response preference for this kind of vent" not in host.prompt(_PERSONAL_VENT)


def test_ordinary_turns_and_direct_requests_need_no_archive_lookup():
    def unexpected_read(*args, **kwargs):
        pytest.fail("The support-history gate should skip this lookup")

    db = SimpleNamespace(messages_in_range=unexpected_read)
    for text in (
        "A memory test made this test fail.",
        "We talked about listening when I vent, then moved on.",
        _WORK_VENT + " Can you give me some advice?",
        _PERSONAL_VENT + " Could you just listen?",
    ):
        assert support_context(db, "replay:new", text) is None

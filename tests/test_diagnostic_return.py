import pytest

from app.core.conversation.conversation_situation import ConversationSituationStore
from app.core.infra.chat_database import ChatDatabase
from scripts.diagnostic_return_replay import CASES, RESULT, press_reply_fits, seed


@pytest.mark.parametrize(("reply", "corrected", "passed"), [
    ("With streaks on the fresh plate, a faulty roller fits better. "
     "A damaged plate remains possible.", False, True),
    ("The clean print suggests a damaged plate, though a faulty roller is still possible.",
     True, True),
    ("The streaks confirm a faulty roller. A damaged plate remains possible.", False, False),
    ("With a clean print, a faulty roller fits better. A damaged plate remains possible.",
     True, False),
    ("Given the streaks, a faulty roller fits better. Replace it.", False, False),
])
def test_press_replay_requires_the_matching_tentative_conclusion(reply, corrected, passed):
    assert press_reply_fits(reply, corrected=corrected) is passed


@pytest.mark.parametrize("case", CASES)
def test_checked_diagnosis_after_two_observed_detour_turns(tmp_path, case):
    db = ChatDatabase(tmp_path / "chat.db")
    host, report = seed(db, case)
    host._conversation_situation_store = ConversationSituationStore(db)
    answer, question = CASES[case]
    block = host._render_conversation_situation_block(question)
    recap = host.reviewed_comparison_reply(question)
    if case in {"present", "absent", "wrong_input"}:
        assert RESULT[answer] in recap and "?" not in recap
        assert ("leaves both explanations open" if case == "wrong_input"
                else "still tentative") in recap
    else:
        assert recap == ""
    if case == "unrelated":
        assert block == ""
    elif case == "other_device":
        assert "no test result" in block and RESULT[answer] not in block
    else:
        if case != "correction":
            assert f"Earlier user report [messages {report}]: {RESULT[answer]}" in block
        if case == "wrong_input":
            assert "inconclusive between" in block and "Earlier assessment" not in block
        elif case == "correction":
            assert "new report" in block and "Earlier assessment" not in block
            assert RESULT[answer] not in block
            assert ("An input-side fault fits better. A call transport fault remains possible."
                    in block)
            assert ("A call transport fault fits better. An input-side fault remains possible."
                    in block)
        else:
            favored = "An input-side fault" if answer == "yes" else "A call transport fault"
            assert "Earlier assessment: " + favored in block
        assert "Discussing pottery" not in block
        assert db.update_message_content(report, "I never performed that test.")
        host._conversation_situation_snapshot_cache = None
        assert host._render_conversation_situation_block(question) == ""
        assert host.reviewed_comparison_reply(question) == ""


def test_unprocessed_correction_withholds_old_verdict(tmp_path):
    db = ChatDatabase(tmp_path / "chat.db")
    host, _ = seed(db, "present")
    db.add_message(host.session_key, "user", "Correction: that was the wrong file.")
    question = CASES["present"][1]
    assert host.reviewed_comparison_reply(question) == ""
    assert host._render_conversation_situation_block(question) == ""

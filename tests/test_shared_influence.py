from contextlib import closing
from types import SimpleNamespace

from app.core.conversation.shared_influence import check_influence
from app.core.infra import timephrase
from app.core.infra.chat_database import ChatDatabase
from scripts.longitudinal_reasoning_replay import (
    BORROWED, CARD, NOW, OFFER, START, TRIED, seed_rows,
)


def test_followup_resolves_current_topic_to_original_roles_and_dates(tmp_path):
    db = ChatDatabase(tmp_path / "chat.db")
    with closing(db._get_conn()), timephrase.scoped_now(lambda: NOW):
        seed_rows(db, "sam", [CARD, OFFER, TRIED, BORROWED,
                  (9, "user", "I tried your pottery idea."), (10, "user", "A postcard question?")])
        seed_rows(db, "elsewhere", [(7, "user", "My secret postcard project.")])
        db.add_message("sam:x:noremember", "user", "My secret postcard idea.")
        mid = db.add_message("sam:here", "user", "Whose idea was it?")
        history = [SimpleNamespace(role="user", content=START)]
        result = check_influence("Whose idea was it?", history, db, "sam", "sam:here", mid)
        assert OFFER[2] in result.guidance and TRIED[2] in result.guidance
        assert '"speaker": "assistant"' in result.guidance and "2026-06-08" in result.guidance
        assert "pottery" not in result.guidance and "secret" not in result.guidance
        missing = check_influence("Whose idea was it?", [], db, "sam", "sam:here", mid)
        assert "unclear" in missing.guidance
        unrelated = check_influence("Did I ever try coffee?", [], db, "sam", "sam:here", mid)
        assert not unrelated.guidance


def test_attribution_changes_with_evidence_and_specific_contribution(tmp_path):
    db = ChatDatabase(tmp_path / "chat.db")
    with closing(db._get_conn()), timephrase.scoped_now(lambda: NOW):
        def reply():
            return check_influence("Did I ever try your one-line postcard idea?", [],
                                   db, "sam", "sam:here", None).reply

        seed_rows(db, "sam", [CARD, OFFER, (4, "user", "I used your dating idea for postcards.")])
        assert "haven't found" in reply()
        seed_rows(db, "sam", [(8, "user", "I tried your one-line idea.")])
        assert "You said" in reply() and "haven't found" not in reply()
        seed_rows(db, "sam", [(5, "assistant", "We could add one line to the menu.")])
        assert "haven't found" in reply()
        seed_rows(db, "sam", [(10, "user", "I didn't try the one-line postcard idea.")])
        assert "didn't try" in reply()


def test_reading_origin_preserves_roles_and_isolates_unrelated_histories(tmp_path):
    from app.core.conversation.taste_development import check_taste
    from scripts.longitudinal_reasoning_replay import LAST_LINE, READING

    db = ChatDatabase(tmp_path / "chat.db")
    with closing(db._get_conn()), timephrase.scoped_now(lambda: NOW):
        for owner, rows, _, _ in READING:
            seed_rows(db, owner, rows)
            result = check_taste(LAST_LINE, "", db, owner, owner + ":current", None)
            if owner == "origin missing":
                assert "can't trace" in result.reply and "Friday" not in result.guidance
            else:
                assert ("You said" if owner == "your observation" else "I said") in result.reply
                speaker = "user" if owner == "your observation" else "assistant"
                assert any('"speaker": "' + speaker + '"' in line and "Friday" in line
                           for line in result.guidance.splitlines() if '"sent"' in line)
        owner = READING[0][0]
        seed_rows(db, owner, [(14, "assistant", "On rereading the last line of the clock poem "
                               "I noticed it rhymes with midnight.")])
        def read(question):
            return check_taste(question, "", db, owner, owner + ":current", None)
        assert "Which scene" in read(LAST_LINE).reply
        assert "midnight" in read("What won you over on the clock poem?").guidance
        assert not read("Who noticed the lights were on?").guidance

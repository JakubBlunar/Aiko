"""Paired ordinary-chat replays over fictional dated sessions."""
import argparse
from contextlib import closing, nullcontext
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import requests

from app.core.conversation.practical_answer import PracticalAnswer
from app.core.infra import timephrase
from app.core.infra.chat_database import ChatDatabase
from app.core.session.prompt_assembler import PromptAssembler
from scripts.live_scenarios import require_local_model
from scripts.story_perspective_replay import BASE, ReplayClient


NOW = datetime(2026, 6, 25, 12, tzinfo=timezone.utc)
PRINTER = [(day, "user", "I spent the evening fixing the printer.") for day in range(1, 13)]
CHOIR = [(13, "user", "We chose songs together for my return to choir."),
         (15, "user", "We picked another song together before choir rehearsal.")]
HIGHLIGHT = (16, "user", "Going back to choir was the biggest thing I did for myself this month.")
QUESTION = "What stands out to you about our month?"
MONTH = [
    ("highlight", PRINTER + CHOIR + [HIGHLIGHT], QUESTION, "songs together; choir was user's"),
    ("frequency", PRINTER * 2 + CHOIR + [HIGHLIGHT], "What took up most of our evenings?",
     "printer on twelve distinct chat days; no invented hours"),
    ("different priority", PRINTER + CHOIR + [(16, "user", "Fixing the printer mattered most "
      "to me because I could finish my family album.")], QUESTION, "user valued the family album"),
    ("unstated priority", PRINTER + CHOIR, QUESTION, "may choose songs; user priority open"),
    ("quiet", [], QUESTION, "too little evidence to pick a highlight"),
    ("unrelated", PRINTER, "What stands out about this painting?", "ordinary chat"),
]

CARD = (1, "user", "I found an old postcard. I always date my postcards in the corner.")
OFFER = (1, "assistant", "We could write one line on the back of the postcard about the day.")
TRIED = (8, "user", "I tried your one-line idea on my new postcard. I wrote about the rain.")
BORROWED = (12, "assistant", "I borrowed your way of dating postcards for my little harbor scene.")
START = "How did our postcard thing start?"
INFLUENCE = [
    ("origin", [CARD, OFFER, TRIED, BORROWED], START,
     "user brought postcard; Aiko suggested line; user tried line; Aiko borrowed dating method"),
    ("reversed", [CARD, (3, "user", OFFER[2]), (8, "assistant", TRIED[2])],
     "Whose idea was the one-line postcard?", "user suggested the line"),
    ("proposal only", [CARD, OFFER], "Did I ever try your postcard idea?",
     "Aiko suggested; user uptake unknown"),
    ("named uptake", [CARD, OFFER, (9, "user", "I tried your one-line idea.")],
     "Did I ever try your postcard idea?", "user explicitly reported trying the named idea"),
    ("prior habit", [CARD, OFFER, (8, "user", "I was already writing a line on each postcard "
      "for years before you suggested it.")], "Did I pick up your postcard idea?",
     "user already did this independently"),
    ("other direction", [CARD, OFFER, TRIED, BORROWED], "Did you pick anything up from me?",
     "Aiko attributed her dating method to user"),
    ("one sided", [CARD, OFFER, TRIED], "Did you pick anything up from me?",
     "only user credited Aiko; no reverse credit"),
    ("unrelated uptake", [CARD, OFFER, (8, "user", "I tried your one-line idea for the menu.")],
     "Did I ever try your postcard idea?", "menu uptake does not establish postcard uptake"),
    ("missing", [], START, "origin unknown"),
]


def seed_rows(db, owner, rows):
    for day, role, content in rows:
        mid = db.add_message(f"{owner}:day-{day}", role, content)
        db._get_conn().execute("UPDATE messages SET created_at=? WHERE id=?",
                               ((NOW.replace(day=1) + timedelta(days=day - 1)).isoformat(), mid))
    db._get_conn().commit()


def replay(cases, route, model):
    scratch = Path(__file__).resolve().parents[1] / ".tmp-tests"
    scratch.mkdir(exist_ok=True)
    with requests.Session() as session, TemporaryDirectory(dir=scratch) as directory:
        session.trust_env = False
        require_local_model(session, BASE, model)
        client = ReplayClient(session, model, 67)
        root = Path(directory)
        persona = root / "persona.txt"
        persona.write_text("You are Aiko, a friendly companion.", encoding="utf-8")
        db = ChatDatabase(root / "fiction.db")
        with closing(db._get_conn()), timephrase.scoped_now(lambda: NOW):
            for owner, rows, question, expected in cases:
                seed_rows(db, owner, rows)
                key = owner + ":current"
                mid = db.add_message(key, "user", question)
                for arm in ("baseline", "candidate"):
                    assembler = PromptAssembler(db, persona_path=persona, continuity_owner=owner,
                                                continuity_max_messages=0)
                    baseline = patch("app.core.session.prompt_assembler." + route,
                                     return_value=PracticalAnswer())
                    with baseline if arm == "baseline" else nullcontext():
                        messages, tel = assembler.assemble_with_budget(
                            key, question, current_user_message_id=mid, context_window=8192,
                            response_budget=240)
                    reply = tel.decision_reply or "".join(client.chat_stream(
                        messages, model=model,
                        options={"temperature": 0.2, "num_ctx": 8192, "num_predict": 240}))
                    print(json.dumps(dict(case=owner, arm=arm, expected=expected, reply=reply)),
                          flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default="baytout3/qwen3.5-uncensored:9b")
    parser.add_argument("--section", choices=("month", "influence"), default="month")
    args = parser.parse_args()
    if args.section == "influence":
        replay(INFLUENCE, "check_influence", args.model)
    else:
        replay(MONTH, "check_retrospective", args.model)

"""Compare ordinary chat and practical reasoning on paired fictional user requests."""
import argparse
from contextlib import closing, nullcontext
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import requests

from app.core.infra.chat_database import ChatDatabase
from app.core.conversation.feasibility import PracticalAnswer
from app.core.session.prompt_assembler import PromptAssembler
from scripts.live_scenarios import require_local_model
from scripts.story_perspective_replay import BASE, ReplayClient


FIXED = ("I arrive at 6:00 pm. The screening starts at 6:00 pm sharp and runs for "
         "60 minutes. I have to leave at 6:45 pm.")
ON_DEMAND = ("I arrive at 6:00 pm. The screening starts whenever I arrive and runs for "
             "60 minutes. I have to leave at 6:45 pm.")
FEASIBILITY = (
    ("fixed start", (), FIXED + " I can arrive earlier. Would that let me see the whole thing?",
     "no; the screening finishes at 7 pm"),
    ("on demand", (), ON_DEMAND + " I can arrive earlier. What's the smallest change that works?",
     "arrive at 5:45 pm, 15 minutes earlier"),
    ("flexible finish", (), FIXED + " I can change when I leave. What's the smallest change?",
     "leave at 7 pm, 15 minutes later"),
    ("both fixed", (), ON_DEMAND + " Neither time can move. Can I fit it all in?",
     "no; 15 minutes short; no proposed reschedule"),
    ("exact fit", (), "I can start at 2:10 pm and must finish by 2:55 pm. The recording takes "
     "45 minutes. Do I need to change anything?", "fits exactly"),
    ("follow-up", (ON_DEMAND,), "I can change my arrival. How much earlier would I need to be?",
     "15 minutes earlier at 5:45 pm"),
    ("material", (), "I have 80 cm of ribbon. I need one unbroken length of 90 cm. "
     "I can get a longer piece. What's the smallest change that works?",
     "one continuous piece of at least 90 cm; 10 cm more length; no splicing"),
    ("equipment", (), "The only scanner available accepts sheets up to 30 cm wide. "
     "My original is 34 cm wide and must stay intact. I can use a different scanner. "
     "What's the smallest change that works?", "scanner capacity at least 34 cm; no cutting"),
    ("unrelated", (), "The bus leaves at 6:45 pm. Write a two-line poem about the rain.",
     "poem; no feasibility guidance"),
)


def replay(cases, route_name, seed, model):
    scratch = Path(__file__).resolve().parents[1] / ".tmp-tests"
    scratch.mkdir(exist_ok=True)
    with requests.Session() as session, TemporaryDirectory(dir=scratch) as directory:
        session.trust_env = False
        require_local_model(session, BASE, model)
        client = ReplayClient(session, model, seed)
        root = Path(directory)
        persona = root / "persona.txt"
        persona.write_text("You are a friendly conversational companion.", encoding="utf-8")
        db = ChatDatabase(root / "replay.db")
        with closing(db._get_conn()):
            for case, history, question, expected in cases:
                for prior in history:
                    db.add_message(case, "user", prior)
                    db.add_message(case, "assistant", "Go on.")
                mid = db.add_message(case, "user", question)
                for arm in ("baseline", "candidate"):
                    assembler = PromptAssembler(db, persona_path=persona, continuity_max_messages=0)
                    baseline = patch("app.core.session.prompt_assembler." + route_name,
                                     return_value=PracticalAnswer())
                    with baseline if arm == "baseline" else nullcontext():
                        messages, telemetry = assembler.assemble_with_budget(
                            case, question, current_user_message_id=mid,
                            context_window=8192, response_budget=240,
                        )
                    reply = telemetry.decision_reply or "".join(client.chat_stream(
                        messages, model=model,
                        options={"temperature": 0.2, "num_ctx": 8192, "num_predict": 240},
                    ))
                    print(json.dumps({"case": case, "arm": arm, "expected": expected,
                                      "checked": bool(telemetry.decision_reply),
                                      "reply": reply}, ensure_ascii=True), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=58)
    parser.add_argument("--model", default="baytout3/qwen3.5-uncensored:9b")
    args = parser.parse_args()
    replay(FEASIBILITY, "check_feasibility", args.seed, args.model)


if __name__ == "__main__":
    main()

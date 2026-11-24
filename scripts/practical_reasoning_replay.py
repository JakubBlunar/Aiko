"""Compare ordinary chat and practical reasoning on paired fictional user requests."""
import argparse
from contextlib import closing, nullcontext
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import requests

from app.core.infra.chat_database import ChatDatabase
from app.core.conversation.practical_answer import PracticalAnswer
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


DRAWINGS = "I finished six drawings in ten morning sessions and four in five evening sessions."
BINARY = " Each session produced at most one drawing."
BOTH = " When did I finish more? When was I more likely to finish one?"
OUTCOMES = (
    ("totals", (), DRAWINGS + " When did I finish more drawings in total?",
     "morning six vs evening four; no rate claim"),
    ("opposite winners", (), DRAWINGS + BINARY + BOTH,
     "morning total six vs four; evening successful sessions 4/5 vs 6/10"),
    ("denominator reversal", (), DRAWINGS.replace("five evening", "eight evening") + BINARY + BOTH,
     "morning higher on both: six vs four and 60% vs 50%"),
    ("tie", (), DRAWINGS.replace("ten morning", "nine morning").replace("five evening",
     "six evening") + BINARY + BOTH, "morning total; equal success rates 6/9 and 4/6"),
    ("missing opportunities", (), "I finished six drawings in ten morning sessions and four "
     "drawings in the evenings. I didn't count evening sessions." + BINARY + BOTH,
     "morning total; evening session count needed for comparison with 6/10"),
    ("multiple finishes", (), DRAWINGS + " Some sessions produced several drawings." + BOTH,
     "morning total; need sessions with at least one finish for each group"),
    ("throughput", (), DRAWINGS + " Some sessions produced several drawings. "
     "When did I finish more drawings per session?", "evening 0.8 vs morning 0.6 drawings/session"),
    ("no opportunities", (), "I finished 3 sketches in 6 weekday sessions and 0 in 0 weekend "
     "sessions. When did I finish more sketches per session?",
     "weekday 0.5; weekend undefined, no rate comparison"),
    ("follow-up", (DRAWINGS,), "Which had the higher average number of drawings per session?",
     "evening 0.8 vs morning 0.6"),
    ("known successes", (), "I finished 6 drawings in 10 morning sessions and 4 in 5 evening "
     "sessions. In the mornings 5 sessions had a finish. In the evenings 2 sessions had a "
     "finish. When was I more likely to finish one?", "morning 5/10 vs evening 2/5"),
    ("causal claim", (), "I finished 4 pages in 5 solo sessions and 6 in 10 group sessions. "
     "Does this prove working solo made me faster?", "0.8 vs 0.6 pages/session; no causal proof "
     "or pages per hour without time"),
    ("labelled runs", (), "Indoor: 12 laps in 3 runs. Outdoor: 14 laps in 7 runs. "
     "Which group averaged more laps per run?", "indoor 4 vs outdoor 2 laps/run"),
    ("new topic after counts", (DRAWINGS,), "Which hat would look better with a blue scarf?",
     "ordinary chat; ignore the prior counts"),
    ("unrelated", (), "I had six drawing sessions. Write a title for my sketchbook.",
     "a title; no outcome calculation"),
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
    parser.add_argument("--section", choices=("feasibility", "outcomes"), default="feasibility")
    parser.add_argument("--model", default="baytout3/qwen3.5-uncensored:9b")
    args = parser.parse_args()
    if args.section == "feasibility":
        replay(FEASIBILITY, "check_feasibility", args.seed, args.model)
    else:
        replay(OUTCOMES, "check_outcomes", args.seed, args.model)


if __name__ == "__main__":
    main()

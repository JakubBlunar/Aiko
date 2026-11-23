"""Ordinary chat with a missing decision premise, its answer, and matched controls."""
import argparse
from contextlib import closing
from pathlib import Path
import json
from tempfile import TemporaryDirectory
from unittest.mock import patch

import requests

from app.core.infra import timephrase
from app.core.infra.chat_database import ChatDatabase
from app.core.session.prompt_assembler import PromptAssembler
from app.core.session.turn_runner import TurnRunner
from scripts.live_scenarios import require_local_model


# Expectations apply to the spoken answer; the model sees only the dialogue.
MIX = ("The console can overwrite my mix with a new master. I'd like to try it and I want "
       "a way back. What should I do?")
DRAFT = ("The loom conversion replaces my threading draft. I want to try it but keep a way "
         "back. What would you suggest?")
CASES = {
    "mix": [(MIX, "ask about a restorable copy"),
            ("Yes, I verified a restorable copy of this mix.", "proceed using the known copy")],
    "loom": [(DRAFT, "ask about a restorable copy"),
             ("No, I have just the original.", "preserve original before conversion")],
    "known": [("I verified a restorable copy of my map. The editor can overwrite the map with "
               "the new projection. I want to try it. What would you suggest?", "proceed"),
              ("Correction: the backup belongs to a different map. I still want a way back.",
               "protect this map")],
    "preview": [("The editor previews a new projection without changing the original map. "
                 "I want to see how it looks. Should I try the preview?", "proceed directly")],
    "invites": [("I'm choosing between printed and digital invitations. I'd rather print "
                 "for ten guests or fewer and send digital ones for a larger party. "
                 "Which option should I choose?", "ask how many guests"),
                ("Eight guests.", "choose print")],
    "casual": [("Suggest a name for a red ceramic bowl.", "answer directly")],
}


def main():
    from scripts.companion_pattern_replay import BASE, ReplayModel

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=83)
    parser.add_argument("--model", default="baytout3/qwen3.5-uncensored:9b")
    args = parser.parse_args()
    scratch = Path(__file__).resolve().parents[1] / ".tmp-tests"
    scratch.mkdir(exist_ok=True)
    with requests.Session() as http, TemporaryDirectory(dir=scratch, prefix="choices-") as tmp:
        client = ReplayModel(http, args.model, args.seed)
        require_local_model(http, BASE, args.model)

        def post(url, *, json, **kwargs):
            payload = {**json, "options": {**json.get("options", {}), "seed": args.seed}}
            return client._post_structured_chat(url, json=payload, **kwargs)

        persona = Path(tmp) / "persona.txt"
        persona.write_text("You are Aiko. Answer {user_name} briefly.", encoding="utf-8")
        moment = timephrase.parse_iso("2026-11-22T12:00:00Z")
        with (patch("app.llm.ollama_client.requests.post", post),
              timephrase.scoped_now(lambda: moment)):
            for case, turns in CASES.items():
                db = ChatDatabase(Path(tmp) / f"{case}.db")
                with closing(db._get_conn()):
                    assembler = PromptAssembler(db, persona_path=persona, continuity_max_messages=0)
                    runner = TurnRunner(client, db, assembler, model=args.model,
                                        context_window=8192, max_tokens=300, temperature=0,
                                        filler_enabled=False)
                    for message, expected in turns:
                        result = runner.run(session_key="replay:choice", user_text=message)
                        print(json.dumps({"case": case, "expected": expected, "reply": result.text,
                                          "finish": client.last_usage.done_reason,
                                          "seed": args.seed, "model": args.model}), flush=True)


if __name__ == "__main__":
    main()

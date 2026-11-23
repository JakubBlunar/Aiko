"""Replay explicit response feedback through ordinary chat in disposable local sessions."""

import argparse
from contextlib import closing
from datetime import timedelta
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

import requests

from app.core.conversation.response_feedback import GUIDANCE
from app.core.infra import timephrase
from app.core.infra.chat_database import ChatDatabase
from app.core.session.inner_life_part2 import InnerLifePart2Mixin
from app.core.session.prompt_assembler import PromptAssembler
from app.core.session.turn_runner import TurnRunner
from scripts.companion_pattern_replay import BASE, ReplayModel
from scripts.live_scenarios import require_local_model


HISTORY = (
    ("user", "I'm waiting for my interview and I have butterflies."),
    ("assistant", "Those butterflies are early for their interview. I'm here with you."),
    ("user", "I liked that you joked with me before the interview. I was getting in my head."),
)
CORRECTION = "Actually, what helped was that you stayed with me."
AUDITION = "My audition starts in ten minutes and my stomach is in knots."
AFTER = "My audition is over. I missed my entrance and I'm upset about how it went."
# Human rubric for the visible reply. Only the ordinary conversational input reaches the model.
CASES = (
    ("similar", HISTORY, AUDITION, "humor", "a gentle joke and support"),
    ("different", HISTORY, AFTER, None, "acknowledge disappointment without a joke"),
    ("baseline", (), AUDITION, None, "ordinary response without learned guidance"),
)


class _ReplayHost(InnerLifePart2Mixin):
    session_key = "replay:later"
    user_display_name = "Sam"

    def __init__(self, db):
        self._chat_db = db
        self._settings = SimpleNamespace(agent=SimpleNamespace())


def _trial(runner, host, client, args, name, message, act, rubric):
    result = runner.run(session_key=host.session_key, user_text=message)
    prompt = result.telemetry.system_prompt
    selected = [key for key, guidance in GUIDANCE.items() if guidance in prompt]
    print(json.dumps({"case": name, "expected": rubric, "reply": result.text,
                      "selected": selected, "finish": client.last_usage.done_reason,
                      "seed": args.seed, "model": args.model}), flush=True)
    if selected != ([act] if act else []) or not result.text.strip():
        raise RuntimeError(f"{name}: unexpected prompt selection or empty reply")
    if name == "feedback" and (
        "direct_response_feedback" not in host._last_implicit_need["reasons"] or "?" in result.text
    ):
        raise RuntimeError("The correction needs a simple acknowledgment")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default="baytout3/qwen3.5-uncensored:9b")
    parser.add_argument("--seed", type=int, default=83)
    args = parser.parse_args()
    scratch = Path(__file__).resolve().parents[1] / ".tmp-tests"
    scratch.mkdir(exist_ok=True)
    with requests.Session() as http, TemporaryDirectory(dir=scratch, prefix="feedback-") as tmp:
        client = ReplayModel(http, args.model, args.seed)
        require_local_model(http, BASE, args.model)

        def post(url, *, json, **kwargs):
            payload = {**json, "options": {**json.get("options", {}), "seed": args.seed}}
            return client._post_structured_chat(url, json=payload, **kwargs)

        persona = Path(tmp) / "persona.txt"
        persona.write_text("You are Aiko. Answer {user_name} briefly.", encoding="utf-8")
        moment = timephrase.parse_iso("2026-11-23T12:00:00Z")
        with patch("app.llm.ollama_client.requests.post", post):
            for name, history, message, act, rubric in CASES:
                db = ChatDatabase(Path(tmp) / f"{name}.db")
                with closing(db._get_conn()):
                    with timephrase.scoped_now(lambda: moment - timedelta(days=7)):
                        for role, line in history:
                            db.add_message("replay:interview", role, line)
                    host = _ReplayHost(db)
                    assembler = PromptAssembler(db, persona_path=persona, continuity_max_messages=0)
                    assembler.set_inner_life_providers(implicit_need=host._render_implicit_need_block)
                    runner = TurnRunner(client, db, assembler, model=args.model,
                                        context_window=8192, max_tokens=300, temperature=0,
                                        filler_enabled=False)
                    with timephrase.scoped_now(lambda: moment):
                        _trial(runner, host, client, args, name, message, act, rubric)
                        if name == "similar":
                            _trial(runner, host, client, args, "feedback", CORRECTION, None,
                                   "acknowledge what the user said helped")
                    if name == "similar":
                        host.session_key = "replay:next-audition"
                        with timephrase.scoped_now(lambda: moment + timedelta(days=7)):
                            _trial(runner, host, client, args, "corrected", message, "company",
                                   "offer presence without a joke, advice or pep talk")


if __name__ == "__main__":
    main()

"""Replay first impressions and revisions with a Git snapshot of the persona and fictional chat."""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
from threading import Event

import requests

from app.core.conversation.opinion_read import read_opinion
from app.core.infra.chat_database import ChatDatabase
from app.core.infra import timephrase
from app.core.session.prompt_assembler import PromptAssembler
from scripts.live_scenarios import require_local_model
from scripts.story_perspective_replay import BASE, ReplayClient


FERRY = "The ferry left at six."
CONTEXT = 24576
GULLS = "By six, the gulls had the pier to themselves."
HISTORY = (
    ("user", "We showed the same film at six and nine. Twice as many came at six."),
    ("assistant", "I'd try six again. I think the early start helped draw that crowd."),
)


def openings(first, second, favorite):
    return (f'I wrote two openings: "{first}" and "{second}" '
            f'I like the {favorite}. Which would you keep, and why?')


CASES = (
    ("direct favorite", (), openings(FERRY, GULLS, "first"), "reason fits either actual line"),
    ("image favorite", (), openings(FERRY, GULLS, "second"), "same material; own reading"),
    ("order swapped", (), openings(GULLS, FERRY, "second"), "reason follows the chosen wording"),
    ("image removed", (), openings(FERRY, "The six o'clock ferry departed on time.", "first"),
     "reason grounded in new wording"),
    ("unseen drafts", (), "I wrote two openings. I like draft one. Which would you keep?",
     "ask to read them"),
    ("different medium", (), "Two names for my citrus soda: Pip or Orchard Hour. I like Pip. "
     "Which do you like?", "a present response to the sound or associations of either name"),
    ("price differed", HISTORY, "Oh, only the six o'clock screening was free. Do you still "
     "think six draws more people?", "price weakens the inference; no opposite conclusion"),
    ("same price", HISTORY, "Oh, both screenings were free. Do you still think six draws more "
     "people?", "price cannot explain the difference; single comparison remains limited"),
    ("personal preference", HISTORY, "I prefer late films. Do you still think six draws more "
     "people?", "personal preference alone doesn't change crowd evidence"),
    ("shared plan", HISTORY, "I prefer late films. Which would you pick for just us?",
     "can accommodate preference independently of turnout"),
    ("different occasion", (("user", "The plum jam sold out; the pear barely sold."),
                            ("assistant", "I'd stock more plum. It seems more popular.")),
     "I forgot to mention only the plum had free samples. Which flavor is more popular?",
     "sampling makes underlying popularity uncertain"),
)


class OpinionReplayClient(ReplayClient):
    raw_read = ""

    def chat_stream(self, *args, format_json=False, **kwargs):
        for reply in super().chat_stream(*args, format_json=format_json, **kwargs):
            if format_json:
                self.raw_read = reply
            yield reply


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=71)
    parser.add_argument("--persona-ref", default="HEAD", help="Git revision or INDEX")
    parser.add_argument("--without-read", action="store_true", help="Compare guidance alone")
    parser.add_argument("--case", action="append", choices=[c[0].replace(" ", "-") for c in CASES])
    args = parser.parse_args()
    model = "baytout3/qwen3.5-uncensored:9b"
    root = Path(__file__).resolve().parents[1]
    scratch = root / ".tmp-tests"
    scratch.mkdir(exist_ok=True)
    with requests.Session() as session, TemporaryDirectory(dir=scratch) as tmp:
        client = OpinionReplayClient(session, model, args.seed)
        require_local_model(session, BASE, model)
        # Use the repository template; someone's installed, edited persona is private.
        persona = Path(tmp) / "persona.txt"
        revision = "" if args.persona_ref == "INDEX" else args.persona_ref
        persona.write_bytes(subprocess.check_output(
            ["git", "cat-file", "blob", f"{revision}:data/persona/aiko_companion.txt"], cwd=root))
        for index, (case, history, question, expected) in enumerate(CASES):
            if args.case and case.replace(" ", "-") not in args.case:
                continue
            client.raw_read = ""
            db = ChatDatabase(Path(tmp) / f"{index}.db")
            with closing(db._get_conn()), timephrase.scoped_now(
                lambda: datetime(2026, 11, 28, 12, tzinfo=timezone.utc)
            ):
                for role, content in history:
                    db.add_message("fiction", role, content)
                mid = db.add_message("fiction", "user", question)
                assembler = PromptAssembler(db, persona_path=persona, continuity_max_messages=0)
                assembler.set_user_display_name_provider(lambda: "Mira")
                messages, tel = assembler.assemble_with_budget(
                    "fiction", question, current_user_message_id=mid,
                    context_window=CONTEXT, response_budget=240)
                for role, content in (*history, ("user", question)):
                    if not any(m["role"] == role and content in m["content"] for m in messages):
                        raise RuntimeError(f"{case}: missing {role} turn from assembled prompt")
                options = {"temperature": 0.2, "num_ctx": CONTEXT, "num_predict": 240}
                reading = ""
                if tel.opinion_guidance and not args.without_read and not tel.decision_reply:
                    reading = read_opinion(client, messages, tel.opinion_guidance, model=model,
                                           options=options,
                                           stop_event=Event())
                reply = tel.decision_reply or reading or "".join(client.chat_stream(
                    messages, model=model, options=options))
                finish = "direct" if tel.decision_reply else client.last_usage.done_reason
                print(json.dumps(dict(case=case, expected=expected, raw_read=client.raw_read,
                                      admitted=bool(reading), reply=reply,
                                      finish=finish)), flush=True)


if __name__ == "__main__":
    main()

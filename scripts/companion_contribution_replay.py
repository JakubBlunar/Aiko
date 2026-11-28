"""Fictional chat pairs for a possible clue, quiet preparation, and a new joke."""
import argparse
from contextlib import closing, ExitStack
import json
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
from threading import Event
from types import SimpleNamespace
from unittest.mock import patch

import requests

from app.core.conversation.practical_answer import PracticalAnswer
from app.core.conversation.opinion_read import read_opinion
from app.core.conversation.clarification_reply import CURRENT_REPLY_GUIDANCE
from app.core.infra import timephrase
from app.core.infra.chat_database import ChatDatabase
from app.core.proactive.cue_store import CueStore
from app.core.session.cue_pool_mixin import CuePoolMixin
from app.core.session.inner_life_part3 import InnerLifePart3Mixin
from app.core.session.prompt_assembler import PromptAssembler
from scripts.live_scenarios import require_local_model
from scripts.longitudinal_reasoning_replay import NOW, seed_rows
from scripts.story_perspective_replay import BASE, ReplayClient


BANNER = ("I finished lettering the birthday banner! I started with the middle letter "
          "and worked outwards. Otherwise I always run out of room.")
SIGN = "Why was the lettering on the cafe sign cramped at the right edge?"
FADED = "Why had the ink on the cafe sign faded to blue?"
TRIM = "The lettering on the cafe sign was cramped because the maker trimmed the right edge."
SUPPER = "Everyone found the room at the community supper. I barely did anything tonight."
SIGN_WORK = "I labelled the side door before the community supper so people could find the room."
PLAN = "I thought about labelling the side door before the community supper to help people find it."
OTHER = "My neighbour labelled the side door for the community supper so people could find it."
SILENCE = "The printer has elected to observe a minute of silence for every page."
TOLL = "The printer charges a toll of one blank page per document."
DELIVERY = "I could get my Saturday groceries delivered. That would save an hour."
ERRAND = "Walking home from the shop with my brother on Saturdays is the only time we get to talk."
WEEKEND = "Sunday's walk with my brother is the only time we get to talk."
BREAKFAST = "We've started doing breakfast instead."
WALK_ASK = "Would you still get that walk home with your brother?"

# Each arm gets an independent database. Earlier-day records are outside the current session.
CASES = {
    "shortcut": [
        ("shared errand", "", [ERRAND], [], DELIVERY, "optional question about the walk"),
        ("independent walk", "", [WEEKEND], [], DELIVERY, "leave the separate walk alone"),
        ("other errand", "", [ERRAND.replace("Saturdays", "Sundays")], [], DELIVERY,
         "a different day's shopping trip is separate"),
        ("breakfast", "", [ERRAND], [("user", DELIVERY), ("assistant", WALK_ASK)], BREAKFAST,
         "take breakfast as the current way they meet; release the concern"),
        ("named breakfast", "", [ERRAND], [("user", DELIVERY), ("assistant", WALK_ASK)],
         "We've started doing Sunday breakfast instead.", "respect the new weekday"),
        ("practice", "", ["Going to choir practice is when my sister and I get to catch up."],
         [], "I might do choir practice online. That would save some time.",
         "may wonder whether online practice changes the catch-up"),
    ],
    "clue": [
        ("layout", SIGN, [], [], BANNER, "banner first; optional tentative layout connection"),
        ("invited", SIGN, [], [], BANNER + " Does that remind you of anything we've seen?",
         "room to voice a possible connection; the original cause is still unknown"),
        ("ink", FADED, [], [], BANNER, "stay with banner; centering doesn't explain fading"),
        ("answered", SIGN, [], [], BANNER, "answered seed stays closed"),
    ],
    "preparation": [
        ("done", "", [SIGN_WORK], [], SUPPER, "may credit preparing sign; effect unknown"),
        ("planned", "", [PLAN], [], SUPPER, "no credit for putting up a sign"),
        ("neighbour", "", [OTHER], [], SUPPER, "no credit to speaker for neighbour's work"),
        ("new route", "", [SIGN_WORK], [("user", SUPPER),
         ("assistant", "You put some work into the sign before the supper.")],
         "Turns out everyone came through the kitchen.", "let route explanation go"),
    ],
    "joke": [
        ("silence", "", [], [], SILENCE, "optional consequence of a silence for every page"),
        ("toll", "", [], [], TOLL, "optional consequence of charging per document"),
        ("serious", "", [], [("user", TOLL), ("assistant", "An expensive way to print receipts.")],
         "Seriously, how do I clear the print queue on Windows?", "practical answer"),
        ("literal", "", [], [], "My printer takes a minute per page. How can I speed it up?",
         "practical reading"),
    ],
}


class QuietHost(CuePoolMixin):
    _settings = SimpleNamespace(agent=SimpleNamespace())

    def __init__(self, store):
        self._cue_store = store
        self._cue_pool_listeners = []

    def _question_balance_suppressed(self):
        return False

    render = InnerLifePart3Mixin._render_curiosity_seeds_block


def run_case(db, persona, case, group, arm, client, model):
    name, question, past, history, text, expected = case
    key = "mira:here"
    host = QuietHost(CueStore(db))
    if question:
        host._cue_store.add("curiosity_seed", "cafe sign", question,
                            payload={"about": "subject", "prompt_text": question})
        if name == "answered":
            host._settle_pool_cues(user_text=TRIM, assistant_text="Ah, that explains it.")
    seed_rows(db, "mira", [(2, "user", source) for source in past])
    for role, content in history:
        db.add_message(key, role, content)
    mid = db.add_message(key, "user", text)
    assembler = PromptAssembler(db, persona_path=persona, continuity_owner="mira",
                                continuity_max_messages=0)
    assembler.set_user_display_name_provider(lambda: "Mira")
    if question:
        assembler.set_inner_life_providers(curiosity_seeds=host.render)
    with ExitStack() as changes:
        if arm == "baseline" and group == "shortcut":
            changes.enter_context(patch("app.core.session.prompt_assembler.check_shortcut",
                                        return_value=PracticalAnswer()))
        elif arm == "baseline":
            changes.enter_context(patch("app.core.session.prompt_assembler.check_preparation",
                                        return_value=PracticalAnswer()))
            changes.enter_context(patch("app.core.session.prompt_assembler.ordinary_reply_guidance",
                                        return_value=CURRENT_REPLY_GUIDANCE))
        messages, tel = assembler.assemble_with_budget(
            key, text, current_user_message_id=mid, context_window=24576, response_budget=280)
    assert not tel.decision_reply
    prompt = messages[0]["content"]
    offered = "Quiet curiosity:" in prompt
    if question:
        assert offered == (name != "answered")
    preparation_offered = "This excerpt is something they already did" in prompt
    if group == "preparation":
        assert preparation_offered == (arm == "candidate" and name == "done")
    if group == "shortcut":
        assert ("Their earlier words describe" in prompt) == (
            arm == "candidate" and name in {"shared errand", "practice"})
        assert ("let the old concern go" in prompt) == (
            arm == "candidate" and name in {"breakfast", "named breakfast"})
    options = {"num_ctx": 24576, "num_predict": 280, "temperature": 0.2}
    reading = read_opinion(client, messages, tel.opinion_guidance, model=model, options=options,
                           stop_event=Event()) if tel.opinion_guidance else ""
    reply = reading or "".join(client.chat_stream(messages, model=model, options=options))
    if question:
        host._settle_pool_cues(user_text=text, assistant_text=reply)
    return dict(group=group, case=name, arm=arm, expected=expected, reply=reply,
                finish=client.last_usage.done_reason, opinion_read=bool(reading),
                curiosity_offered=offered,
                preparation_offered=preparation_offered,
                cue_state=host._cue_store.list_for_user()[0].state if question else None)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--section", choices=CASES)
    parser.add_argument("--candidate", default="HEAD", help="Git revision or INDEX")
    parser.add_argument("--baseline", default="a757c8dc")
    parser.add_argument("--arm", choices=("baseline", "candidate"))
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    scratch = root / ".tmp-tests"
    scratch.mkdir(exist_ok=True)
    model = "baytout3/qwen3.5-uncensored:9b"
    with requests.Session() as session, TemporaryDirectory(dir=scratch) as directory:
        session.trust_env = False
        require_local_model(session, BASE, model)
        client = ReplayClient(session, model, 67)
        for arm, ref in (("baseline", args.baseline), ("candidate", args.candidate)):
            if args.arm and arm != args.arm:
                continue
            target = Path(directory) / arm
            target.mkdir()
            for filename in ("aiko_companion.txt", "conditional_handling.txt"):
                revision = "" if ref == "INDEX" else ref
                (target / filename).write_bytes(subprocess.check_output(
                    ["git", "cat-file", "blob", f"{revision}:data/persona/{filename}"], cwd=root))
            for group, cases in CASES.items():
                if args.section and group != args.section:
                    continue
                for case in cases:
                    db = ChatDatabase(target / f"{case[0]}.db")
                    with closing(db._get_conn()), timephrase.scoped_now(lambda: NOW):
                        result = run_case(db, target / "aiko_companion.txt", case,
                                          group, arm, client, model)
                        print(json.dumps(result), flush=True)


if __name__ == "__main__":
    main()

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


BOOK_CLUB = "For book club, should we vote on books or take turns picking them?"
BOOK_REASON = "I like taking turns. Once in a while we get the book only one person would bring."
VOTE = "I prefer voting because everyone gets a say in every book we read."
TURNOUT = "I prefer voting because our reluctant readers come when a book they voted for wins."
MAP_CLAIM = "I think every book with a journey in it should have a map. What do you reckon?"
MAP_HISTORY = (("user", MAP_CLAIM), ("assistant", "I'd want one for a six-city chase. "
               "If the twist is that she's walked in a circle, the map could give it away."))
TIDE = "On my maps, a dotted path means you can only cross at low tide."
CAUSEWAY = (("user", TIDE), ("assistant", "I'd draw the old causeway dotted. Have I got that?"))


CASES = (
    ("local convention", (), TIDE, "may try a hypothetical crossing on this person's maps"),
    ("dry causeway", CAUSEWAY, "Actually that causeway stays dry. Try a different example.",
     "use a different crossing and retain the low-tide condition"),
    ("accepted causeway", CAUSEWAY, "Yes, exactly. Try another example.",
     "apply the same convention to a fresh example"),
    ("legend request", (), TIDE + " Write a one-line legend for it.", "give just the legend"),
    ("recipe convention", (), "In my recipe cards, a star means the dough needs an overnight rest.",
     "may try a hypothetical recipe satisfying that condition"),
    ("notebook convention", (), "In my notebooks, a hollow circle means an observation "
     "still needs a second measurement.",
     "apply the circle to an imagined observation without claiming to have made one"),
    ("map claim", (), MAP_CLAIM, "one concrete case can illuminate her view"),
    ("map preference", (), "I love looking at maps after I finish a book. What do you reckon?",
     "a post-reading pleasure leaves surprises intact"),
    ("closed foldout", MAP_HISTORY, "Oh, a foldout you could leave closed!",
     "recognize that the reader can choose when to look"),
    ("bigger map", MAP_HISTORY, "What about a bigger map?",
     "size alone does not address revealing the route"),
    ("changed subject", MAP_HISTORY, "How do I fold a paper crane?",
     "answer the new practical request"),
    ("museum labels", (), "I think all museum labels should name the artist up front. "
     "What's your take?", "a concrete exhibit can make her reason intelligible"),
    ("minority book", (("user", BOOK_CLUB), ("assistant", VOTE)), BOOK_REASON,
     "weigh finding an overlooked book against a say for everyone"),
    ("reluctant readers", (("user", BOOK_CLUB), ("assistant", TURNOUT)), BOOK_REASON,
     "weigh discovery against attendance"),
    ("social pressure", (("user", BOOK_CLUB), ("assistant", VOTE)),
     "Come on, just admit taking turns picking books is better.", "pressure adds no reason"),
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

"""Compare a fresh writing occasion and later self-report with a fictional practice history."""
from contextlib import closing
from datetime import timedelta
import json
from pathlib import Path
from tempfile import TemporaryDirectory

import requests

from app.core.infra.chat_database import ChatDatabase
from app.core.session.prompt_assembler import PromptAssembler
from app.core.world import idle_creations, writing_practice
from app.core.world.world_store import WorldStore
from scripts.live_scenarios import require_local_model
from scripts.longitudinal_reasoning_replay import NOW
from scripts.story_perspective_replay import BASE, ReplayClient


FIRST = "She folded the empty envelope. She knew that absence could weigh as much as paper."
REVISION = "She turned the envelope over. A pale stamp clung to its corner."
IMAGE = "Two blue cups stood on the table."
LATER = "The latch rattled and fell still. " + IMAGE
EXPLAIN = " She understood at last how lonely the house had become."
REVIEW = "What have you been getting better at?"
FRESH = "Write me a short scene about someone closing a seaside kiosk for winter. Three sentences."
CASES = (
    ("revision alone", "", REVIEW, "quote revised stamp; separate later work unknown"),
    ("later original", LATER, REVIEW, "cups end a separate piece"),
    ("old habit", LATER + EXPLAIN, REVIEW, "image followed by explanation"),
    ("fresh writing", "", FRESH, "inspect actual last line for concrete image, no gloss"),
    ("requested style", "", FRESH + " End by explaining the character's feelings directly.",
     "direct explanation takes precedence"),
)


def seed_work(store, later=""):
    def save(title, text, day, **fields):
        draft = idle_creations.parse_draft(dict(title=title, form="short_scene", text=text,
                                                **fields), store.list_items())
        return idle_creations.save_draft(store, draft, location_id=None,
                                         now=NOW + timedelta(days=day))

    source = save("Envelope", FIRST, 0, practice_aim=writing_practice.AIM)
    item_id = source["item_id"]
    ref = writing_practice.reference(item_id, idle_creations.revisions(store.get_item(item_id))[0])
    save("Envelope", REVISION, 1, item_id=item_id, base_revision=1, practice_ref=ref,
         image_passage="A pale stamp clung to its corner.")
    if later:
        return save("Cups", later, 3, practice_ref=ref, image_passage=IMAGE)


def main():
    scratch = Path(__file__).resolve().parents[1] / ".tmp-tests"
    scratch.mkdir(exist_ok=True)
    model = "baytout3/qwen3.5-uncensored:9b"
    with requests.Session() as session, TemporaryDirectory(dir=scratch) as directory:
        session.trust_env = False
        require_local_model(session, BASE, model)
        client = ReplayClient(session, model, 68)
        root = Path(directory)
        persona = root / "persona.txt"
        persona.write_text("You are Aiko, a friendly companion.", encoding="utf-8")
        for case, later, question, expected in CASES:
            path = root / (case + ".db")
            db = ChatDatabase(path)
            with closing(db._get_conn()), closing(WorldStore(path)) as store:
                seed_work(store, later)
                for arm in ("baseline", "candidate"):
                    assembler = PromptAssembler(db, persona_path=persona)
                    if arm == "candidate":
                        assembler.set_inner_life_providers(writing_items=store.list_items)
                    messages, tel = assembler.assemble_with_budget(
                        "fiction:current", question, context_window=8192, response_budget=240)
                    reply = tel.decision_reply or "".join(client.chat_stream(
                        messages, options={"temperature": .2, "num_ctx": 8192, "num_predict": 240},
                        model=model))
                    done = "recorded" if tel.decision_reply else client.last_usage.done_reason
                    print(json.dumps(dict(case=case, arm=arm, expected=expected, reply=reply,
                                          done=done)), flush=True)


if __name__ == "__main__":
    main()

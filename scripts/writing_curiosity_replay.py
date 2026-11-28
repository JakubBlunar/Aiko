"""Compare the same fictional scene with a changed image, a changed sound, or just a plan."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import requests

from app.core.proactive.curiosity_seed_worker import CuriositySeedWorker
from app.core.session.inner_life_part3 import InnerLifePart3Mixin
from scripts.live_scenarios import require_local_model
from scripts.opinion_replay import OpinionReplayClient
from scripts.story_perspective_replay import BASE


SOUND = "A pair of footsteps echoed down the empty station."
CUP = "A second cup sat on the empty bench."
LOUD = "The footsteps boomed off the station walls."


def station_item(second):
    revisions = []
    for index, passage in enumerate([SOUND, second] if second else [SOUND], 1):
        revisions.append(dict(author="aiko", mode="creative_fiction", revision=index,
                              at=f"2026-06-0{index}T12:00:00Z", title="Empty Station",
                              text=passage,
                              text_sha256=hashlib.sha256(passage.encode()).hexdigest()))
    return SimpleNamespace(id=17, state=dict(creation_schema=1, revisions=revisions,
                                             plan="Maybe try a second cup."))


def replies(client, model, seed):
    row = SimpleNamespace(subject=seed["topic"], payload={
        **seed, "quality": {"drafts": seed["drafts"], "useful_because": seed["useful_because"]}})
    offers = iter((row, None))
    host = SimpleNamespace(_settings=SimpleNamespace(agent=SimpleNamespace()),
                           _question_balance_suppressed=lambda: False,
                           take_pool_cue=lambda *args, **kwargs: next(offers))
    block = InnerLifePart3Mixin._render_curiosity_seeds_block(host)
    path = Path(__file__).resolve().parents[1] / "data/persona/conditional_handling.txt"
    sections = path.read_text(encoding="utf-8")
    handling = sections.split("Quiet curiosity:\n", 1)[1].split("\n\n", 1)[0]
    for case, text in (
        ("lingering", "I loved how crowded that empty room felt in the film. A single glass "
         "was enough to make me feel someone had just left."),
        ("practical", "The programme lists the film at 18:15. What time does it start?"),
    ):
        messages = [{"role": "system", "content": "You are Aiko, a friendly companion.\n" +
                     block + "\n" + handling}, {"role": "user", "content": text}]
        reply = "".join(client.chat_stream(messages, model=model, options={
            "num_ctx": 8192, "num_predict": 160, "temperature": 0.2}))
        print(json.dumps({"case": case, "reply": reply}), flush=True)


def main():
    model = "baytout3/qwen3.5-uncensored:9b"
    with requests.Session() as session:
        session.trust_env = False
        require_local_model(session, BASE, model)
        client = OpinionReplayClient(session, model, 67)
        for name, second in (("object", CUP), ("sound", LOUD), ("plan", None)):
            item = station_item(second)
            worker = CuriositySeedWorker(
                cue_store_provider=lambda: None, topic_graph=None, embedder=None,
                ollama=client, chat_model=model, cancel_event=Event(),
                agent_settings=SimpleNamespace(), memory_settings=SimpleNamespace(),
                clock=lambda: datetime(2026, 6, 4, 12, tzinfo=timezone.utc),
                writing_items_provider=lambda item=item: [item],
                assistant_display_name_provider=lambda: "Aiko",
                user_display_name_provider=lambda: "Mira")
            client.raw_read = ""
            seeds = worker._call_llm(persona_text="", summary_text="", cluster_text="",
                                     active_text="")
            print(json.dumps({"case": name, "seeds": seeds, "raw": client.raw_read}), flush=True)
            if name == "object" and seeds:
                replies(client, model, seeds[0])


if __name__ == "__main__":
    main()

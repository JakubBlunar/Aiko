"""Compare ordinary reply guidance on fictional stories using an installed loopback model."""
import argparse
from contextlib import closing
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from types import SimpleNamespace

import requests

from app.core.infra.chat_database import ChatDatabase
from app.core.infra.settings import OllamaSettings
from app.core.conversation.conversation_situation import ConversationSituationStore, StoryFrame
from app.core.conversation.conversation_situation_worker import ConversationSituationWorker
from app.core.conversation.story_reply import read_story
from app.core.session.conversation_situation_mixin import ConversationSituationMixin
from app.core.session.prompt_assembler import PromptAssembler
from scripts.live_scenarios import LoopbackPolicyClient, require_local_model


BASE = "http://127.0.0.1:11434"
MEETING = ("I told Lina we'd meet at the pier. After she left I moved the meeting to the "
           "bakery and told Tomas.")
NOTE = ("Ravi and Tess saw me put the tickets in the blue tin. After Ravi went home, "
        "Tess watched me move them to the desk.")
CASES = (
    # Each tuple gives an independent history, newest turn, and expected meaning.
    ("different recipients", (MEETING,), "Where is Lina likely to go?", "pier"),
    ("planned relay", (MEETING,), "Tomas meant to call Lina. Where will she head?",
     "probably pier; the call is unconfirmed"),
    ("acknowledged relay", (MEETING,), "Lina called Tomas back and repeated the new place. "
     "Where is she expecting us?", "bakery"),
    ("unclear receipt", (MEETING,), "Tomas sent the update but can't see if she read it. "
     "Does Lina know?", "receipt unknown; neither certain ignorance nor certain knowledge"),
    ("world fact", (MEETING,), "What is the actual meeting place?", "bakery"),
    ("witness", (NOTE,), "Where would Tess look first?", "desk"),
    ("absent witness", (NOTE,), "Where would Ravi look first?", "blue tin"),
    ("earlier viewpoint", (NOTE,), "Ravi found them on the desk the next day. "
     "Before opening the door that morning, where did he expect them to be?", "blue tin"),
    ("narrative misunderstanding", (), "I told Lark to bring the book to the studio. "
     "After Lark left, I switched to the hall and only told Noa. "
     "Lark's still waiting in the studio. I guess she just ignored me.",
     "recognize Lark followed the instruction she received; avoid attributing defiance"),
    ("correction", (MEETING,), "Wait, the update went to Lina too. I don't remember "
     "telling Tomas about either place. Where would Tomas be expecting to meet us?",
     "Tomas's original instructions are unknown"),
    ("ordinary pivot", (NOTE,), "Different topic. What's an anagram of listen?", "silent etc."),
    ("held-out recipient", ("Elsa and Niamh were both cast as understudies. I told Niamh "
                            "the rehearsal was Friday. Only Elsa heard me move it to Sunday.",),
     "Niamh repeated Sunday back to Elsa on their walk home. Which day is Niamh expecting?",
     "Sunday"),
    ("held-out missed update", ("Elsa and Niamh were both cast as understudies. I told Niamh "
                                "the rehearsal was Friday. Only Elsa heard me move it to Sunday.",),
     "Elsa planned to pass that along on their walk home. Which day is Niamh expecting?",
     "probably Friday"),
    ("genuine ambiguity", ("Ada picked the green badge. Pia picked the orange badge.",),
     "Which badge did she pick?", "ask which person or distinguish the two possibilities"),
    ("acknowledged change and blame", (), "I originally asked Joel to wait outside. After he "
     "left, I told Eva to meet indoors. Joel later called and repeated the indoor plan back "
     "to me. He still waited outside. Was he ignoring me on purpose?",
     "Joel knew about the change; motive is unknown"),
    ("unseen shared joke", ("Oren asked me why the crew always says 'apricot'. I promised "
                            "to explain the joke. Only Bea heard the explanation backstage.",),
     "Oren laughed when Bea said 'apricot'. Does that show Oren knows the story behind it?",
     "laughter alone leaves Oren's knowledge open"),
)


class ReplayClient(LoopbackPolicyClient):
    """Use the checked loopback session for both structured and ordinary replies."""

    def __init__(self, session, model, seed):
        super().__init__(OllamaSettings(BASE, chat_model=model, context_window=8192), BASE, session)
        self.seed = seed

    def chat_json(self, messages, *, options=None, **kwargs):
        return super().chat_json(messages, options={**(options or {}), "seed": self.seed}, **kwargs)

    def chat_stream(self, messages, *, options, model, format_json=False, **kwargs):
        payload = {"model": model, "messages": messages, "stream": False, "think": False,
                   "options": {**options, "seed": self.seed}}
        if format_json:
            payload["format"] = "json"
        with self._post_structured_chat(f"{BASE}/api/chat", json=payload, timeout=120) as response:
            response.raise_for_status()
            result = response.json()
        self.last_usage = SimpleNamespace(done_reason=result.get("done_reason"))
        if format_json:
            try:
                reading = json.loads(result["message"]["content"])
                self.selection = {k: reading[k] for k in ("person", "evidence", "status")
                                  if k in reading} if isinstance(reading, dict) else {}
            except ValueError:
                self.selection = {}
        yield result["message"]["content"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=59)
    parser.add_argument("--case", action="append", choices=[case[0] for case in CASES])
    parser.add_argument("--model", default="baytout3/qwen3.5-uncensored:9b")
    parser.add_argument("--baseline", action="store_true",
                        help="Use ordinary history without the story reading")
    parser.add_argument("--observer", action="store_true",
                        help="Run the production situation observer on each story prefix")
    args = parser.parse_args()
    scratch = Path(__file__).resolve().parents[1] / ".tmp-tests"
    scratch.mkdir(exist_ok=True)
    with requests.Session() as session, TemporaryDirectory(dir=scratch) as directory:
        session.trust_env = False
        require_local_model(session, BASE, args.model)
        if args.baseline and args.observer:
            parser.error("choose either the baseline or observer arm")
        client = ReplayClient(session, args.model, args.seed)
        root = Path(directory)
        persona = root / "persona.txt"
        persona.write_text("You are a friendly conversational companion.", encoding="utf-8")
        db = ChatDatabase(root / "stories.db")
        with closing(db._get_conn()):
            for case, history, question, expected in CASES:
                if args.case and case not in args.case:
                    continue
                assembler = PromptAssembler(db, persona_path=persona, continuity_max_messages=0)
                for turn in history:
                    db.add_message(case, "user", turn)
                    db.add_message(case, "assistant", "Go on.")
                observed = None
                store = ConversationSituationStore(db)
                if args.observer and history:
                    worker = ConversationSituationWorker(
                        client=client, chat_db=db, store=store, model=args.model,
                        world_snapshot_provider=lambda: {}, judgment_shadow_enabled=False,
                    )
                    state = worker.run(case)
                    observed = bool(state and isinstance(state.pragmatic_frame, StoryFrame))
                if not args.baseline:
                    host = ConversationSituationMixin()
                    host.session_key = case
                    host._chat_db = db
                    host._conversation_situation_store = store
                    host._settings = SimpleNamespace(agent=SimpleNamespace())
                    assembler.set_inner_life_providers(
                        conversation_situation=host._render_conversation_situation_block)
                current = db.add_message(case, "user", question)
                messages, telemetry = assembler.assemble_with_budget(
                    case, question, current_user_message_id=current,
                    context_window=8192, response_budget=240,
                )
                options = {"temperature": 0.2, "num_ctx": 8192, "num_predict": 240}
                reply = read_story(client, messages, model=args.model, options=options,
                                   stop_event=Event()) if telemetry.story_read else ""
                admitted = bool(reply)
                if not reply:
                    reply = "".join(client.chat_stream(messages, options=options, model=args.model))
                print(json.dumps({"case": case, "expected": expected,
                                  "observed": observed,
                                  "projected": "Earlier narrated events" in telemetry.system_prompt,
                                  "requested": telemetry.story_read, "admitted": admitted,
                                  "selection": client.selection if telemetry.story_read else None,
                                  "reply": reply, "done": client.last_usage.done_reason}),
                      flush=True)


if __name__ == "__main__":
    main()

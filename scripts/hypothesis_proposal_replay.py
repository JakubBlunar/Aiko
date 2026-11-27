"""Replay grounded hunches and abstentions with fictional inputs on numeric loopback."""
from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from types import SimpleNamespace

import requests

from app.core.concepts.hypothesis_store import HypothesisStore
from app.core.infra.chat_database import ChatDatabase
from app.core.infra import timephrase
from app.core.infra.settings import OllamaSettings
from app.core.proactive.hypothesis_proposer_worker import HypothesisProposerWorker
from scripts.companion_pattern_replay import BASE, LocalEmbeddings
from scripts.live_scenarios import LoopbackPolicyClient, require_local_model


class LocalProposerClient(LoopbackPolicyClient):
    def __init__(self, session, model):
        settings = OllamaSettings(BASE, chat_model=model, context_window=8192,
                                  think_num_predict_headroom=2048)
        super().__init__(settings, BASE, session)
        self.responses = []

    def chat_stream(self, messages, *, stop_event, options, **kwargs):
        # Buffer the local response to capture its own completion status and raw proposal.
        if not stop_event.is_set():
            raw, usage = self.chat_json(messages, options={**options, "seed": 71}, **kwargs)
            self.last_usage = usage  # Match the production streaming client's final chunk.
            self.responses.append((raw, usage.done_reason))
            yield raw


def main():
    # Positive: a choice between drafts changes with viewing distance. What the
    # distance reveals is open. Control: the maker has already explained the choice.
    observation = ("Mira liked the thin letters at her desk. From the doorway she "
                   "picked the thick letters for the sign.")
    explained = observation + (" Mira explained that the thin strokes disappeared at "
                               "that distance and the thick strokes stayed legible.")
    song = ("self", "Aiko listened to two takes of the same song. In the second take she "
                    "replayed the pause before the chorus twice.")
    cases = {
        "open": [("fact", observation)],
        "explained": [("fact", explained)],
        "thin": [("fact", "Mira collected a parcel from the post office.")],
        "self": [song],
        "mixed": [("fact", explained), song],
    }
    moment = datetime(2026, 11, 27, 12, tzinfo=timezone.utc)
    model = "baytout3/qwen3.5-uncensored:9b"
    embedding = "qwen3-embedding:0.6b"
    scratch = Path(__file__).resolve().parents[1] / ".tmp-tests"
    scratch.mkdir(exist_ok=True)
    with requests.Session() as session, TemporaryDirectory(dir=scratch) as tmp:
        client = LocalProposerClient(session, model)
        for name in (model, embedding):
            require_local_model(session, BASE, name)
        with timephrase.scoped_now(lambda: moment):
            for case, records in cases.items():
                db = ChatDatabase(Path(tmp) / f"{case}.db")
                with closing(db._get_conn()):
                    hypotheses = HypothesisStore(db)
                    notes = [SimpleNamespace(id=i, kind=kind, metadata={}, content=content,
                                             created_at=moment.isoformat())
                             for i, (kind, content) in enumerate(records, start=1)]
                    memories = SimpleNamespace(list_recent=lambda rows=notes, **_: rows)
                    worker = HypothesisProposerWorker(
                        hypothesis_store_provider=lambda db_store=hypotheses: db_store,
                        concept_store_provider=lambda: None,
                        memory_store_provider=lambda rows=memories: rows,
                        user_display_name_provider=lambda: "Mira", chat_model=model, ollama=client,
                        embedder=LocalEmbeddings(session, embedding), cancel_event=Event(),
                        context_window=lambda: 8192,
                    )
                    start = len(client.responses)
                    result = worker.run()
                    rows = hypotheses.list_by(live=True)
                    second = worker.run() if not rows else {}
                    print(json.dumps({"case": case, "first": result, "retry": second,
                                      "calls": len(client.responses) - start,
                                      "responses": client.responses[start:],
                                      "stored": [(r.statement, r.quality) for r in rows]}),
                          flush=True)


if __name__ == "__main__":
    main()

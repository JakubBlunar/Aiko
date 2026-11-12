"""Real citations can still assert an unsupported relation across worker lanes."""
import json
from types import SimpleNamespace

import pytest

from app.core.conversation.digest_fidelity import parse_claims
from app.core.infra import timephrase
from app.core.memory.memory_admission import admit_memory
from app.core.memory.promise_worker import PromiseExtractionWorker
from app.core.relationship.moment_evidence import admit as admit_moment


@pytest.mark.parametrize("source,claim,quote,expected", [
    ("You should bring headphones to the waiting room.",
     "Noah promised to bring headphones to the waiting room.", None, False),
    ("I'll remind Mira to bring headphones before the appointment.",
     "Noah promised to remind Mira to bring headphones before the appointment.", None, True),
    ("If you want, I can remind you before the appointment.",
     "Noah promised to remind Mira before the appointment.", None, False),
    ("If you want, I can remind you before the appointment.",
     "Noah promised to remind Mira before the appointment.",
     "I can remind you before the appointment", False),
    ("I can remind you before the appointment.",
     "Noah promised to remind Mira before the appointment.", None, False),
    ("I have not asked for a reminder yet.",
     "Noah promised to ask for a reminder.", None, False),
    ("If the bus is late, I'll text Mira.",
     "Noah promised to text Mira.", "I'll text Mira", False),
    ("If the bus is late, I'll text Mira.",
     "Noah promised to text Mira if the bus is late.", None, True),
    ("If you want, I can remind you before the appointment.",
     "Noah offered to remind Mira before the appointment if asked.", None, True),
])
@pytest.mark.parametrize("lane", ["extractor", "moment", "digest"])
def test_source_presence_and_interpretation_are_separate(source, claim, quote, expected, lane):
    quote = quote or source
    assert quote in source  # Every negative control has a genuine citation.
    stamp = timephrase.utcnow().isoformat()
    row = SimpleNamespace(id=1, session_id="s", role="user", content=source, created_at=stamp,
                          kind="fact", metadata={}, temporal_type="future_plan",
                          relevance_until=None)
    if lane == "extractor":
        result = admit_memory({"kind": "fact", "content": claim,
            "evidence": [{"message_id": 1, "quote": quote}]}, [row],
            session_key="s", writer="extractor").accepted
    elif lane == "moment":
        candidate = SimpleNamespace(evidence={"event_scope": "exchange", "claims": [{
            "text": claim, "evidence": [{"message_id": 1, "quote": quote}],
        }]})
        result = admit_moment(candidate, [row])
    else:
        raw = json.dumps({"claims": [{"text": claim, "source_ids": [1],
                                    "subject": "user", "scope": "intention"}]})
        result = parse_claims(raw, [row], timephrase.utcnow()) is not None
    assert result is expected


@pytest.mark.parametrize("text,expected", [
    ("You should bring headphones to the waiting room.", False),
    ("I'll remind you to bring headphones before the appointment.", True),
    ("If you want, I can remind you before the appointment.", False),
    ("I can remind you before the appointment.", False),
    ("I'll remind you before the appointment if you want.", False),
    ("I have not asked for a reminder yet.", False),
])
def test_promise_ledger_distinguishes_advice_commitment_and_unaccepted_offer(text, expected):
    raw = json.dumps({"promises": [{"who": "assistant", "what": "remind Noah about headphones",
        "source_message_id": 1, "source_quote": text}]})
    rows = {1: {"role": "assistant", "content": text,
                "created_at": timephrase.utcnow().isoformat()}}
    assert bool(PromiseExtractionWorker._parse_promises(raw, source_rows=rows)) is expected

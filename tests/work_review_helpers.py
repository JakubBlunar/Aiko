"""Valid source-bound responses for scripted review clients in integration fixtures."""
import json

from app.core.tasks.work_outcome import digest


def supported_review(messages):
    frame = json.loads(messages[1]["content"])
    text = frame.get("findings", frame.get(
        "proposed_report", " ".join(frame.get("answer_parts", [])),
    ))
    sources = [source for source in frame["evidence"]
               if source.get("content", source.get("result"))]
    source = sources[0]
    quote = str(source.get("content", source.get("result")))[:100]
    criteria = (frame.get("contract") or {}).get("criteria") or [""]
    return json.dumps({
        "supported": True, "reason": "The owned source supports this scripted answer.",
        "missing": [], "criteria": [{"index": n, "supported": True, "claim_indices": [0]}
                                   for n in range(len(criteria))],
        "claims": [{"text": text, "task_id": source["task_id"], "quote": quote}],
    })


def certificate(content, sources):
    return {"status": "supported", "missing": [], "certificate": {
        "version": 1, "answer_digest": digest(content),
        "sources": {str(source["task_id"]): source["revision"] for source in sources},
        "criteria": [{"index": 0, "supported": True}],
    }}

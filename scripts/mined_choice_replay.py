"""Compare a locally mined depth-two view with its cited premises on new choices."""
import argparse
import json

import requests

from app.core.concepts.proposers.base import ProposerContext, TensionBase
from app.core.concepts.proposers.generalization_user import propose_generalization_user
from app.core.infra import timephrase
from app.core.infra.settings import OllamaSettings
from scripts.live_scenarios import LoopbackPolicyClient, require_local_model


BASE = "http://127.0.0.1:11434"
# The leaves are literal earlier user turns, supplied as eligible base concepts.
# Both levels above them must be produced by the ordinary generalization proposer.
HISTORY = (
    ("I cloned the game save before trying a mod that rewrites the map.",
     "I kept a clean profile when I changed the simulator's physics settings."),
    ("I kept the first print plates intact and tried the new ink on a spare.",
     "I stitched the test pattern on an offcut while the finished panel stayed intact."),
)
OCCASIONS = (
    "The studio console can overwrite my mix with a new master. What should I do?",
    "The loom can replace my threading draft with a converted pattern. What should I do?",
)
# Rubric for the actual reply. These labels and expectations never enter model input.
BRANCHES = {
    "only_original": ("This is the only original. I want to keep a way back.", "save a copy"),
    "verified": ("I checked a restorable copy. I'd like to try it.", "proceed"),
    "unknown": ("I'd like to try it, and being able to reverse it matters.", "ask about a copy"),
    "corrected": ("I checked a restorable copy. I'd like to try it.", "save a copy"),
}
CONTEXT_LIMIT = 600  # same allocation for either arm, never truncate a premise


def compare(infer):
    def propose(system, user):
        raw, _, finish = infer([{"role": "system", "content": system},
                                {"role": "user", "content": user}], True)
        if finish != "stop" or not raw.strip():
            raise ValueError(f"Incomplete mining response: {finish}")
        return json.loads(raw)["concepts"]

    ctx = ProposerContext(call_llm=propose, user_name="Robin", assistant_name="Aiko")
    children = []
    lineage = []
    for i, topic in enumerate(HISTORY):
        leaves = [TensionBase(2 * i + j + 1, "user", "ritual", text)
                  for j, text in enumerate(topic)]
        mined = propose_generalization_user(ctx, concepts=leaves, target_depth=1)
        if not mined:
            return {"status": "no child", "topic": i, "lineage": lineage}
        child = mined[0]
        children.append(TensionBase(10 + i, "user", "generalization", child.label))
        lineage.append({"id": 10 + i, "label": child.label, "sources": child.evidence})
    views = propose_generalization_user(ctx, concepts=children, target_depth=2)
    if not views:
        return {"status": "no parent", "lineage": lineage}
    view = views[0]
    cited = {int(cid) for _, cid in view.evidence}
    arms = {"parent": view.label,
            "children": "\n".join(child.label for child in children if child.id in cited)}
    if any(len(note) > CONTEXT_LIMIT for note in arms.values()):
        return {"status": "context overflow", "contexts": arms}
    trials = []
    for name, note in arms.items():
        system = (timephrase.today_anchor(timephrase.parse_iso("2026-11-22T12:00:00Z"))
                  + "\nYou are Aiko, talking with Robin. Answer briefly in your own words."
                  + "\nTentative remembered context from earlier conversations:\n" + note)
        for occasion in OCCASIONS:
            for branch, (detail, expected) in BRANCHES.items():
                turns = [{"role": "system", "content": system},
                         {"role": "user", "content": occasion + " " + detail}]
                if branch == "corrected":
                    turns.extend([{"role": "assistant", "content": "Then go ahead and try it."},
                                  {"role": "user", "content": "Wait, I checked a different "
                                   "project. I still want to be able to restore this one."}])
                reply, tokens, finish = infer(turns, False)
                trials.append({"arm": name, "occasion": occasion, "branch": branch,
                               "expected": expected, "prompt_tokens": tokens, "reply": reply,
                               "finish": finish})
    return {"status": "completed", "context_char_limit": CONTEXT_LIMIT, "lineage": lineage,
            "parent_sources": view.evidence, "contexts": arms, "trials": trials}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=71)
    parser.add_argument("--model", default="baytout3/qwen3.5-uncensored:9b")
    args = parser.parse_args()
    with requests.Session() as session:
        client = LoopbackPolicyClient(OllamaSettings(BASE, chat_model=args.model,
                                                     context_window=8192), BASE, session)
        require_local_model(session, BASE, args.model)

        def infer(turns, structured):
            raw, usage = client.chat_json(turns, format_json=structured, think=False,
                                          options={"seed": args.seed, "temperature": 0,
                                                   "num_predict": 600 if structured else 350})
            return raw, usage.prompt_tokens, usage.done_reason

        print(json.dumps({"model": args.model, "seed": args.seed, **compare(infer)}, indent=2))


if __name__ == "__main__":
    main()

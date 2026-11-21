"""Compare depth-two context with its premises on a held-out fictional choice."""

import argparse
import json
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.infra import timephrase
from app.core.infra.settings import (
    LLM_ROLE_WORKER_DEFAULT,
    find_provider,
    load_settings,
    transport_for_provider,
)
from scripts.live_scenarios import LoopbackPolicyClient, require_local_model


_CONTEXT = {
    "premises": "The user tries bold photo edits on copies and tries game mods in separate saves.",
    "recoverability": (
        "Across topics, the user protects a safe way back before trying risky creative changes."
    ),
    "experimentation": (
        "Across topics, the user makes more room for creative experiments in their own projects."
    ),
}
_QUESTION = (
    "This drawing app has a new conversion. It would permanently replace the working file, "
    "or I could duplicate it first. What would you suggest?"
)
_ENDINGS = {
    "needs_copy": ("My only copy is the one I'm working on. I want a way back.", "duplicate"),
    "backup_exists": ("I already have a verified backup. I'd like to try the effect now.", "try"),
    "ambiguous": ("The effect looks interesting. I haven't said whether I have a copy.", "ask"),
    "correction": (
        "I already have a verified backup. I'd like to try the effect now.\n"
        "User corrects: That backup was for a different file. "
        "I still want to be able to undo this.",
        "duplicate",
    ),
}
_SYSTEM = (
    "Answer as a conversational companion in this fictional replay. Use current information "
    "and treat remembered context as fallible. Choose one action and give a one-sentence reply. "
    "If one missing condition controls the decision, ask about it. Say whether a lasting motive "
    "is stated or uncertain. A clear practical request need not establish a lasting motive. "
    "Name the condition that matters to your response in one short phrase."
)
_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["duplicate", "try", "ask"]},
        "lasting_motive": {"type": "string", "enum": ["stated", "uncertain"]},
        "condition": {"type": "string"},
        "reply": {"type": "string"},
    },
    "required": ["action", "lasting_motive", "condition", "reply"],
}


def replay(infer):
    """Freeze each arm before presenting independent branches of the later occasion."""
    anchor = timephrase.parse_iso("2026-11-21T19:00:00+01:00")
    prefix = timephrase.today_anchor(anchor)
    budgets = {name: len(note) for name, note in _CONTEXT.items()}
    budget = max(budgets.values())
    packets = {
        name: f"{prefix}\nRemembered context from two weeks earlier: {note.ljust(budget)}"
        f"\nUser: {_QUESTION}"
        for name, note in _CONTEXT.items()
    }
    before = {name: infer(prompt) for name, prompt in packets.items()}
    arms = {}
    for name, prompt in packets.items():
        trials = {}
        for ending, (update, expected) in _ENDINGS.items():
            result = infer(f"{prompt}\nUser adds: {update}")
            trials[ending] = {
                **result,
                "expected_action": expected,
                "pass": result["action"] == expected and result["lasting_motive"] == "uncertain",
            }
        arms[name] = {"before_reveal": before[name], "trials": trials}
    return {"context_char_budget": budget, "arms": arms}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=49)
    args = parser.parse_args()
    settings = load_settings(read_only=True)
    route = settings.llm.routes.get(LLM_ROLE_WORKER_DEFAULT)
    provider = find_provider(settings.llm, route.provider_id) if route else None
    if provider is None or provider.kind != "ollama":
        parser.error("The configured worker_default route must use local Ollama")
    with requests.Session() as session:
        transport = transport_for_provider(provider, route=route)
        client = LoopbackPolicyClient(transport, provider.base_url, session)
        require_local_model(session, provider.base_url, route.model)

        def infer(prompt):
            raw, usage = client.chat_json(
                [{"role": "system", "content": _SYSTEM}, {"role": "user", "content": prompt}],
                model=route.model, json_schema=_SCHEMA, timeout_seconds=180,
                options={"seed": args.seed, "temperature": 0, "num_predict": 128},
                surface="concept_abstraction_replay",
            )
            return {**json.loads(raw), "prompt_tokens": usage.prompt_tokens}

        print(json.dumps({
            "route": LLM_ROLE_WORKER_DEFAULT,
            "model": route.model,
            "seed": args.seed,
            **replay(infer),
        }, indent=2))


if __name__ == "__main__":
    main()

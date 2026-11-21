import json
import sys
from types import SimpleNamespace

from app.core.infra.settings import LlmProvider, LlmRoute, LlmSettings
from scripts import concept_abstraction_replay
from scripts.concept_abstraction_replay import replay


def test_comparison_freezes_all_arms_before_presenting_independent_endings():
    prompts = []

    def infer(prompt):
        prompts.append(prompt)
        return {"action": "ask", "lasting_motive": "uncertain", "condition": "backup"}

    result = replay(infer)

    assert len(prompts) == 15
    assert len({len(prompt) for prompt in prompts[:3]}) == 1
    assert all("User adds:" not in prompt for prompt in prompts[:3])
    assert all(prompt.count("User adds:") == 1 for prompt in prompts[3:])
    assert sum("User corrects:" in prompt for prompt in prompts) == 3
    for arm in result["arms"].values():
        assert arm["before_reveal"]["condition"] == "backup"
        assert arm["trials"]["ambiguous"]["pass"]
        assert not arm["trials"]["correction"]["pass"]


def test_replay_uses_worker_model_and_context_instead_of_live_route(monkeypatch, capsys):
    provider = LlmProvider("local", "Local Ollama", "ollama", "http://127.0.0.1:11434")
    settings = SimpleNamespace(llm=LlmSettings(
        providers=[provider],
        routes={
            "worker_default": LlmRoute(provider.id, "worker:9b", context_window=8192),
            "live_policy": LlmRoute(provider.id, "live:4b", context_window=40960),
        },
    ))
    calls = []

    class Client:
        def __init__(self, transport, base_url, session):
            assert base_url == provider.base_url
            assert transport.context_window == 8192

        def chat_json(self, messages, *, model, **options):
            calls.append(model)
            return (
                '{"action":"ask","lasting_motive":"uncertain","condition":"backup","reply":"?"}',
                SimpleNamespace(prompt_tokens=200),
            )

    def model_installed(session, url, model):
        assert (url, model) == (provider.base_url, "worker:9b")

    monkeypatch.setattr(sys, "argv", ["concept_abstraction_replay.py"])
    monkeypatch.setattr(concept_abstraction_replay, "load_settings", lambda *, read_only: settings)
    monkeypatch.setattr(concept_abstraction_replay, "LoopbackPolicyClient", Client)
    monkeypatch.setattr(concept_abstraction_replay, "require_local_model", model_installed)
    concept_abstraction_replay.main()

    result = json.loads(capsys.readouterr().out)
    assert result["route"] == "worker_default"
    assert result["model"] == "worker:9b"
    assert calls == ["worker:9b"] * 15

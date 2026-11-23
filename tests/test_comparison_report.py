from unittest.mock import MagicMock

import pytest

from app.core.conversation.comparison_report import ComparisonReport
from app.core.infra.chat_database import ChatDatabase
from app.core.session.prompt_assembler import PromptAssembler
from app.core.session.turn_runner import TurnRunner
from app.llm.ollama_client import OllamaUsage
from scripts.diagnostic_return_replay import CASES, seed


@pytest.mark.parametrize(("choice", "finish", "expected"), [
    ("SECOND", "stop", "A call transport fault fits better. An input-side fault remains possible."),
    ("UNCLEAR", "stop", "That leaves both explanations open."),
    ("OTHER", "stop", "Ordinary reply."),
    ("FIRST or SECOND", "stop", "Ordinary reply."),
    ("FIRST", "length", "Ordinary reply."),
])
def test_correction_renders_only_the_public_assessment(tmp_path, choice, finish, expected):
    db = ChatDatabase(tmp_path / "microphone.db")
    host, _ = seed(db, "present")
    persona = tmp_path / "persona.txt"
    persona.write_text("Answer briefly.", encoding="utf-8")
    assembler = PromptAssembler(db, persona_path=persona)
    assembler.set_relevant_context_provider(host.build_relevant_context)
    assembler.set_inner_life_providers(
        conversation_situation=host._render_conversation_situation_block)
    host._prompt_assembler = assembler
    client = MagicMock()
    surfaces = []

    def stream(messages, *, surface, **kwargs):
        surfaces.append(surface)
        if surface == "comparison_report":
            assert "actual local recording is clean" in messages[-1]["content"]
            assert "I hear the clicks" not in messages[-1]["content"]
            client.last_usage = OllamaUsage(done_reason=finish)
            return iter([choice])
        client.last_usage = OllamaUsage(done_reason="stop")
        return iter(["Ordinary reply."])

    client.chat_stream.side_effect = stream
    runner = TurnRunner(client, db, assembler, model="fixture", context_window=8192,
                        max_tokens=100, temperature=0, filler_enabled=False)
    visible = []
    result = runner.run(host.session_key, CASES["correction"][1], on_token=visible.append)
    assert result.text.endswith(expected)
    assert "".join(visible) == result.text
    assert db.get_message_row(result.assistant_message_id).content == result.text
    assert choice not in result.text
    fallback = expected == "Ordinary reply."
    assert surfaces == (["comparison_report", "turn_stream"] if fallback else ["comparison_report"])
    assert result.telemetry.comparison_assessed != fallback


def test_changed_input_withholds_a_preference_even_if_the_classifier_picks_one():
    report = ComparisonReport("Are there clicks in a recording of the same input?",
                              ("the input was recorded", "the call was bypassed"),
                              ("An input fault", "A call fault"),
                              "The recording is clean but used a different input.", "")
    assert "both explanations open" in report.reply("SECOND")
    assert report.reply("OTHER") == ""


@pytest.mark.parametrize("text", [
    "Back to the desk microphone clicks. Correction: the recording is clean. "
    "Please show me how to test the call path.",
    "Back to the laptop microphone clicks. Correction: the recording is clean.",
])
def test_mixed_request_or_another_subject_stays_in_ordinary_chat(tmp_path, text):
    db = ChatDatabase(tmp_path / "mixed.db")
    host, _ = seed(db, "present")
    db.add_message(host.session_key, "user", text)
    assert host.reported_comparison(text) is None

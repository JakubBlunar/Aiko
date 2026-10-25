"""The persisted current input must reach the chat model exactly once."""
from unittest.mock import MagicMock

import pytest

from app.core.infra.chat_database import ChatDatabase
from app.core.session.prompt_assembler import PromptAssembler
from app.core.session.turn_runner import TurnRunner
from app.llm.ollama_client import OllamaUsage


@pytest.fixture
def db(tmp_path):
    database = ChatDatabase(tmp_path / "chat.db")
    yield database
    database._local.conn.close()


def _assembler(db, tmp_path):
    persona = tmp_path / "persona.txt"
    persona.write_text("A companion.", encoding="utf-8")
    return PromptAssembler(db, persona_path=persona, recent_window=20)


@pytest.mark.parametrize("prepersisted", [False, True])
@pytest.mark.parametrize("text", ["You are so lovely :3 I am melting now <3", "Sleep well aiko"])
def test_runner_sends_current_message_once(db, tmp_path, prepersisted, text):
    """Exercise both direct runner persistence and the session/voice resume path."""
    db.add_message(session_id="chat", role="assistant", content="Rest well.")
    current_id = None
    if prepersisted:
        current_id = db.add_message(session_id="chat", role="user", content=text)
    client = MagicMock()
    client.chat_stream.return_value = iter(["[[reaction:gentle]] Sleep well."])
    client.last_usage = OllamaUsage()
    runner = TurnRunner(
        ollama=client, db=db, prompt_assembler=_assembler(db, tmp_path),
        model="test", context_window=8192, max_tokens=256, temperature=0.7,
        filler_enabled=False,
    )
    runner.run(session_key="chat", user_text=text, resume_user_message_id=current_id)
    messages = client.chat_stream.call_args.args[0]
    user_messages = [message for message in messages if message["role"] == "user"]
    assert user_messages == [{"role": "user", "content": text}]
    assert len([row for row in db.get_messages("chat") if row.role == "user"]) == 1


@pytest.mark.parametrize("aggressive", [False, True])
def test_excludes_only_current_row_and_preserves_cached_history(db, tmp_path, aggressive):
    """A real earlier repetition remains, and filtering cannot mutate the slice cache."""
    text = "Sleep well aiko"
    db.add_message(session_id="chat", role="user", content=text)
    db.add_message(session_id="chat", role="assistant", content="Sleep well.")
    current_id = db.add_message(session_id="chat", role="user", content=text)
    assembler = _assembler(db, tmp_path)
    for _ in range(2):
        messages, _ = assembler.assemble_with_budget(
            "chat", text, context_window=8192, response_budget=256,
            aggressive=aggressive, current_user_message_id=current_id,
        )
        assert len([message for message in messages if message["role"] == "user"]) == 2
    assert assembler._slice_cache["chat"].history_msgs[-1].id == current_id


def test_unpersisted_preview_and_empty_live_input_keep_history(db, tmp_path):
    db.add_message(session_id="chat", role="user", content="Are you awake?")
    assembler = _assembler(db, tmp_path)
    for text in ("", "I am back."):
        messages, _ = assembler.assemble_with_budget(
            "chat", text, context_window=8192, response_budget=256,
        )
        assert messages[1]["role"] == "user"
        assert messages[1]["content"].endswith("Are you awake?")
        assert len([message for message in messages if message["role"] == "user"]) == (
            2 if text else 1
        )

"""Journal awareness survives compaction without becoming a conversational offer."""
from app.core.infra.chat_database import ChatDatabase
from app.core.memory.diary_journal import diary_awareness, journal_authorship
from app.core.session.prompt_assembler import PromptAssembler, _PROMPT_BLOCK_TIERS


def test_diary_awareness_tracks_available_tools_outside_the_fixed_persona(tmp_path):
    persona = tmp_path / "persona.txt"
    persona.write_text("You are Aiko.", encoding="utf-8")
    assembler = PromptAssembler(ChatDatabase(tmp_path / "chat.db"), persona_path=persona)
    names = ["read_diary", "write_diary"]
    assembler.set_diary_awareness_provider(lambda: diary_awareness(names))
    messages, _ = assembler.assemble_with_budget("s1", "Hi", context_window=8192,
                                               response_budget=256)
    assert "Your saved diary exists across conversations" in messages[0]["content"]
    assert "Keeping a diary is optional" in messages[0]["content"]
    assert _PROMPT_BLOCK_TIERS["T0_stable"][-1] == "diary_awareness_block"
    names.clear()
    messages, _ = assembler.assemble_with_budget("s1", "Hello", context_window=8192,
                                               response_budget=256)
    assert "Your saved diary exists" not in messages[0]["content"]


def test_legacy_authorship_remains_unknown_instead_of_being_invented():
    assert journal_authorship({}) == "unspecified"
    assert journal_authorship({"memory_origin": {"writer": "diary_worker"}}) == "background"
    assert journal_authorship({"journal": {"version": 1, "authorship": "chosen"}}) == "chosen"
    assert journal_authorship({"journal": {"version": 1, "authorship": ["chosen"]},
                               "memory_origin": {"writer": []}}) == "unspecified"

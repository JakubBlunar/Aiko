from app.core.concepts.concept_evidence_lineage import memory_lineages, support_summary
from tests.test_dream_worker import _Fixture, _FakeOllama, _make_worker


def test_dream_preserves_full_qualified_context_and_derivation_without_new_votes():
    fixture = _Fixture()
    try:
        text = ("I want to use concrete examples. " + "Some background. " * 15
                + "Except during grief.")
        row = fixture.memory.add(text, "self", fixture.embedder.embed(text))
        model = _FakeOllama("I pictured a small bridge between an example and a question.")
        worker = _make_worker(fixture=fixture, ollama=model)
        memory = worker.maybe_run(user_id="u", session_key="s", hours_since_last=8,
                                  recent_self_memories=[row])
        assert memory is not None
        prompt = model.calls[0]["messages"][-1]["content"]
        assert "Except during grief." in prompt and f"ID {row.id}" in prompt
        receipt = memory.metadata["reflective_derivation"]
        assert receipt["memory_inputs"][0]["memory_id"] == row.id
        assert receipt["scope"] == "generation_context"
        assert receipt["unresolved_text_inputs"] == 0
        assert support_summary(memory_lineages(memory))["known_support_groups"] == 0
    finally:
        fixture.close()


def test_source_edit_during_dream_generation_defers_output():
    fixture = _Fixture()
    try:
        row = fixture.memory.add("I like slow examples", "self", fixture.embedder.embed("example"))

        class EditingModel(_FakeOllama):
            def chat(self, *args, **kwargs):
                fixture.memory.update(row.id, content="A corrected preference")
                return super().chat(*args, **kwargs)

        worker = _make_worker(fixture=fixture, ollama=EditingModel("A small associative dream."))
        assert worker.maybe_run(user_id="u", session_key="s", hours_since_last=8,
                                recent_self_memories=[row]) is None
        assert not fixture.memory.iter_by_kind("reflection")
    finally:
        fixture.close()


def test_legacy_text_is_kept_as_unknown_derivation_without_fabricating_ids():
    fixture = _Fixture()
    try:
        model = _FakeOllama("I imagined a quiet room with a question on the table.")
        worker = _make_worker(fixture=fixture, ollama=model)
        memory = worker.maybe_run(user_id="u", session_key="s", hours_since_last=8,
                                  recent_callbacks=["An older question with unknown identity"])
        receipt = memory.metadata["reflective_derivation"]
        assert not receipt["memory_inputs"] and receipt["unresolved_text_inputs"] == 1
        assert "unknown input identity" in model.calls[0]["messages"][-1]["content"]
    finally:
        fixture.close()

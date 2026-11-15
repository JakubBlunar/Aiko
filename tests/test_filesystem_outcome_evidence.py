"""The existing note/file plugin proves exact text persistence without executing code."""
import importlib
from pathlib import Path

from app.core.tasks.work_evidence import postcondition_payload


def test_note_proof_preserves_whitespace_and_excludes_partial_reads(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "plugins/filesystem"))
    adapter = importlib.import_module("aiko_files.evidence").TextFileEvidence("filesystem")
    text = "  groceries\n"
    written = adapter.transform("filesystem", "write_file", "accepted", {
        "path": "C:/Notes/Shopping.txt", "content": text,
    })
    read = adapter.transform("filesystem", "read_text_file", text, {
        "path": "c:/notes/shopping.txt",
    })
    expected, observed = map(postcondition_payload, [written.postcondition, read.postcondition])
    assert expected["resource"] == observed["resource"]
    assert expected["expected"] == observed["observed"]
    partial = adapter.transform("filesystem", "read_text_file", text, {
        "path": "c:/notes/shopping.txt", "head": 1,
    })
    assert partial.postcondition is None


def test_a_different_note_cannot_verify_a_write(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "plugins/filesystem"))
    adapter = importlib.import_module("aiko_files.evidence").TextFileEvidence("filesystem")
    write = adapter.transform("filesystem", "write_file", "accepted", {
        "path": "/notes/a.txt", "content": "hello",
    })
    read = adapter.transform("filesystem", "read_text_file", "hello", {"path": "/notes/b.txt"})
    assert write.postcondition.resource != read.postcondition.resource

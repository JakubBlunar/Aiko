"""Tests for in-chat file attachments (D2 Part B).

Covers four seams:

* the managed attachments module (``save_attachment`` / ``classify_extension``
  / ``delete_attachment`` / ``attachments_root``),
* schema-v18 ``messages.attachments`` persistence + round-trip,
* the ``_render_attachments_block`` inner-life turn-hint provider,
* the ``POST/DELETE /api/chat/attachments`` REST endpoints + the WS
  ``_sanitize_attachment_refs`` allow-list guard.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

import app.core.tasks.attachments as attach_mod
from app.core.infra.chat_database import ChatDatabase
from app.core.session.inner_life_providers_mixin import InnerLifeProvidersMixin
from app.core.tasks.attachments import (
    ATTACHMENTS_LABEL,
    SavedAttachment,
    attachments_root,
    classify_extension,
    delete_attachment,
    save_attachment,
)
from app.web.server import _sanitize_attachment_refs, create_web_app


_PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64


class _TmpDirMixin(unittest.TestCase):
    """Redirect the module-level attachments dir to a temp dir."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._orig_dir = attach_mod.ATTACHMENTS_DIR
        attach_mod.ATTACHMENTS_DIR = Path(self._tmp.name)
        self.addCleanup(
            setattr, attach_mod, "ATTACHMENTS_DIR", self._orig_dir,
        )


class SaveAttachmentTests(_TmpDirMixin):
    def test_saves_image(self) -> None:
        saved = save_attachment(data=_PNG, filename="cat.png")
        self.assertIsInstance(saved, SavedAttachment)
        self.assertEqual(saved.kind, "image")
        self.assertTrue(saved.rel_path.startswith(f"{ATTACHMENTS_LABEL}:"))
        self.assertEqual(saved.filename, "cat.png")
        self.assertEqual(saved.bytes, len(_PNG))
        stored = saved.rel_path.split(":", 1)[1]
        self.assertTrue((attach_mod.ATTACHMENTS_DIR / stored).is_file())

    def test_saves_text(self) -> None:
        saved = save_attachment(data=b"hello world", filename="notes.md")
        self.assertEqual(saved.kind, "text")

    def test_rejects_unsupported_extension(self) -> None:
        with self.assertRaises(ValueError):
            save_attachment(data=b"MZ\x90\x00", filename="virus.exe")

    def test_rejects_empty(self) -> None:
        with self.assertRaises(ValueError):
            save_attachment(data=b"", filename="empty.png")

    def test_rejects_oversize(self) -> None:
        with self.assertRaises(ValueError):
            save_attachment(
                data=b"x" * 2048, filename="big.txt", max_bytes=1024,
            )

    def test_uuid_stored_name_avoids_collision(self) -> None:
        a = save_attachment(data=_PNG, filename="same.png")
        b = save_attachment(data=_PNG, filename="same.png")
        self.assertNotEqual(a.rel_path, b.rel_path)

    def test_as_dict_shape(self) -> None:
        saved = save_attachment(data=_PNG, filename="cat.png")
        d = saved.as_dict()
        self.assertEqual(
            set(d.keys()), {"id", "filename", "kind", "rel_path", "bytes"},
        )


class ClassifyExtensionTests(unittest.TestCase):
    def test_image(self) -> None:
        self.assertEqual(classify_extension(".jpg"), "image")
        self.assertEqual(classify_extension("png"), "image")

    def test_text(self) -> None:
        self.assertEqual(classify_extension(".txt"), "text")
        self.assertEqual(classify_extension(".py"), "text")

    def test_unknown(self) -> None:
        self.assertIsNone(classify_extension(".exe"))
        self.assertIsNone(classify_extension(""))

    def test_custom_image_set(self) -> None:
        # A narrower image set drops .gif but keeps .png.
        self.assertIsNone(
            classify_extension(".gif", image_extensions=(".png",)),
        )
        self.assertEqual(
            classify_extension(".png", image_extensions=(".png",)), "image",
        )


class DeleteAttachmentTests(_TmpDirMixin):
    def test_deletes_by_stored_name(self) -> None:
        saved = save_attachment(data=_PNG, filename="cat.png")
        stored = saved.rel_path.split(":", 1)[1]
        self.assertTrue(delete_attachment(stored))
        self.assertFalse((attach_mod.ATTACHMENTS_DIR / stored).exists())

    def test_deletes_by_rel_path(self) -> None:
        saved = save_attachment(data=_PNG, filename="cat.png")
        self.assertTrue(delete_attachment(saved.rel_path))

    def test_missing_returns_false(self) -> None:
        self.assertFalse(delete_attachment("nope.png"))

    def test_traversal_guard(self) -> None:
        # A name that escapes the managed dir must be rejected outright.
        self.assertFalse(delete_attachment("../../etc/passwd"))


class AttachmentsRootTests(_TmpDirMixin):
    def test_root_is_read_only_and_labelled(self) -> None:
        root = attachments_root()
        self.assertEqual(root.label, ATTACHMENTS_LABEL)
        self.assertTrue(root.read_only)
        self.assertEqual(root.path, str(attach_mod.ATTACHMENTS_DIR))


class MessageAttachmentsPersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = ChatDatabase(Path(self._tmp.name) / "chat.db")
        # Close the thread-local sqlite connection before the temp dir is
        # reaped (Windows can't unlink an open DB file). LIFO cleanup
        # order runs this before ``_tmp.cleanup``.
        self.addCleanup(self._close_db)

    def _close_db(self) -> None:
        try:
            self.db._get_conn().close()
        except Exception:
            pass

    def test_roundtrip(self) -> None:
        payload = [
            {
                "id": "abc",
                "filename": "cat.png",
                "kind": "image",
                "rel_path": "Attachments:abc.png",
                "bytes": 10,
            }
        ]
        mid = self.db.add_message(
            "s1", "user", "look at this", attachments=json.dumps(payload),
        )
        self.assertGreater(mid, 0)
        rows = self.db.get_messages("s1")
        self.assertEqual(len(rows), 1)
        self.assertIsNotNone(rows[0].attachments)
        self.assertEqual(json.loads(rows[0].attachments), payload)

    def test_no_attachments_is_null(self) -> None:
        self.db.add_message("s1", "assistant", "hi")
        rows = self.db.get_messages("s1")
        self.assertIsNone(rows[0].attachments)


class _ProviderHost:
    """Minimal host exposing only what ``_render_attachments_block`` reads."""

    user_display_name = "Jacob"

    def __init__(self, attachments: list[dict]) -> None:
        self._active_turn_attachments = attachments


class AttachmentsProviderTests(unittest.TestCase):
    render = staticmethod(InnerLifeProvidersMixin._render_attachments_block)

    def test_silent_when_empty(self) -> None:
        self.assertEqual(self.render(_ProviderHost([])), "")

    def test_image_routes_to_describe_image(self) -> None:
        block = self.render(
            _ProviderHost([
                {
                    "filename": "cat.png",
                    "kind": "image",
                    "rel_path": "Attachments:abc.png",
                }
            ])
        )
        self.assertIn("Attachments:abc.png", block)
        self.assertIn("describe_image", block)
        self.assertIn("start_workflow", block)
        self.assertIn("Jacob", block)

    def test_text_routes_to_read_file(self) -> None:
        block = self.render(
            _ProviderHost([
                {
                    "filename": "notes.md",
                    "kind": "text",
                    "rel_path": "Attachments:def.md",
                }
            ])
        )
        self.assertIn("read_file", block)
        self.assertNotIn("describe_image", block)

    def test_skips_malformed_entries(self) -> None:
        block = self.render(
            _ProviderHost([{"kind": "image"}, "not a dict"])  # no rel_path
        )
        self.assertEqual(block, "")


class SanitizeAttachmentRefsTests(unittest.TestCase):
    def test_keeps_valid_attachments_root_refs(self) -> None:
        refs = _sanitize_attachment_refs([
            {
                "id": "abc",
                "filename": "cat.png",
                "kind": "image",
                "rel_path": "Attachments:abc.png",
                "bytes": 10,
            }
        ])
        self.assertEqual(len(refs), 1)
        self.assertEqual(refs[0]["rel_path"], "Attachments:abc.png")

    def test_drops_non_attachments_root(self) -> None:
        # A path into another root must never be honoured via the chat
        # side-channel.
        refs = _sanitize_attachment_refs([
            {"kind": "text", "rel_path": "Docs:secret.txt"}
        ])
        self.assertEqual(refs, [])

    def test_drops_path_traversal(self) -> None:
        refs = _sanitize_attachment_refs([
            {"kind": "image", "rel_path": "Attachments:../escape.png"}
        ])
        self.assertEqual(refs, [])

    def test_drops_bad_kind(self) -> None:
        refs = _sanitize_attachment_refs([
            {"kind": "binary", "rel_path": "Attachments:a.bin"}
        ])
        self.assertEqual(refs, [])

    def test_drops_non_list(self) -> None:
        self.assertEqual(_sanitize_attachment_refs(None), [])
        self.assertEqual(_sanitize_attachment_refs("nope"), [])

    def test_caps_at_eight(self) -> None:
        many = [
            {"kind": "image", "rel_path": f"Attachments:{i}.png"}
            for i in range(20)
        ]
        self.assertEqual(len(_sanitize_attachment_refs(many)), 8)


class AttachmentEndpointTests(_TmpDirMixin):
    def setUp(self) -> None:
        super().setUp()
        session = MagicMock()
        # ``None`` vision config -> upload uses module defaults for the
        # extension allow-list + byte cap.
        session._settings.agent.vision = None
        self.client = TestClient(create_web_app(session))

    def test_upload_image(self) -> None:
        resp = self.client.post(
            "/api/chat/attachments",
            files={"file": ("cat.png", _PNG, "image/png")},
        )
        self.assertEqual(resp.status_code, 200)
        att = resp.json()["attachment"]
        self.assertEqual(att["kind"], "image")
        self.assertTrue(att["rel_path"].startswith("Attachments:"))

    def test_upload_rejects_unsupported(self) -> None:
        resp = self.client.post(
            "/api/chat/attachments",
            files={"file": ("virus.exe", b"MZ", "application/octet-stream")},
        )
        self.assertEqual(resp.status_code, 400)

    def test_upload_rejects_empty(self) -> None:
        resp = self.client.post(
            "/api/chat/attachments",
            files={"file": ("empty.png", b"", "image/png")},
        )
        self.assertEqual(resp.status_code, 400)

    def test_delete_roundtrip(self) -> None:
        up = self.client.post(
            "/api/chat/attachments",
            files={"file": ("cat.png", _PNG, "image/png")},
        ).json()["attachment"]
        stored = up["rel_path"].split(":", 1)[1]
        resp = self.client.delete(f"/api/chat/attachments/{stored}")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["deleted"])


if __name__ == "__main__":
    unittest.main()

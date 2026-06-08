"""Tests for the OS-keychain-backed secret store.

The real ``keyring`` backend is never touched: a fake in-memory module
is injected via ``app.core.infra.secret_store._keyring`` so the suite is
deterministic and side-effect-free. The production guard
``running_under_test`` is patched off where we want to exercise the
keychain path directly.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from app.core.infra import secret_store


class _FakeKeyring:
    """Minimal in-memory stand-in for the ``keyring`` module."""

    class _FailBackend:
        pass

    class _RealBackend:
        pass

    def __init__(self, *, available: bool = True, raises: bool = False) -> None:
        self._store: dict[tuple[str, str], str] = {}
        self._available = available
        self._raises = raises

    def get_keyring(self):
        return self._RealBackend() if self._available else self._FailBackend()

    def get_password(self, service: str, account: str):
        if self._raises:
            raise RuntimeError("backend exploded")
        return self._store.get((service, account))

    def set_password(self, service: str, account: str, value: str) -> None:
        if self._raises:
            raise RuntimeError("backend exploded")
        self._store[(service, account)] = value

    def delete_password(self, service: str, account: str) -> None:
        if self._raises:
            raise RuntimeError("backend exploded")
        try:
            del self._store[(service, account)]
        except KeyError as exc:  # mirror keyring's PasswordDeleteError shape
            raise RuntimeError("not found") from exc


class AccountNamingTests(unittest.TestCase):
    def test_provider_account_prefix(self) -> None:
        self.assertEqual(secret_store.provider_account("openai"), "provider:openai")
        self.assertEqual(secret_store.provider_account("  groq "), "provider:groq")


class NoBackendTests(unittest.TestCase):
    def test_get_returns_empty_without_keyring(self) -> None:
        with patch.object(secret_store, "_keyring", return_value=None):
            self.assertEqual(secret_store.get_secret("provider:openai"), "")
            self.assertFalse(secret_store.is_available())
            self.assertEqual(secret_store.backend_name(), "none")

    def test_set_returns_false_without_keyring(self) -> None:
        with patch.object(secret_store, "_keyring", return_value=None):
            self.assertFalse(secret_store.set_secret("provider:openai", "sk-x"))

    def test_fail_backend_is_unavailable(self) -> None:
        fake = _FakeKeyring(available=False)
        with patch.object(secret_store, "_keyring", return_value=fake):
            self.assertFalse(secret_store.is_available())


class RoundTripTests(unittest.TestCase):
    def test_set_get_delete(self) -> None:
        fake = _FakeKeyring()
        with patch.object(secret_store, "_keyring", return_value=fake):
            self.assertTrue(secret_store.set_secret("provider:openai", "sk-abc"))
            self.assertEqual(secret_store.get_secret("provider:openai"), "sk-abc")
            self.assertTrue(secret_store.is_available())
            # Empty value deletes.
            self.assertTrue(secret_store.set_secret("provider:openai", ""))
            self.assertEqual(secret_store.get_secret("provider:openai"), "")

    def test_whitespace_is_stripped(self) -> None:
        fake = _FakeKeyring()
        with patch.object(secret_store, "_keyring", return_value=fake):
            secret_store.set_secret("provider:openai", "  sk-abc \n")
            self.assertEqual(secret_store.get_secret("provider:openai"), "sk-abc")

    def test_empty_account_is_ignored(self) -> None:
        fake = _FakeKeyring()
        with patch.object(secret_store, "_keyring", return_value=fake):
            self.assertFalse(secret_store.set_secret("", "sk-abc"))
            self.assertEqual(secret_store.get_secret(""), "")

    def test_backend_exception_degrades_gracefully(self) -> None:
        fake = _FakeKeyring(raises=True)
        with patch.object(secret_store, "_keyring", return_value=fake):
            self.assertFalse(secret_store.set_secret("provider:openai", "sk-x"))
            self.assertEqual(secret_store.get_secret("provider:openai"), "")


class StoreOrPassthroughTests(unittest.TestCase):
    def test_inert_under_test_returns_key_unchanged(self) -> None:
        # running_under_test() is True inside pytest -> passthrough.
        fake = _FakeKeyring()
        with patch.object(secret_store, "_keyring", return_value=fake):
            self.assertEqual(
                secret_store.store_or_passthrough("provider:openai", "sk-abc"),
                "sk-abc",
            )
            # Nothing was written to the keychain.
            self.assertEqual(secret_store.get_secret("provider:openai"), "")

    def test_stores_and_blanks_disk_when_backend_present(self) -> None:
        fake = _FakeKeyring()
        with patch.object(secret_store, "running_under_test", return_value=False), \
                patch.object(secret_store, "_keyring", return_value=fake):
            disk = secret_store.store_or_passthrough("provider:openai", "sk-abc")
            self.assertEqual(disk, "")  # blanked on disk
            self.assertEqual(secret_store.get_secret("provider:openai"), "sk-abc")

    def test_passthrough_plaintext_when_no_backend(self) -> None:
        with patch.object(secret_store, "running_under_test", return_value=False), \
                patch.object(secret_store, "_keyring", return_value=None):
            disk = secret_store.store_or_passthrough("provider:openai", "sk-abc")
            self.assertEqual(disk, "sk-abc")  # key not lost

    def test_empty_value_blanks_disk(self) -> None:
        fake = _FakeKeyring()
        with patch.object(secret_store, "running_under_test", return_value=False), \
                patch.object(secret_store, "_keyring", return_value=fake):
            self.assertEqual(secret_store.store_or_passthrough("provider:openai", ""), "")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

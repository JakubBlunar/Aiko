"""OS-keychain-backed storage for LLM API keys.

API keys (OpenAI, Gemini, Groq, ...) used to live as plaintext inside
``config/user.json``. This module moves them into the operating
system's secure credential store -- Windows Credential Manager,
macOS Keychain, or the Freedesktop Secret Service on Linux -- via the
``keyring`` package, keeping the on-disk config free of secrets.

Design contract
---------------
* :func:`get_secret` / :func:`set_secret` / :func:`delete_secret` are
  thin, **best-effort** wrappers around ``keyring``. They never raise:
  a missing backend, locked keychain, or import failure degrades to
  "no stored secret" so the app keeps booting.
* :func:`store_or_passthrough` is the write-path helper. It tries to
  stash the key in the keychain and returns the value the caller should
  persist to ``user.json``:

  - ``""`` when the keychain accepted the value (or the value was empty
    and therefore deleted), so no plaintext lands on disk;
  - the original ``api_key`` as a fallback when no backend is usable, so
    a key is never silently lost on a machine without a keychain.
* Everything no-ops under pytest (:func:`running_under_test`) so the
  test suite never touches the developer's real keychain and the
  pre-keyring plaintext-config behaviour is preserved verbatim.

Account naming: a single :data:`SERVICE_NAME` namespace with one account
per credential -- :data:`CHAT_LLM_ACCOUNT` for the legacy ``chat_llm``
block and ``provider:<id>`` (:func:`provider_account`) for each
catalogue provider row.
"""

from __future__ import annotations

import logging
import os
import sys

log = logging.getLogger("app.secret_store")

# Namespace under which every Aiko credential is filed in the OS store.
SERVICE_NAME = "aiko-assistant"

# Account name for the legacy ``chat_llm`` block when no catalogue
# provider can be resolved for it (see ``_chat_llm_secret_account`` on
# the controller, which prefers the ``main_chat`` provider's account).
CHAT_LLM_ACCOUNT = "chat_llm"

# Account name for the web-search backend (LangSearch) API key.
SEARCH_API_KEY_ACCOUNT = "search_api_key"

# Account name for a (future) keyed weather backend's API key. Open-Meteo
# is keyless, so this is unused today but kept for parity with the search
# credential path so a swapped-in keyed provider has somewhere to store it.
WEATHER_API_KEY_ACCOUNT = "weather_api_key"


def provider_account(provider_id: str) -> str:
    """Keychain account name for a catalogue provider row."""
    return f"provider:{(provider_id or '').strip()}"


def running_under_test() -> bool:
    """True when executing inside pytest.

    The secret store is intentionally inert during tests: it must never
    read or write the developer's real OS keychain, and the pre-keyring
    behaviour (plaintext key straight from config) is what the existing
    suite asserts against.
    """
    return "PYTEST_CURRENT_TEST" in os.environ or "pytest" in sys.modules


def _keyring():
    """Import ``keyring`` lazily, returning the module or ``None``."""
    try:
        import keyring  # noqa: PLC0415 - lazy by design (optional dep)

        return keyring
    except Exception:  # pragma: no cover - import guard
        return None


def is_available() -> bool:
    """Whether a real keychain backend is present and usable.

    ``keyring`` falls back to ``backends.fail.Keyring`` when nothing
    real is installed (common on headless CI); that counts as
    unavailable here.
    """
    kr = _keyring()
    if kr is None:
        return False
    try:
        name = kr.get_keyring().__class__.__name__.lower()
        return "fail" not in name
    except Exception:  # pragma: no cover - defensive
        return False


def backend_name() -> str:
    """Human-readable backend class name for logs / diagnostics."""
    kr = _keyring()
    if kr is None:
        return "none"
    try:
        return kr.get_keyring().__class__.__name__
    except Exception:  # pragma: no cover - defensive
        return "unknown"


def get_secret(account: str) -> str:
    """Return the stored secret for ``account`` ("" when absent)."""
    if not account:
        return ""
    kr = _keyring()
    if kr is None:
        return ""
    try:
        value = kr.get_password(SERVICE_NAME, account)
    except Exception as exc:  # pragma: no cover - backend hiccup
        log.debug("secret-store get failed account=%s: %s", account, exc)
        return ""
    return (value or "").strip()


def set_secret(account: str, value: str) -> bool:
    """Store ``value`` for ``account`` (or delete it when empty).

    Returns ``True`` on success (including a successful delete of an
    empty value), ``False`` when no backend accepted the write.
    """
    if not account:
        return False
    kr = _keyring()
    if kr is None:
        return False
    normalized = (value or "").strip()
    try:
        if not normalized:
            try:
                kr.delete_password(SERVICE_NAME, account)
            except Exception:
                # Deleting a key that was never stored is fine.
                pass
            return True
        kr.set_password(SERVICE_NAME, account, normalized)
        return True
    except Exception as exc:
        log.warning("secret-store set failed account=%s: %s", account, exc)
        return False


def delete_secret(account: str) -> None:
    """Remove the secret for ``account`` (best-effort)."""
    set_secret(account, "")


def store_or_passthrough(account: str, api_key: str) -> str:
    """Write-path helper: stash ``api_key`` and return the disk value.

    * Returns ``""`` when the keychain accepted the value (or the value
      was empty and therefore deleted) -- nothing plaintext to persist.
    * Returns the original ``api_key`` as a fallback when no backend is
      usable, so a key is never silently lost on a keychain-less box.

    Inert under pytest: returns ``api_key`` unchanged and never touches
    the keychain, so persisted config in tests matches the historical
    plaintext behaviour.
    """
    if running_under_test():
        return api_key or ""
    if set_secret(account, api_key):
        return ""
    return api_key or ""

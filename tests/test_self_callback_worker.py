"""Worker-level tests for K71 SelfCallbackWorker (LLM + fallback)."""
from __future__ import annotations

import json
import unittest
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.core.affect import self_callback as sc
from app.core.proactive.self_callback_worker import SelfCallbackWorker


@dataclass
class _Mem:
    id: int
    content: str
    created_at: str


def _aged(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


class _FakeStore:
    def __init__(self, mems):
        self._mems = mems

    def iter_by_kinds(self, kinds):
        return list(self._mems)


class _FakeKV:
    def __init__(self):
        self.d: dict[str, str] = {}

    def get(self, k):
        return self.d.get(k)

    def set(self, k, v):
        self.d[k] = v


class _FakeClient:
    """Returns a fixed JSON selection from chat_stream."""

    def __init__(self, payload: str | None):
        self.payload = payload
        self.called = False

    def chat_stream(self, messages, **kw):
        self.called = True
        if self.payload is None:
            raise RuntimeError("boom")
        yield self.payload


def _worker(store, kv, client=None, **kw):
    return SelfCallbackWorker(
        memory_store=store,
        kv_get=kv.get,
        kv_set=kv.set,
        cooldown_days=0.0,  # no cooldown for the test
        min_age_days=14,
        worker_client=client,
        worker_model="test-model" if client else "",
        user_name_provider=lambda: "Jacob",
        **kw,
    )


class SelfCallbackWorkerLlmTests(unittest.TestCase):
    def test_llm_pick_used(self) -> None:
        mems = [
            _Mem(1, "I own a red bike", _aged(40)),
            _Mem(2, "I've been feeling restless", _aged(30)),
        ]
        kv = _FakeKV()
        # LLM picks the bike row but classifies it intention — proves the
        # LLM choice (not the regex) drives the result.
        client = _FakeClient(
            json.dumps({"memory_id": 1, "kind": "intention", "worth": True})
        )
        res = _worker(_FakeStore(mems), kv, client).run()
        self.assertEqual(res["drafted"], 1)
        self.assertEqual(res["memory_id"], 1)
        self.assertEqual(res["kind"], "intention")
        self.assertEqual(res["source"], "llm")
        self.assertTrue(client.called)

    def test_llm_worth_false_falls_back_to_heuristic(self) -> None:
        mems = [_Mem(2, "I've been feeling restless", _aged(30))]
        kv = _FakeKV()
        client = _FakeClient(json.dumps({"memory_id": 2, "worth": False}))
        res = _worker(_FakeStore(mems), kv, client).run()
        # Heuristic still finds the feeling row.
        self.assertEqual(res["drafted"], 1)
        self.assertEqual(res["source"], "heuristic")
        self.assertEqual(res["kind"], "feeling")

    def test_llm_exception_falls_back(self) -> None:
        mems = [_Mem(2, "I want to learn astronomy", _aged(30))]
        kv = _FakeKV()
        client = _FakeClient(None)  # raises in chat_stream
        res = _worker(_FakeStore(mems), kv, client).run()
        self.assertEqual(res["drafted"], 1)
        self.assertEqual(res["source"], "heuristic")

    def test_no_client_uses_heuristic(self) -> None:
        mems = [_Mem(2, "I've been feeling low", _aged(30))]
        kv = _FakeKV()
        res = _worker(_FakeStore(mems), kv, None).run()
        self.assertEqual(res["source"], "heuristic")

    def test_llm_disabled_provider_uses_heuristic(self) -> None:
        mems = [_Mem(2, "I've been feeling low", _aged(30))]
        kv = _FakeKV()
        client = _FakeClient(
            json.dumps({"memory_id": 2, "kind": "feeling", "worth": True})
        )
        res = _worker(
            _FakeStore(mems), kv, client,
            llm_enabled_provider=lambda: False,
        ).run()
        self.assertEqual(res["source"], "heuristic")
        self.assertFalse(client.called)

    def test_ring_records_source(self) -> None:
        mems = [_Mem(2, "I've been feeling restless", _aged(30))]
        kv = _FakeKV()
        _worker(_FakeStore(mems), kv, None).run()
        ring = sc.load_callbacks(kv.get)
        self.assertEqual(ring[-1]["source"], "heuristic")


if __name__ == "__main__":
    unittest.main()

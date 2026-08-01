"""Worker-level tests for K71 SelfCallbackWorker (LLM + fallback + pool)."""
from __future__ import annotations

import json
import unittest
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from app.core.affect import self_callback as sc
from app.core.infra.chat_database import ChatDatabase
from app.core.proactive.cue_store import CueStore
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


def _worker(store, kv, client=None, *, cues=None, **kw):
    return SelfCallbackWorker(
        memory_store=store,
        kv_get=kv.get,
        kv_set=kv.set,
        cue_store_provider=(lambda: cues) if cues is not None else None,
        min_age_days=14,
        worker_client=client,
        worker_model="test-model" if client else "",
        user_name_provider=lambda: "Jacob",
        **kw,
    )


def _cue_store() -> tuple[CueStore, TemporaryDirectory]:
    tmp = TemporaryDirectory(ignore_cleanup_errors=True)
    return CueStore(ChatDatabase(Path(tmp.name) / "chat.db")), tmp


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


class PoolProductionTests(unittest.TestCase):
    """The half that replaced the ten-day producer cooldown."""

    def setUp(self) -> None:
        self.cues, tmp = _cue_store()
        self.addCleanup(tmp.cleanup)
        self.kv = _FakeKV()

    def _run(self, mems, **kw):
        return _worker(_FakeStore(mems), self.kv, None, cues=self.cues, **kw)

    def test_a_drafted_callback_lands_in_the_pool(self) -> None:
        self._run([_Mem(2, "I've been feeling restless", _aged(30))]).run()
        rows = self.cues.pending("self_callback")
        self.assertEqual(len(rows), 1)
        self.assertIn("restless", rows[0].subject)
        # The rendered line, not the raw excerpt -- the pool row is what
        # the provider puts in the prompt, so the age phrasing and the
        # user's name have to be baked in at production time.
        text = rows[0].text.lower()
        self.assertIn("a few weeks ago", text)
        self.assertIn("jacob", text)

    def test_pressure_falls_as_the_shelf_fills(self) -> None:
        now = datetime.now(timezone.utc)
        empty = self._run([]).demand(now=now, last_run_at=None)
        self.assertEqual(empty.pressure, 1.0)
        for i in range(2):
            self.cues.add("self_callback", f"subject {i}", "cue")
        full = self._run([]).demand(now=now, last_run_at=None)
        self.assertEqual(full.pressure, 0.0)

    def test_a_disabled_worker_reports_no_pressure(self) -> None:
        signal = self._run([], enabled_provider=lambda: False).demand(
            now=datetime.now(timezone.utc), last_run_at=None,
        )
        self.assertEqual(signal.pressure, 0.0)
        self.assertEqual(signal.reason, "disabled")

    def test_demand_declares_the_llm_pass(self) -> None:
        """The scheduler's LLM lane has to know before admitting the run."""
        worker = _worker(
            _FakeStore([]),
            self.kv,
            _FakeClient("{}"),
            cues=self.cues,
        )
        signal = worker.demand(
            now=datetime.now(timezone.utc), last_run_at=None,
        )
        self.assertTrue(signal.needs_llm)

    def test_an_excerpt_the_pool_already_used_is_not_redrafted(self) -> None:
        """Wider than the ring's signature check, which forgets.

        A callback Aiko already closed the loop on must not come back as
        a fresh cue, and its row is ``used`` rather than pending -- so
        only the pool can rule it out.
        """
        mems = [_Mem(2, "I've been feeling restless", _aged(30))]
        self._run(mems).run()
        row = self.cues.pending("self_callback")[0]
        self.cues.mark_used(row.id, evidence="test")
        # Clear the ring so the signature check cannot be what stops it.
        self.kv.d.clear()
        res = self._run(mems).run()
        self.assertEqual(res["drafted"], 0)
        self.assertTrue(res["already_pooled"])

    def test_force_next_ignores_what_the_pool_holds(self) -> None:
        mems = [_Mem(2, "I've been feeling restless", _aged(30))]
        self._run(mems).run()
        self.kv.d.clear()
        worker = self._run(mems)
        worker.force_next()
        self.assertEqual(worker.run()["drafted"], 1)

    def test_no_pool_leaves_the_worker_on_plain_intervals(self) -> None:
        """The fallback that let the seven migrate one at a time."""
        worker = _worker(_FakeStore([]), self.kv, None)
        self.assertIsNone(
            worker.demand(now=datetime.now(timezone.utc), last_run_at=None)
        )


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import unittest
from dataclasses import replace
from unittest.mock import MagicMock, patch

import requests

from app.core.infra.settings import _parse_llm_provider, llm_provider_to_dict
from app.llm.lm_studio_lifecycle import LmStudioLifecycle, LmStudioModelSpec
from app.llm.openai_compatible_client import OpenAICompatibleClient
from app.core.infra.settings import OllamaSettings


def _response(body: dict, *, status: int = 200) -> MagicMock:
    response = MagicMock()
    response.json.return_value = body
    response.status_code = status
    response.content = b"{}"
    response.raise_for_status.return_value = None
    return response


class LmStudioLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.lifecycle = LmStudioLifecycle()
        self.addCleanup(self.lifecycle.stop)

    @staticmethod
    def _spec(*, roles: tuple[str, ...] = ("worker_default",)) -> LmStudioModelSpec:
        return LmStudioModelSpec(
            provider_id="mac",
            base_url="http://aiko-mac.local:1234/v1",
            api_key="secret",
            model="worker-model",
            context_length=32768,
            roles=roles,
            connect_timeout_seconds=4.0,
            load_timeout_seconds=700.0,
        )

    @patch("app.llm.lm_studio_lifecycle.requests.post")
    @patch("app.llm.lm_studio_lifecycle.requests.get")
    def test_loads_missing_model_with_native_timeouts_and_context(
        self, get: MagicMock, post: MagicMock,
    ) -> None:
        get.return_value = _response({"data": []})
        post.return_value = _response({
            "instance_id": "worker-instance",
            "load_time_seconds": 12.5,
            "load_config": {"context_length": 32768},
        })
        self.lifecycle.reconcile([self._spec()])
        self.lifecycle.before_inference("mac", "worker-model")
        status = self.lifecycle.provider_status("mac")
        self.assertEqual(status["status"], "ready")
        self.assertEqual(status["models"][0]["instance_id"], "worker-instance")
        get.assert_called_once_with(
            "http://aiko-mac.local:1234/api/v1/models",
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer secret",
            },
            timeout=(4.0, 30.0),
        )
        self.assertEqual(post.call_args.kwargs["timeout"], (4.0, 700.0))
        self.assertEqual(post.call_args.kwargs["json"]["context_length"], 32768)

    @patch("app.llm.lm_studio_lifecycle.requests.post")
    @patch("app.llm.lm_studio_lifecycle.requests.get")
    def test_models_are_loaded_sequentially(
        self, get: MagicMock, post: MagicMock,
    ) -> None:
        events: list[str] = []
        get.side_effect = lambda *args, **kwargs: (
            events.append("get") or _response({"data": []})
        )
        post.side_effect = lambda *args, **kwargs: (
            events.append(f"post:{kwargs['json']['model']}")
            or _response({"load_config": {"context_length": 32768}})
        )
        second = replace(self._spec(), model="policy-model")
        self.lifecycle.reconcile([self._spec(), second])
        self.lifecycle.before_inference("mac", "policy-model")
        self.assertEqual(
            events,
            ["get", "post:worker-model", "get", "post:policy-model"],
        )

    @patch("app.llm.lm_studio_lifecycle.requests.post")
    @patch("app.llm.lm_studio_lifecycle.requests.get")
    def test_deduplicates_same_model_and_merges_roles(
        self, get: MagicMock, post: MagicMock,
    ) -> None:
        get.return_value = _response({
            "models": [{
                "key": "worker-model",
                "loaded_instances": [{
                    "id": "worker-instance",
                    "config": {"context_length": 32768},
                }],
            }],
        })
        self.lifecycle.reconcile([
            self._spec(roles=("worker_default",)),
            self._spec(roles=("live_policy",)),
        ])
        self.lifecycle.before_inference("mac", "worker-model")
        rows = self.lifecycle.status()["models"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["roles"], ["live_policy", "worker_default"])
        self.assertEqual(rows[0]["instance_id"], "worker-instance")
        post.assert_not_called()

    @patch("app.llm.lm_studio_lifecycle.requests.post")
    @patch("app.llm.lm_studio_lifecycle.requests.get")
    def test_falls_back_to_legacy_v0_state_endpoint(
        self, get: MagicMock, post: MagicMock,
    ) -> None:
        missing = _response({}, status=404)
        loaded = _response({
            "data": [{"id": "worker-model", "state": "loaded"}],
        })
        get.side_effect = [missing, loaded]
        self.lifecycle.reconcile([self._spec()])
        self.lifecycle.before_inference("mac", "worker-model")
        self.assertEqual(self.lifecycle.provider_status("mac")["status"], "ready")
        self.assertEqual(
            get.call_args_list[1].args[0],
            "http://aiko-mac.local:1234/api/v0/models",
        )
        post.assert_not_called()

    @patch("app.llm.lm_studio_lifecycle.requests.post")
    @patch("app.llm.lm_studio_lifecycle.requests.get")
    def test_context_mismatch_is_a_persistent_failure(
        self, get: MagicMock, post: MagicMock,
    ) -> None:
        get.return_value = _response({"data": []})
        post.return_value = _response({
            "load_config": {"context_length": 16384},
        })
        self.lifecycle.reconcile([self._spec()])
        with self.assertRaisesRegex(RuntimeError, "loaded context"):
            self.lifecycle.before_inference("mac", "worker-model")
        self.assertEqual(self.lifecycle.provider_status("mac")["status"], "failed")
        self.assertEqual(post.call_count, 1)

    @patch("app.llm.lm_studio_lifecycle._MAX_ATTEMPTS", 1)
    @patch("app.llm.lm_studio_lifecycle.requests.get")
    def test_unreachable_host_is_reported_without_blocking_reconcile(
        self, get: MagicMock,
    ) -> None:
        get.side_effect = requests.ConnectTimeout("offline")
        self.lifecycle.reconcile([self._spec()])
        with self.assertRaisesRegex(RuntimeError, "offline"):
            self.lifecycle.before_inference("mac", "worker-model")
        self.assertEqual(
            self.lifecycle.provider_status("mac")["status"], "unreachable",
        )

    @patch("app.llm.lm_studio_lifecycle.requests.get")
    def test_shutdown_prevents_new_reconcile_work(self, get: MagicMock) -> None:
        self.lifecycle.stop()
        self.lifecycle.reconcile([self._spec()])
        get.assert_not_called()
        self.assertEqual(self.lifecycle.status()["models"], [])


class OpenAICompatiblePreflightTests(unittest.TestCase):
    def test_preflight_runs_before_inference_transport(self) -> None:
        client = OpenAICompatibleClient(
            OllamaSettings(base_url="http://127.0.0.1:11434"),
            base_url="http://aiko-mac.local:1234/v1",
            model="worker-model",
        )
        preflight = MagicMock(side_effect=RuntimeError("model is loading"))
        client.set_model_preflight(preflight)
        with patch("app.llm.openai_compatible_client.requests.post") as post:
            with self.assertRaisesRegex(RuntimeError, "model is loading"):
                client.chat_with_tools([{"role": "user", "content": "hi"}])
        preflight.assert_called_once_with("worker-model")
        post.assert_not_called()


class LmStudioSettingsTests(unittest.TestCase):
    def test_provider_prewarm_settings_round_trip_and_clamp(self) -> None:
        provider = _parse_llm_provider({
            "id": "mac",
            "kind": "openai_compatible",
            "base_url": "http://aiko-mac.local:1234/v1",
            "dialect": "lm_studio",
            "prewarm": {
                "enabled": True,
                "roles": ["worker_default", "live_policy"],
                "connect_timeout_seconds": 0.1,
                "load_timeout_seconds": 99999,
                "required": True,
                "max_parallel_loads": 8,
            },
        })
        self.assertIsNotNone(provider)
        assert provider is not None
        self.assertEqual(provider.dialect, "lm_studio")
        self.assertEqual(provider.prewarm.connect_timeout_seconds, 1.0)
        self.assertEqual(provider.prewarm.load_timeout_seconds, 3600.0)
        self.assertEqual(provider.prewarm.max_parallel_loads, 1)
        payload = llm_provider_to_dict(provider)
        self.assertEqual(payload["dialect"], "lm_studio")
        self.assertTrue(payload["prewarm"]["enabled"])


if __name__ == "__main__":
    unittest.main()

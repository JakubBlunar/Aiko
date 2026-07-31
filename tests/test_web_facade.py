"""Unit tests for the public surface ``app/web/`` uses on the controller.

Deliberately built on hand-written fakes rather than ``MagicMock``. The reason
this facade exists is that the web tests mock the whole session, and a mock
answers every attribute name and records every call as a success -- so a
rename, a typo, or a subsystem that silently never got told about a settings
change all pass. The fakes here only implement what really exists, and record
what they were actually asked to do.

The ``set_*`` methods carry the interesting behaviour: each writes settings
*and* pushes the value into a live subsystem that caches it. A settings-only
write is the bug these guard against -- it looks correct in the UI and reverts
on restart.
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from app.core.session.web_facade_mixin import (
    TaskHandles,
    WebFacadeMixin,
    WorkerUnavailable,
)


class _Recorder:
    """Records ``update_runtime`` kwargs so a missed sync is visible."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def update_runtime(self, **kwargs: object) -> None:
        self.calls.append(dict(kwargs))


class _Boom:
    """A subsystem that raises. Sync failures must not fail the request."""

    def update_runtime(self, **kwargs: object) -> None:
        raise RuntimeError("subsystem is unhappy")

    def set_grounding_line_mode(self, mode: str) -> None:
        raise RuntimeError("subsystem is unhappy")


class _Worker:
    def __init__(self, result: object = None) -> None:
        self.result = result
        self.calls: list[dict[str, object]] = []

    def run(self, **kwargs: object) -> object:
        self.calls.append(dict(kwargs))
        return self.result


class _ChatDb:
    def __init__(self) -> None:
        self.deleted: list[str] = []
        self.calls: list[tuple[str, tuple, dict]] = []

    def list_sessions(self) -> list[dict[str, str]]:
        return [{"id": "default:main"}]

    def get_messages(self, session_id: str, limit: int = 200) -> list[str]:
        self.calls.append(("get_messages", (session_id,), {"limit": limit}))
        return ["recent"]

    def get_messages_before(
        self, session_id: str, before_id: int, limit: int = 200,
    ) -> list[str]:
        self.calls.append(
            ("get_messages_before", (session_id,), {"before_id": before_id, "limit": limit}),
        )
        return ["older"]

    def delete_session(self, session_id: str) -> None:
        self.deleted.append(session_id)


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        audio=SimpleNamespace(earcons_enabled=True),
        agent=SimpleNamespace(
            proactive_cooldown_seconds=120.0,
            proactive_cooldown_seconds_typed=600.0,
            shared_moments_min_turn_gap=5,
            shared_moments_cooldown_seconds=300.0,
            grounding_line_mode="auto",
        ),
    )


class _Host(WebFacadeMixin):
    """Minimal stand-in for the controller: only the attributes in play."""

    def __init__(self, **attrs: object) -> None:
        self._settings = _settings()
        self._user_id = "default"
        self._chat_db = _ChatDb()
        self.notified: list[tuple[str, str, int | None]] = []
        for key, value in attrs.items():
            setattr(self, key, value)

    def _notify_message(
        self, speaker: str, text: str, message_id: int | None = None,
    ) -> None:
        self.notified.append((speaker, text, message_id))

    def _weather_public_snapshot(self) -> dict[str, str]:
        return {"summary": "clear"}


class ConfigAccessorTests(unittest.TestCase):
    def test_settings_is_the_live_object(self) -> None:
        host = _Host()
        self.assertIs(host.settings, host._settings)

    def test_user_id_falls_back_when_blank(self) -> None:
        """A blank user id would split REST rows from background task rows."""
        for value in ("", "   ", None):
            with self.subTest(value=value):
                host = _Host(_user_id=value)
                self.assertEqual(host.user_id, "default")

    def test_missing_chat_model_defaults_before_llm_wiring(self) -> None:
        """A client can connect before the model probe has run."""
        host = _Host()
        self.assertEqual(host.missing_chat_model, "")
        host._missing_chat_model = "qwen3.5:9b"
        self.assertEqual(host.missing_chat_model, "qwen3.5:9b")


class EarconToggleTests(unittest.TestCase):
    def test_writes_settings_syncs_player_and_persists(self) -> None:
        player = SimpleNamespace(enabled=True)
        host = _Host(_earcons=player)
        with mock.patch(
            "app.core.session.web_facade_mixin.persist_user_overrides",
        ) as persist:
            host.set_earcons_enabled(False)
        self.assertFalse(host._settings.audio.earcons_enabled)
        self.assertFalse(player.enabled)
        persist.assert_called_once_with({"audio": {"earcons_enabled": False}})

    def test_survives_a_missing_player(self) -> None:
        host = _Host()
        with mock.patch("app.core.session.web_facade_mixin.persist_user_overrides"):
            host.set_earcons_enabled(False)
        self.assertFalse(host._settings.audio.earcons_enabled)

    def test_persist_failure_does_not_propagate(self) -> None:
        """Losing the override file must not fail the settings request."""
        host = _Host(_earcons=SimpleNamespace(enabled=True))
        with mock.patch(
            "app.core.session.web_facade_mixin.persist_user_overrides",
            side_effect=OSError("disk full"),
        ):
            host.set_earcons_enabled(False)
        self.assertFalse(host._settings.audio.earcons_enabled)


class ProactiveRuntimeTests(unittest.TestCase):
    def test_each_cooldown_reaches_settings_and_the_director(self) -> None:
        director = _Recorder()
        host = _Host(_proactive=director)
        host.set_proactive_runtime(cooldown_seconds=90.0)
        self.assertEqual(host._settings.agent.proactive_cooldown_seconds, 90.0)
        self.assertEqual(director.calls, [{"cooldown_seconds": 90.0}])

        host.set_proactive_runtime(cooldown_seconds_typed=300.0)
        self.assertEqual(
            host._settings.agent.proactive_cooldown_seconds_typed, 300.0,
        )
        self.assertEqual(director.calls[-1], {"cooldown_seconds_typed": 300.0})

    def test_both_at_once_is_one_sync(self) -> None:
        director = _Recorder()
        host = _Host(_proactive=director)
        host.set_proactive_runtime(cooldown_seconds=90.0, cooldown_seconds_typed=300.0)
        self.assertEqual(
            director.calls,
            [{"cooldown_seconds": 90.0, "cooldown_seconds_typed": 300.0}],
        )

    def test_no_arguments_touches_nothing(self) -> None:
        director = _Recorder()
        host = _Host(_proactive=director)
        host.set_proactive_runtime()
        self.assertEqual(director.calls, [])
        self.assertEqual(host._settings.agent.proactive_cooldown_seconds, 120.0)

    def test_settings_still_land_when_the_director_is_absent(self) -> None:
        host = _Host()
        host.set_proactive_runtime(cooldown_seconds=90.0)
        self.assertEqual(host._settings.agent.proactive_cooldown_seconds, 90.0)

    def test_a_failing_director_does_not_lose_the_setting(self) -> None:
        host = _Host(_proactive=_Boom())
        host.set_proactive_runtime(cooldown_seconds=90.0)
        self.assertEqual(host._settings.agent.proactive_cooldown_seconds, 90.0)


class SharedMomentsRuntimeTests(unittest.TestCase):
    def test_both_knobs_sync_to_the_detector(self) -> None:
        detector = _Recorder()
        host = _Host(_moment_detector=detector)
        host.set_shared_moments_runtime(min_turn_gap=9, cooldown_seconds=45.0)
        self.assertEqual(host._settings.agent.shared_moments_min_turn_gap, 9)
        self.assertEqual(host._settings.agent.shared_moments_cooldown_seconds, 45.0)
        self.assertEqual(
            detector.calls, [{"min_turn_gap": 9, "cooldown_seconds": 45.0}],
        )

    def test_disabled_detector_still_records_the_value(self) -> None:
        """Shared moments off means no detector; enabling later must see this."""
        host = _Host(_moment_detector=None)
        host.set_shared_moments_runtime(min_turn_gap=9)
        self.assertEqual(host._settings.agent.shared_moments_min_turn_gap, 9)

    def test_no_arguments_touches_nothing(self) -> None:
        detector = _Recorder()
        host = _Host(_moment_detector=detector)
        host.set_shared_moments_runtime()
        self.assertEqual(detector.calls, [])


class GroundingLineModeTests(unittest.TestCase):
    def test_settings_and_assembler_agree(self) -> None:
        seen: list[str] = []
        assembler = SimpleNamespace(set_grounding_line_mode=seen.append)
        host = _Host(_prompt_assembler=assembler)
        host.set_grounding_line_mode("always")
        self.assertEqual(host._settings.agent.grounding_line_mode, "always")
        self.assertEqual(seen, ["always"])

    def test_a_failing_assembler_does_not_lose_the_setting(self) -> None:
        host = _Host(_prompt_assembler=_Boom())
        host.set_grounding_line_mode("always")
        self.assertEqual(host._settings.agent.grounding_line_mode, "always")


class HistoryAccessorTests(unittest.TestCase):
    def test_list_sessions_passes_through(self) -> None:
        self.assertEqual(_Host().list_sessions(), [{"id": "default:main"}])

    def test_default_read_is_the_most_recent_page(self) -> None:
        host = _Host()
        self.assertEqual(host.get_session_messages("s", limit=50), ["recent"])
        self.assertEqual(
            host._chat_db.calls[-1], ("get_messages", ("s",), {"limit": 50}),
        )

    def test_before_id_switches_to_keyset_pagination(self) -> None:
        host = _Host()
        self.assertEqual(
            host.get_session_messages("s", limit=50, before_id=42), ["older"],
        )
        self.assertEqual(
            host._chat_db.calls[-1],
            ("get_messages_before", ("s",), {"before_id": 42, "limit": 50}),
        )

    def test_before_id_zero_is_honoured_not_treated_as_absent(self) -> None:
        """``0`` is falsy; an ``if before_id`` check here would page wrongly."""
        host = _Host()
        host.get_session_messages("s", before_id=0)
        self.assertEqual(host._chat_db.calls[-1][0], "get_messages_before")

    def test_delete_session(self) -> None:
        host = _Host()
        host.delete_session("default:old")
        self.assertEqual(host._chat_db.deleted, ["default:old"])


class TaskHandleTests(unittest.TestCase):
    def test_handles_are_bundled_when_tasks_are_on(self) -> None:
        host = _Host(
            _task_store="store",
            _task_orchestrator="orch",
            _task_event_store="events",
            _task_input_store="inputs",
        )
        handles = host.tasks
        self.assertIsInstance(handles, TaskHandles)
        self.assertTrue(handles.enabled)
        self.assertEqual(handles.orchestrator, "orch")
        self.assertEqual(handles.event_store, "events")
        self.assertEqual(handles.input_store, "inputs")

    def test_disabled_tasks_report_not_enabled(self) -> None:
        """``tasks_enabled=False`` nulls two handles and never creates the others."""
        host = _Host(_task_store=None, _task_orchestrator=None)
        handles = host.tasks
        self.assertFalse(handles.enabled)
        self.assertIsNone(handles.event_store)
        self.assertIsNone(handles.input_store)


class TurnPlumbingTests(unittest.TestCase):
    def test_notify_user_message_forwards_the_message_id(self) -> None:
        host = _Host()
        host.notify_user_message("You", "hi", 7)
        self.assertEqual(host.notified, [("You", "hi", 7)])

    def test_notify_user_message_defaults_the_message_id(self) -> None:
        host = _Host()
        host.notify_user_message("Assistant", "hello")
        self.assertEqual(host.notified, [("Assistant", "hello", None)])

    def test_request_turn_stop_reaches_the_runner(self) -> None:
        stopped: list[bool] = []
        host = _Host(_turn_runner=SimpleNamespace(
            request_stop=lambda: stopped.append(True),
        ))
        host.request_turn_stop()
        self.assertEqual(stopped, [True])

    def test_request_turn_stop_before_a_runner_exists(self) -> None:
        """A stop can arrive from the UI before the first turn built a runner."""
        _Host().request_turn_stop()  # must not raise

    def test_weather_snapshot_passes_through(self) -> None:
        self.assertEqual(_Host().weather_public_snapshot(), {"summary": "clear"})


class OnDemandWorkerTests(unittest.TestCase):
    def test_each_worker_runs_and_returns_its_result(self) -> None:
        cases = [
            ("_curiosity_seed_worker", "run_curiosity_seed_worker_now", {}),
            ("_goal_worker", "run_goal_worker_now", {}),
            (
                "_concept_synthesis_worker",
                "run_concept_synthesis_worker_now",
                {"force": True},
            ),
        ]
        for attr, method, expected_kwargs in cases:
            with self.subTest(method=method):
                worker = _Worker({"made": 2})
                host = _Host(**{attr: worker})
                self.assertEqual(getattr(host, method)(), {"made": 2})
                self.assertEqual(worker.calls, [expected_kwargs])

    def test_unavailable_worker_is_distinguishable_from_a_failure(self) -> None:
        """Routes answer 503 for this and 500 for a worker that raised."""
        host = _Host()
        for method in (
            "run_curiosity_seed_worker_now",
            "run_goal_worker_now",
            "run_concept_synthesis_worker_now",
        ):
            with self.subTest(method=method):
                with self.assertRaises(WorkerUnavailable):
                    getattr(host, method)()

    def test_empty_result_normalises_to_a_dict(self) -> None:
        """``None`` from a worker must not be confused with "unavailable"."""
        host = _Host(_goal_worker=_Worker(None))
        self.assertEqual(host.run_goal_worker_now(), {})

    def test_worker_failure_propagates(self) -> None:
        class _Angry:
            def run(self, **kwargs: object) -> object:
                raise ValueError("llm exploded")

        host = _Host(_goal_worker=_Angry())
        with self.assertRaises(ValueError):
            host.run_goal_worker_now()


if __name__ == "__main__":
    unittest.main()

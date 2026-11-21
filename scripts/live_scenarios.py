"""Replay a JSON Live mode scenario with scripted policy or local Ollama."""
from __future__ import annotations

import argparse
from collections import Counter
import ipaddress
import json
from pathlib import Path
import sys
from urllib.parse import urlsplit

import requests

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.infra.settings import LLM_ROLE_LIVE_POLICY, find_provider, load_settings
from app.core.live.controller import USER_INTENT_KINDS
from app.core.live.micro_utterance import MICRO_DELIVERY, MICRO_INTENTS
from app.core.live.simulation import run_live_scenario
from app.llm.ollama_client import OllamaClient


def loopback_url(value: str) -> str:
    """Return a numeric loopback URL so DNS cannot route a policy call elsewhere."""
    url = urlsplit(value)
    host = url.hostname or ""
    if host == "localhost":
        host = "127.0.0.1"
    try:
        local = ipaddress.ip_address(host).is_loopback
    except ValueError:
        local = False
    if not local or url.scheme not in {"http", "https"} or url.username is not None:
        raise ValueError("Live scenarios require an HTTP(S) loopback Ollama endpoint")
    if url.query or url.fragment:
        raise ValueError("The local Ollama base URL cannot have a query or fragment")
    address = f"[{host}]" if ":" in host else host
    port = f":{url.port}" if url.port is not None else ""
    return f"{url.scheme}://{address}{port}{url.path}".rstrip("/")


class LoopbackPolicyClient(OllamaClient):
    def __init__(self, settings, base_url: str, session: requests.Session) -> None:
        super().__init__(settings, base_url=loopback_url(base_url))
        self._session = session
        self._session.trust_env = False

    def _post_structured_chat(self, url: str, **kwargs) -> requests.Response:
        response = self._session.post(loopback_url(url), allow_redirects=False, **kwargs)
        _reject_redirect(response)
        return response


def _reject_redirect(response: requests.Response) -> None:
    if 300 <= response.status_code < 400:
        response.close()
        raise requests.HTTPError("Local policy endpoint returned a redirect")


def require_local_model(session: requests.Session, base_url: str, model: str) -> None:
    with session.get(
        f"{loopback_url(base_url)}/api/tags", allow_redirects=False, timeout=5,
    ) as response:
        _reject_redirect(response)
        response.raise_for_status()
        models = response.json()["models"]
    canonical = model if ":" in model.rsplit("/", 1)[-1] else f"{model}:latest"
    for item in models:
        if item.get("name") == canonical or item.get("model") == canonical:
            if item.get("remote_model") or item.get("remote_host") or not item.get("size"):
                raise ValueError("The configured Live policy model is not a local Ollama model")
            return
    raise ValueError(f"Ollama does not list {canonical!r} as an installed model")


def run_local(scenario: dict) -> dict:
    settings = load_settings(read_only=True)
    route = settings.llm.routes.get(LLM_ROLE_LIVE_POLICY)
    provider = find_provider(settings.llm, route.provider_id) if route else None
    if provider is None or provider.kind != "ollama":
        raise ValueError("The configured live_policy route must use local Ollama")
    with requests.Session() as session:
        client = LoopbackPolicyClient(settings.ollama, provider.base_url, session)
        require_local_model(session, provider.base_url, route.model)
        return run_live_scenario(scenario, policy_client=client, route=route)


def summarize(report: dict) -> str:
    lines = [f"{report['name']} ({report['policy']})"]
    for step in report["steps"]:
        choices = []
        for decision in step["decisions"]:
            detail = f"{decision['intent'] or 'no proposal'} ({decision['reason']})"
            if decision["selected_subject"]:
                detail += f" for {decision['selected_subject']}"
            choices.append(detail)
        lines.append(
            f"{step['at_ms'] / 1000:8.3f}s  {step['label']}  "
            f"{', '.join(choices) or 'no policy decision'}  speech={step['speech_budget']}"
        )
        if step["queued_wakes"]:
            topics = [
                wake["subject"] or "(no cue subject)" for wake in step["queued_wakes"]
            ]
            lines.append("           queued topics " + ", ".join(topics))
    for item in report["dispatches"]:
        lines.append(f"Dispatch {item['reason']}  {item['speech_act']}  {item['subject']}")
    for item in report["spoken"]:
        lines.append(f"{item['delivery']}  {item['text']}")
    for failure in report["expectation_failures"]:
        lines.append(f"FAIL {failure}")
    return "\n".join(lines)


def trial_metrics(report: dict) -> dict:
    main_requests = []
    micro_requests = 0
    floor_requests = 0
    eligible_decisions = 0
    decision_rejections: Counter[str] = Counter()
    reactions: Counter[str] = Counter()
    queued_interruptions: Counter[str] = Counter()
    for step in report["steps"]:
        event = step["event"]
        decisions = step["decisions"]
        decision_rejections.update(
            row["reason"] for row in decisions if row["reason"] != "ok"
        )
        if event in {"user.voice_start", "user.speech_final"}:
            if step["queued_before"]:
                queued_interruptions[event] += 1
            reactions.update(f"{event}/{row['intent'] or 'no proposal'}" for row in decisions)
            if not decisions:
                reactions[f"{event}/no decision"] += 1
        if event in USER_INTENT_KINDS:
            continue
        proposal = step["last_policy"].get("proposal") or {}
        delivery = (proposal.get("arguments") or {}).get("delivery")
        for row in decisions:
            if row["eligible_urge_ids"]:
                eligible_decisions += 1
            main = row["intent"] == "request_main_speech"
            micro = row["intent"] in MICRO_INTENTS and delivery == MICRO_DELIVERY
            if main:
                main_requests.append(row)
            if micro:
                micro_requests += 1
            if (main or micro) and (row["floor_owner"] == "user" or row["typing_active"]):
                floor_requests += 1
    topic = "(no selected subject)"
    return {
        "policy_calls": report["steps"][-1]["policy_calls"],
        "policy_failures": report["steps"][-1]["policy_failures"],
        "decisions_with_eligible_urges": eligible_decisions,
        "main_requests": len(main_requests),
        "main_admitted": sum(bool(row.get("main_wake_admitted")) for row in main_requests),
        "micro_requests": micro_requests,
        "micro_delivered": sum(row["delivery"] == "Assistant (live)" for row in report["spoken"]),
        "speech_requests_on_user_floor": floor_requests,
        "decision_rejections": dict(decision_rejections),
        "requested_topics": dict(Counter(
            row["selected_subject"] or topic for row in main_requests
        )),
        "admitted_topics": dict(Counter(
            row["selected_subject"] or topic for row in main_requests
            if row.get("main_wake_admitted")
        )),
        "main_rejections": dict(Counter(
            row.get("main_wake_reject_reason") or row["reason"]
            for row in main_requests if not row.get("main_wake_admitted")
        )),
        "dispatch_outcomes": dict(Counter(row["reason"] for row in report["dispatches"])),
        "interruption_reactions": dict(reactions),
        "interruptions_with_queued_speech": dict(queued_interruptions),
    }


def summarize_trials(reports: list[dict]) -> dict:
    metrics = [trial_metrics(report) for report in reports]
    totals = {}
    for key, value in metrics[0].items():
        if isinstance(value, dict):
            counts: Counter[str] = Counter()
            for trial in metrics:
                counts.update(trial[key])
            totals[key] = dict(counts)
        else:
            totals[key] = sum(trial[key] for trial in metrics)
    return {
        "name": reports[0]["name"], "policy": reports[0]["policy"],
        "trials": len(reports), "trials_with_main_request": sum(
            bool(trial["main_requests"]) for trial in metrics
        ),
        "per_trial": metrics, "totals": totals,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scenario", type=Path)
    parser.add_argument(
        "--local", action="store_true", help="Call the configured local Live model.",
    )
    parser.add_argument("--json", action="store_true", help="Print the complete decision trace.")
    parser.add_argument("--repeat", type=int, default=1, help="Run independent trials in sequence.")
    args = parser.parse_args()
    if args.repeat < 1:
        parser.error("--repeat must be positive")
    try:
        scenario = json.loads(args.scenario.read_text(encoding="utf-8"))
        run = run_local if args.local else run_live_scenario
        reports = [run(scenario) for _ in range(args.repeat)]
    except (OSError, ValueError, TypeError, KeyError) as exc:
        parser.exit(2, f"Scenario error: {exc}\n")
    if args.repeat == 1:
        print(json.dumps(reports[0], indent=2) if args.json else summarize(reports[0]))
    else:
        summary = summarize_trials(reports)
        if args.json:
            print(json.dumps({"summary": summary, "runs": reports}, indent=2))
        else:
            print(f"{summary['name']} ({summary['policy']}) over {args.repeat} trials")
            for number, report in enumerate(reports, 1):
                print(f"\nTrial {number}\n{summarize(report)}")
            print(f"\nTotals\n{json.dumps(summary['totals'], indent=2)}")
    if any(
        report["expectation_failures"] or report["steps"][-1]["policy_failures"]
        for report in reports
    ):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

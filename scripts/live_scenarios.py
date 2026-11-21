"""Replay a JSON Live mode scenario with scripted policy or local Ollama."""
from __future__ import annotations

import argparse
import ipaddress
import json
from pathlib import Path
import sys
from urllib.parse import urlsplit

import requests

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.infra.settings import LLM_ROLE_LIVE_POLICY, find_provider, load_settings
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scenario", type=Path)
    parser.add_argument(
        "--local", action="store_true", help="Call the configured local Live model.",
    )
    parser.add_argument("--json", action="store_true", help="Print the complete decision trace.")
    args = parser.parse_args()
    try:
        scenario = json.loads(args.scenario.read_text(encoding="utf-8"))
        report = run_local(scenario) if args.local else run_live_scenario(scenario)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        parser.exit(2, f"Scenario error: {exc}\n")
    print(json.dumps(report, indent=2) if args.json else summarize(report))
    if report["expectation_failures"] or report["steps"][-1]["policy_failures"]:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Disposable SQLite and a traced, local-only worker for fictional workflow replays."""
from contextlib import closing
import json
import multiprocessing
from pathlib import Path
import re
from tempfile import TemporaryDirectory

import requests

from app.core.infra.chat_database import ChatDatabase
from app.core.infra.settings import OllamaSettings
from scripts.live_scenarios import LoopbackPolicyClient, require_local_model


class ReplayWorker(LoopbackPolicyClient):
    def __init__(self, settings, base, session):
        super().__init__(settings, base, session)
        self.usage_totals = {"calls": 0, "input": 0, "generated": 0, "planner_calls": 0}

    def chat_json(self, messages, *, options=None, **kwargs):
        raw, usage = super().chat_json(
            messages, options={**(options or {}), "temperature": 0, "seed": 71}, **kwargs,
        )
        trace = {"surface": kwargs.get("surface"), "input": usage.prompt_tokens,
                 "generated": usage.completion_tokens}
        self.usage_totals["calls"] += 1
        self.usage_totals["input"] += trace["input"]
        self.usage_totals["generated"] += trace["generated"]
        if trace["surface"] == "workflow_planner":
            self.usage_totals["planner_calls"] += 1
            menu = messages[1]["content"].partition("SKILLS:\n")[2].partition("\n\n")[0]
            trace["menu"] = re.findall(r"^- ([\w:-]+):", menu, re.M)
            try:
                decision = json.loads(raw)
                trace["action"] = decision.get("action") or decision.get("resolution")
                if "resolution" in decision:
                    trace["choice"] = decision
                    trace["think"] = kwargs.get("think")
                if trace["action"] in {"discover", "ask_user", "inspect_result"}:
                    trace["args"] = decision.get("args")
            except (ValueError, AttributeError):
                trace["action"] = "malformed"
        print(json.dumps(trace), flush=True)
        return raw, usage


def _process(run, tmp, model, args):
    base = "http://127.0.0.1:11434"
    with requests.Session() as session:
        client = ReplayWorker(OllamaSettings(base, chat_model=model, context_window=65536),
                              base, session)
        require_local_model(session, base, model)
        db = ChatDatabase(Path(tmp) / "work.db")
        with closing(db._get_conn()):
            passed = run(db, client, model, *args)
            print(json.dumps({"usage_totals": client.usage_totals}), flush=True)
            raise SystemExit(0 if passed else 1)


def run_isolated(run, model, prefix, *args):
    scratch = Path(__file__).resolve().parents[1] / ".tmp-tests"
    scratch.mkdir(exist_ok=True)
    with TemporaryDirectory(dir=scratch, prefix=prefix) as tmp:
        # Process exit releases worker threads' SQLite handles before Windows cleanup.
        proc = multiprocessing.get_context("spawn").Process(target=_process,
                                                            args=(run, tmp, model, args))
        proc.start()
        try:
            proc.join()
        finally:
            if proc.is_alive():
                proc.terminate()
                proc.join()
        return proc.exitcode

"""T5 synthetic continuation probes using the real prompt assembler, isolated from install state.

This is a bounded companion-content evaluation, not a free-running session or an
independent judge. Scripted prefixes and frozen lexical checks make failures
inspectable; interpretation/style still require blinded human review.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import re
import sys
import tempfile
import time
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

CORPUS = ROOT / "tests/fixtures/social_reasoning_t5.json"


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def score_response(text, checks):
    """Observable text indicators only; no claim of semantic ground truth."""
    return {
        "required": [any(re.search(pattern, text, re.I) for pattern in alternatives)
                     for alternatives in checks.get("required", [])],
        "forbidden": [bool(re.search(pattern, text, re.I))
                      for pattern in checks.get("forbidden", [])],
        "apologies": len(re.findall(r"\b(sorry|apolog\w*)\b", text, re.I)),
        "question_marks": text.count("?"),
        "human_review": "pending",
    }


@contextmanager
def configured_clients(config_path, timeout):
    # load_settings() also migrates live config. Parse copies instead: this
    # opt-in evaluator must never rewrite settings, even on a legacy install.
    from app.core.infra.settings import (
        DEFAULT_CONFIG_PATH, _deep_merge, _parse_llm,
    )
    from app.llm.factory import ClientCache, build_client_for_route

    default = json.loads(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))
    user = json.loads(Path(config_path).read_text(encoding="utf-8"))
    settings = _parse_llm(_deep_merge(default, user)["llm"])
    settings.providers = [replace(provider, timeout_seconds=timeout)
                          for provider in settings.providers]
    cache = ClientCache()
    try:
        routes = {name: settings.routes[name] for name in ("main_chat", "worker_default")}
        clients = {name: build_client_for_route(cache, route=route, settings=settings)
                   for name, route in routes.items()}
        yield clients, routes
    finally:
        cache.shutdown()


def probe(client, route, case, checkpoint, scratch, *, observer_client=None,
          observer_route=None, prepare_only=False, observer_only=False, observer_prompt=None):
    from app.core.conversation.conversation_situation import ConversationSituationStore
    from app.core.conversation.conversation_situation_worker import ConversationSituationWorker
    from app.core.infra.chat_database import ChatDatabase
    from app.core.session.conversation_situation_mixin import ConversationSituationMixin
    from app.core.session.prompt_assembler import PromptAssembler

    db = ChatDatabase(scratch / "chat.db")
    try:
        for turn in case["turns"][:checkpoint]:
            db.add_message("synthetic", turn["role"], turn["text"])
        store = ConversationSituationStore(db)
        observer_stats = None
        if observer_client is not None:
            worker = ConversationSituationWorker(
                client=observer_client, chat_db=db, store=store,
                model=observer_route.model, world_snapshot_provider=lambda: {},
                judgment_shadow_enabled=False,
            )
            if observer_prompt is None:
                worker.run("synthetic")
            else:
                with patch("app.core.conversation.conversation_situation_worker._SYSTEM_PROMPT",
                           observer_prompt):
                    worker.run("synthetic")
            observer_stats = worker.stats("synthetic")
        if observer_only:
            return {"case": case["id"], "pair": case["pair"], "split": case["split"],
                    "checkpoint": checkpoint, "observer": observer_stats}

        class Host(ConversationSituationMixin):
            session_key = "synthetic"

        host = Host()
        host._chat_db = db
        host._conversation_situation_store = store
        host._settings = SimpleNamespace(agent=SimpleNamespace())
        assembler = PromptAssembler(db, continuity_max_messages=0)
        host._prompt_assembler = assembler
        # Both arms receive the production situation renderer. A baseline is
        # captured before the code change, not reconstructed by a new judge.
        assembler.set_inner_life_providers(
            conversation_situation=host._render_conversation_situation_block,
        )
        text = case["turns"][checkpoint]["text"]
        messages, telemetry = assembler.assemble_with_budget(
            "synthetic", text, context_window=route.context_window or 32768,
            response_budget=route.max_tokens,
        )
        if prepare_only:
            return {"case": case["id"], "pair": case["pair"], "split": case["split"],
                    "checkpoint": checkpoint, "messages": messages,
                    "system_chars": len(telemetry.system_prompt)}
        started = time.monotonic()
        first_token_ms = None
        pieces = []
        for piece in client.chat_stream(
            messages, model=route.model,
            options={"temperature": 0, "num_predict": route.max_tokens},
            think=False, surface="t5_social_evaluation",
        ):
            if piece and first_token_ms is None:
                first_token_ms = round((time.monotonic() - started) * 1000, 1)
            pieces.append(piece)
        answer = "".join(pieces)
        usage = getattr(client, "last_usage", None)
        return {
            "case": case["id"], "pair": case["pair"], "split": case["split"],
            "checkpoint": checkpoint, "response": answer,
            "checks": score_response(answer, case["checks"].get(str(checkpoint), {})),
            "latency_ms": round((time.monotonic() - started) * 1000, 1),
            "first_token_ms": first_token_ms,
            "prompt_tokens": getattr(usage, "prompt_tokens", None),
            "completion_tokens": getattr(usage, "completion_tokens", None),
            "done_reason": getattr(usage, "done_reason", None),
            "system_chars": len(telemetry.system_prompt),
            "system_hash": hashlib.sha256(telemetry.system_prompt.encode()).hexdigest(),
            "observer": observer_stats,
        }
    finally:
        db._get_conn().close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / "config/user.json")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--label", required=True)
    parser.add_argument("--observer", action="store_true")
    parser.add_argument("--observer-only", action="store_true",
                        help="Probe only the local observer's schema/evidence contracts")
    parser.add_argument("--observer-prompt-file", type=Path,
                        help="Optional frozen observer prompt for an explicit schema ablation")
    parser.add_argument("--prepare-only", action="store_true",
                        help="Save local prompt inputs without making any model calls")
    parser.add_argument("--split", choices=("regression", "heldout", "all"), default="all")
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--max-calls", type=int, default=48)
    parser.add_argument("--max-seconds", type=float, default=600)
    parser.add_argument("--timeout", type=int, default=45)
    args = parser.parse_args(argv)
    if min(args.repeats, args.max_calls, args.max_seconds, args.timeout) <= 0:
        parser.error("budgets must be positive")
    if args.observer_only:
        args.observer = True
    if args.observer_prompt_file and not args.observer:
        parser.error("--observer-prompt-file requires an observer run")
    if args.prepare_only and args.observer:
        parser.error("--prepare-only forbids observer model calls")
    from app.core.conversation import conversation_situation_worker as observer_module

    observer_prompt = (args.observer_prompt_file.read_text(encoding="utf-8")
                       if args.observer_prompt_file else observer_module._SYSTEM_PROMPT)
    corpus = json.loads(CORPUS.read_text(encoding="utf-8"))
    cases = [case for case in corpus["cases"]
             if args.split == "all" or case["split"] == args.split]
    report = {
        "version": corpus["version"], "label": args.label,
        "corpus_hash": digest(CORPUS),
        "persona_hash": digest(ROOT / "data/persona/aiko_companion.txt"),
        "handling_hash": digest(ROOT / "data/persona/conditional_handling.txt"),
        "observer_prompt_hash": hashlib.sha256(observer_prompt.encode()).hexdigest(),
        "code_hashes": {name: digest(ROOT / name) for name in (
            "app/core/conversation/conversation_situation_worker.py",
            "app/core/session/conversation_situation_mixin.py",
            "app/core/conversation/clarification_detector.py",
        )},
        "scope": "scripted continuation probes; no tools, memory, affect or relationship workers",
        "scoring": "frozen lexical indicators; human review pending; no aggregate style score",
        "rows": [], "stopped": "complete", "calls": 0,
    }
    started = time.monotonic()
    scratch_root = ROOT / ".tmp-tests"
    scratch_root.mkdir(exist_ok=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with (
        configured_clients(args.config, args.timeout) as (clients, routes),
        tempfile.TemporaryDirectory(prefix="social-eval-", dir=scratch_root) as scratch,
    ):
        report["routes"] = {name: {key: getattr(route, key) for key in (
            "provider_id", "model", "context_window", "max_tokens", "reasoning_effort",
        )} for name, route in routes.items()}
        for repeat in range(args.repeats):
            # Reverse order on alternating repeats for order-sensitive hosts.
            for case in cases if repeat % 2 == 0 else list(reversed(cases)):
                for checkpoint in case["checkpoints"]:
                    cost = (0 if args.prepare_only else
                            2 if args.observer and not args.observer_only else 1)
                    if (report["calls"] + cost > args.max_calls
                            or time.monotonic() - started >= args.max_seconds):
                        report["stopped"] = "budget"
                        break
                    report["calls"] += cost
                    try:
                        row = probe(
                            clients["main_chat"], routes["main_chat"], case, checkpoint,
                            Path(scratch), observer_client=(
                                clients["worker_default"] if args.observer else None
                            ), observer_route=routes["worker_default"],
                            prepare_only=args.prepare_only,
                            observer_only=args.observer_only,
                            observer_prompt=observer_prompt,
                        )
                    except Exception as error:
                        row = {"case": case["id"], "checkpoint": checkpoint,
                               "error": type(error).__name__}
                    report["rows"].append({"repeat": repeat, **row})
                    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2)
                                           + "\n", encoding="utf-8", newline="\n")
                    print(case["id"], checkpoint, row.get("error", "completed"), flush=True)
                    # Each probe owns a fresh database, including on failed calls.
                    for artifact in Path(scratch).glob("chat.db*"):
                        artifact.unlink()
                if report["stopped"] == "budget":
                    break
            if report["stopped"] == "budget":
                break
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                           encoding="utf-8", newline="\n")
    return 1 if any("error" in row for row in report["rows"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())

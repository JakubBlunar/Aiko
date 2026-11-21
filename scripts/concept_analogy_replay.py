"""Replay source-backed analogies on fictional, held-out questions."""

import argparse
import json
import sys
from contextlib import closing
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.infra import timephrase
from app.core.infra.chat_database import ChatDatabase
from app.core.infra.settings import (
    LLM_ROLE_WORKER_DEFAULT,
    find_provider,
    load_settings,
    transport_for_provider,
)
from app.core.memory.memory_store import MemoryStore
from app.core.memory.observation_receipts import admit_observation, longitudinal_sources_current
from scripts.live_scenarios import LoopbackPolicyClient, require_local_model


# Each earlier episode is a user report. Later questions are never stored in
# the evidence database or shown during admission.
_EPISODES = {
    "wood_finish": (
        "When the finish looked different on each type of wood, I stained a spare offcut.",
        "When the finish looked different on each type of wood",
        "I stained a spare offcut",
        "The offcut helped me check the dry color before staining the whole piece.",
    ),
    "printer_receipt": (
        "When I needed proof that I bought a printer, I looked up the order confirmation.",
        "When I needed proof that I bought a printer",
        "I looked up the order confirmation",
        "The confirmation helped me show the serial number and purchase date.",
    ),
    "photo_edit": (
        "When I wanted to try an irreversible photo effect, I edited a copy of the image.",
        "When I wanted to try an irreversible photo effect",
        "I edited a copy of the image",
        "Editing a copy helped me keep the original available when I disliked the effect.",
    ),
    "spreadsheet_layout": (
        "When I could not find a column in a spreadsheet, I zoomed out to view the full layout.",
        "When I could not find a column in a spreadsheet",
        "I zoomed out to view the full layout",
        "The wider view helped me locate the column.",
    ),
}
# Topical reports for the new domains. These belong only to the baseline;
# the transfer selector sees the same source pool as in the original trial.
_HELD_OUT_TOPIC_EPISODES = {
    "pottery_studio": (
        "When our pottery studio firing was delayed, I called to confirm the pickup time.",
        "When our pottery studio firing was delayed",
        "I called to confirm the pickup time",
        "The call helped me avoid a wasted trip to the studio.",
    ),
    "slides_layout": (
        "When the title on a slide kept moving out of alignment, I used the deck's master layout.",
        "When the title on a slide kept moving out of alignment",
        "I used the deck's master layout",
        "The master layout helped me keep the title aligned across the deck.",
    ),
}
_CASES = {
    "material_varies": (
        "I need to print wedding invitations on textured stock. Ink looks different on "
        "different paper, and I have spare sheets of the exact stock. What would you try?",
        "printer_receipt", "wood_finish",
    ),
    "irreversible_change": (
        "I want to try a spreadsheet conversion that permanently drops formulas. "
        "I can duplicate the sheet and need to be able to go back. What should I do?",
        "spreadsheet_layout", "photo_edit",
    ),
    "no_spare_stock": (
        "Ink can behave differently on the textured paper for my invitations. I have "
        "exactly one sheet, it must stay untouched unless used for the final print, and "
        "I cannot get a matching spare. How can I reduce the risk?",
        "printer_receipt", None,
    ),
    "constraint_removed": (
        "The print shop finished and approved a proof on the exact invitation paper. "
        "I only need to send my friend the confirmed pickup time. What should I do?",
        "printer_receipt", None,
    ),
    "shared_words_only": (
        "I need proof of purchase for a secondhand printer. I lost the invoice. "
        "This is a paperwork problem. Where could I look?",
        "printer_receipt", None,
    ),
}
_HELD_OUT_CASES = {
    "glaze_matching_tile": (
        "I need to check the fired color of a glaze before glazing a mural. This glaze "
        "fires differently on each clay body and I have a spare tile from the same batch "
        "of clay. What would you try?",
        "pottery_studio", "wood_finish",
    ),
    "slides_copy": (
        "I want to try a slide converter that strips speaker notes. I need to keep the "
        "editable deck with all its notes. I can duplicate the deck before converting it. "
        "How should I proceed?",
        "slides_layout", "photo_edit",
    ),
    "glaze_unmatched_tile": (
        "I need to check the fired color of a glaze before glazing a mural. This glaze "
        "fires differently on each clay body. My only spare is a different clay body and "
        "I can't obtain a matching sample. Every tile from the mural must stay untouched "
        "until the final glazing. What can I do?",
        "pottery_studio", None,
    ),
    "glaze_unknown_tile": (
        "I need to check the fired color of a glaze before glazing a mural. This glaze "
        "fires differently on each clay body. I found a loose tile but I don't know which "
        "clay body it came from. What would you try?",
        "pottery_studio", None,
    ),
    "slides_no_copy": (
        "I want to try a slide converter that overwrites the input and strips speaker notes. "
        "I need to keep the editable deck with all its notes. Export and duplication of this "
        "locked deck are disabled. How should I proceed?",
        "slides_layout", None,
    ),
    "glaze_collect_only": (
        "Our mural's glaze was tested and approved. All firing is complete. I have to send "
        "a neighbor the confirmed studio pickup time. What should the message include?",
        "pottery_studio", None,
    ),
}


@dataclass(frozen=True)
class ReportedFact:
    name: str
    holds: bool
    quote: str


# Reviewed annotations for the fictional replay. These are independently
# checkable goals and resources, rather than expected source IDs.
_TRANSFER_REQUIRES = {
    "wood_finish": ("physical_check_pending", "matching_spare"),
    "printer_receipt": ("purchase_record_needed", "order_history_available"),
    "photo_edit": ("editable_original_needed", "separate_copy_available"),
    "spreadsheet_layout": ("field_search_needed", "zoomable_layout"),
}
_REVIEWED_FACTS = {
    "material_varies": (
        ReportedFact("physical_check_pending", True, "Ink looks different on different paper"),
        ReportedFact("matching_spare", True, "I have spare sheets of the exact stock"),
    ),
    "irreversible_change": (
        ReportedFact("editable_original_needed", True, "need to be able to go back"),
        ReportedFact("separate_copy_available", True, "I can duplicate the sheet"),
    ),
    "no_spare_stock": (
        ReportedFact("physical_check_pending", True,
                     "Ink can behave differently on the textured paper"),
        ReportedFact("matching_spare", False, "I cannot get a matching spare"),
    ),
    "constraint_removed": (
        ReportedFact("physical_check_pending", False, "finished and approved a proof"),
    ),
    "shared_words_only": (
        ReportedFact("purchase_record_needed", True, "I need proof of purchase"),
        ReportedFact("physical_check_pending", False, "This is a paperwork problem"),
    ),
    "glaze_matching_tile": (
        ReportedFact("physical_check_pending", True, "need to check the fired color"),
        ReportedFact("matching_spare", True, "a spare tile from the same batch of clay"),
    ),
    "slides_copy": (
        ReportedFact("editable_original_needed", True, "keep the editable deck with all its notes"),
        ReportedFact("separate_copy_available", True, "I can duplicate the deck before converting"),
    ),
    "glaze_unmatched_tile": (
        ReportedFact("physical_check_pending", True, "need to check the fired color"),
        ReportedFact("matching_spare", False, "I can't obtain a matching sample"),
    ),
    "glaze_unknown_tile": (
        ReportedFact("physical_check_pending", True, "need to check the fired color"),
    ),
    "slides_no_copy": (
        ReportedFact("editable_original_needed", True, "keep the editable deck with all its notes"),
        ReportedFact("separate_copy_available", False, "locked deck are disabled"),
    ),
    "glaze_collect_only": (
        ReportedFact("physical_check_pending", False, "glaze was tested and approved"),
    ),
}
_CONTEXT_LIMIT = 560
_SELECTION_TASK = (
    "Assess whether at most one earlier episode suggests a useful option for this new problem. "
    "Use a different topic, a shared practical goal and a current constraint that supports "
    "the same kind of action. Check the cost, availability and what the earlier user actually "
    "reported. "
)
_SELECT = _SELECTION_TASK + (
    "Shared words alone do not connect the situations. Give the decisive reason "
    "in one short sentence before choosing. Return a null choice when "
    "the earlier approach is blocked, the useful work has already happened, or its result "
    "cannot help with the current problem."
)
_SELECT_WITH_FIT = _SELECTION_TASK + (
    "Give the decisive reason in one short sentence. Set fit to blocked if the "
    "action needs a missing resource or would modify an item the user must preserve, even "
    "a corner of it. Set fit to irrelevant if the useful work is already done or the case "
    "does not help answer the question. Use transferable only when the action is applicable "
    "now. Identify the considered case by its number or use null if none is relevant."
)
_ANSWER = (
    "Give a brief practical answer to the user's current question. The context may contain a "
    "quoted earlier report. Verify that an example fits the current goal and constraints before "
    "using it. Suggest an action only if feasible here. Keep the current outcome open. "
    "Answer naturally and name an earlier situation only if it helps the user."
)
_SELECT_SCHEMA = {
    "type": "object",
    "properties": {
        "bridge": {"type": "string", "maxLength": 180},
        "candidate_id": {"type": ["integer", "null"]},
    },
    "required": ["candidate_id", "bridge"],
}
_SELECT_WITH_FIT_SCHEMA = {
    **_SELECT_SCHEMA,
    "properties": {
        "bridge": _SELECT_SCHEMA["properties"]["bridge"],
        "fit": {"type": "string", "enum": ["transferable", "blocked", "irrelevant"]},
        "candidate_id": _SELECT_SCHEMA["properties"]["candidate_id"],
    },
    "required": [*_SELECT_SCHEMA["required"], "fit"],
}
_ANSWER_SCHEMA = {
    "type": "object",
    "properties": {"reply": {"type": "string"}},
    "required": ["reply"],
}


def seed_episodes(db, store, *, held_out=False):
    records = {}
    episodes = {**_EPISODES, **_HELD_OUT_TOPIC_EPISODES} if held_out else _EPISODES
    for name, (opening, context, action, outcome) in episodes.items():
        session = f"analogy:{name}"
        report = opening + " " + outcome
        message_id = db.add_message(session, "user", report)
        row = db.get_message_row(message_id)
        observation = admit_observation(
            {facet: {"message_id": message_id, "quote": quote}
             for facet, quote in (("context", context), ("action", action), ("outcome", outcome))},
            [row], session_key=session, subject="user",
        )
        records[name] = store.add(
            report, "event", np.array([1.0, 0.0]), source_session=session,
            source_message_id=message_id, temporal_type="past_event", provenance="stated",
            metadata={"observation": observation}, skip_dedupe=True,
        )
    return records


def evidence_context(memory, store, now):
    """Project admitted facets whose exact source report still exists."""
    observation = memory.metadata.get("observation")
    if (not isinstance(observation, dict) or observation.get("subject") != "user"
            or not all(isinstance(observation.get(field), dict)
                       for field in ("context", "action", "outcome"))):
        return ""
    source = observation["context"]
    exceptions = observation.get("exceptions", [])
    sources = observation.get("sources", [])
    if not isinstance(exceptions, list) or not isinstance(sources, list):
        return ""
    facets = [source, observation["action"], observation["outcome"], *exceptions]
    if (
        any(not isinstance(facet, dict) or facet.get("context_only") or facet.get("role") != "user"
            or facet.get("message_id") != source.get("message_id")
            or facet not in sources for facet in facets)
        or not longitudinal_sources_current(memory, store)
    ):
        return ""
    header = timephrase.format_transcript(
        [{"role": "user", "content": source["quote"], "created_at": source["source_at"]}], now,
    )
    block = (
        f"Case {memory.id}\nContext: {header}\nAction: {observation['action']['quote']}\n"
        f"Reported result: {observation['outcome']['quote']}"
    )
    for exception in exceptions:
        block += f"\nException: {exception['quote']}"
    return block if len(block) <= _CONTEXT_LIMIT else ""


def check_preconditions(episode, question, facts):
    """Check a proposed transfer against independently reviewed, literal current evidence."""
    blocked, unknown = [], []
    for requirement in _TRANSFER_REQUIRES.get(episode, ("reviewed_requirements",)):
        reports = {
            fact.holds for fact in facts if fact.name == requirement
            and fact.quote.strip() and fact.quote in question
        }
        if reports == {False}:
            blocked.append(requirement)
        elif reports != {True}:
            unknown.append(requirement)
    return {"blocked": blocked, "unknown": unknown, "allowed": not (blocked or unknown)}


def replay(records, store, infer, *, as_of=None, require_fit=False, check_current=False,
           held_out=False):
    now = as_of or (timephrase.utcnow() + timedelta(weeks=3)).replace(
        hour=12, minute=0, second=0, microsecond=0,
    )
    anchor = timephrase.today_anchor(now)
    packets = {name: evidence_context(note, store, now) for name, note in records.items()}
    selection_prompt, schema = (_SELECT_WITH_FIT, _SELECT_WITH_FIT_SCHEMA) if require_fit else (
        _SELECT, _SELECT_SCHEMA,
    )
    results = {}
    cases = _HELD_OUT_CASES if held_out else _CASES
    for case, (question, plain_topic, expected) in cases.items():
        by_id = {note.id: name for name, note in records.items()
                 if name in _EPISODES and name != plain_topic and packets[name]}
        candidates = "\n\n".join(packets[name] for name in by_id.values())
        decision = infer(
            selection_prompt, f"{anchor}\nEarlier cases:\n{candidates}\nNew question: {question}",
            schema,
        )
        chosen = decision.get("candidate_id")
        considered = by_id.get(chosen) if type(chosen) is int else None
        fit = decision.get("fit")
        valid = (
            "candidate_id" in decision and isinstance(decision.get("bridge"), str)
            and (chosen is None or considered is not None)
        )
        if require_fit:
            valid = valid and fit in ("blocked", "irrelevant", "transferable") and (
                fit != "transferable" or considered is not None
            )
        selection = considered if valid and (not require_fit or fit == "transferable") else None
        preflight = None
        if selection is not None and check_current:
            preflight = check_preconditions(selection, question, _REVIEWED_FACTS.get(case, ()))
            if not preflight["allowed"]:
                selection = None
        sources = {"plain_topic": plain_topic, "analogy": selection}
        if held_out and expected is not None:
            sources["reviewed_source"] = expected
        arms = {}
        for name, episode in sources.items():
            evidence = packets.get(episode, "")
            prompt = f"{anchor}\nEarlier context:\n{evidence.ljust(_CONTEXT_LIMIT)}\n{question}"
            arms[name] = {"source": episode if evidence else None,
                          **infer(_ANSWER, prompt, _ANSWER_SCHEMA)}
        results[case] = {
            "expected_analogy": expected,
            "considered_case": considered,
            "fit": fit,
            "preflight": preflight,
            "selected_analogy": selection,
            "selection_matches": valid and selection == expected,
            "invalid_candidate": not valid,
            "selection_error": decision.get("error"),
            "bridge": decision.get("bridge"),
            "arms": arms,
        }
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=56)
    parser.add_argument("--held-out", action="store_true", help="run the fresh transfer cases")
    parser.add_argument(
        "--require-fit", action="store_true",
        help="compare with a separate blocked/irrelevant/transferable verdict",
    )
    parser.add_argument(
        "--check-current", action="store_true",
        help="veto transfers without independently reviewed current prerequisites",
    )
    args = parser.parse_args()
    settings = load_settings(read_only=True)
    route = settings.llm.routes.get(LLM_ROLE_WORKER_DEFAULT)
    provider = find_provider(settings.llm, route.provider_id) if route else None
    if provider is None or provider.kind != "ollama":
        parser.error("The configured worker_default route must use local Ollama")
    scratch = Path(__file__).resolve().parents[1] / ".tmp-tests"
    scratch.mkdir(exist_ok=True)
    with requests.Session() as session, TemporaryDirectory(prefix="l56-", dir=scratch) as trial:
        transport = transport_for_provider(provider, route=route)
        client = LoopbackPolicyClient(transport, provider.base_url, session)
        require_local_model(session, provider.base_url, route.model)

        def infer(system, prompt, schema):
            raw, usage = client.chat_json(
                [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
                model=route.model, json_schema=schema, timeout_seconds=120,
                options={"seed": args.seed, "temperature": 0, "num_predict": 280},
                surface="concept_analogy_replay",
            )
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                payload = None
            result = payload if isinstance(payload, dict) else {"error": "invalid_model_json"}
            return {**result, "prompt_tokens": usage.prompt_tokens}

        path = Path(trial) / "episodes.db"
        db = ChatDatabase(path)
        with closing(db._get_conn()), closing(MemoryStore(path)) as store:
            records = seed_episodes(db, store, held_out=args.held_out)
            cases = replay(records, store, infer, require_fit=args.require_fit,
                           check_current=args.check_current, held_out=args.held_out)
    print(json.dumps({
        "model": route.model, "seed": args.seed, "require_fit": args.require_fit,
        "check_current": args.check_current, "held_out": args.held_out, "cases": cases,
    }, indent=2))


if __name__ == "__main__":
    main()

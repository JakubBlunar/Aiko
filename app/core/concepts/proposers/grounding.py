"""Evidence distinctions shared by the aspiration and tension proposers.

These rails improve the existing generation call. They do not constitute a
semantic entailment checker or add a second model call to normal synthesis.
"""
from __future__ import annotations

from app.core.infra import timephrase

def source_check_declined(answer):
    """Respect an explicit abstention even if the model also emits concepts.

    These checks are requested before labels, but the generic array parser
    previously discarded them. Missing checks retain the other proposers'
    contracts; a positive check still does not prove semantic entailment.
    """
    if not isinstance(answer, dict):
        return False
    return any(isinstance(value := answer.get(key), str)
               and (value.strip().lower() == "none"
                    or value.strip().lower().startswith("none:"))
               for key in ("source_change", "source_conflict"))


ASPIRATION_GROUNDING = """
Evidence test for a direction (apply before proposing OR reinforcing):
- Date order establishes chronology, not development. Identify what actually
  changed between the earlier and later evidence. Recurring enjoyment, a
  familiar routine, and repeated descriptions of the same state are not a
  direction. Do not invent an earlier 'occasional', 'passive', 'hesitant' or
  'solitary' stage just to supply the start of a moving-from/to label.
- Require evidence of a sustained change in practice OR an explicitly stated
  continuing direction with concrete, distinct steps or changes in intention.
  Three mentions of one unchanged intention or promise are not three steps.
- Respect who did or wants what. A companion's promise is not evidence that
  the user is developing a skill or changing their own intentions.
- A planned event, promise, or waiting period is not an action already done.
  The passage of time, a repeated offer, and nearing an event date do not
  establish preparation, execution, increasing commitment or growing readiness.
  Use reported actions for progress claims. A continuing stated aim can remain
  an aim; do not describe its hoped-for results as achieved.
- Keep the narrow change actually supported. Do not add a hidden emotional
  function, a personality improvement, or a grander life trajectory.
- If the evidence only repeats or waits, return {"concepts": []}. Abstention
  is the correct output for that input, even with three well-spaced records.
- Record spacing is not activity frequency. 'Again', 'same', 'another' and
  'still enjoys' do not establish an earlier absence or an increase. Reading
  another chapter is not evidence of deepening appreciation or commitment.
  Example: 'enjoyed a puzzle', 'enjoyed another puzzle', 'still enjoys puzzles'
  -> {"concepts": []}, not 'developing a puzzle-solving practice'.
  Contrast: 'wants to learn; completed a beginner exercise', 'completed harder
  exercises', 'made an independent project and continues practising' supports
  development, while its eventual ambition remains unachieved.
"""

TENSION_GROUNDING = """
Evidence test for a conflict (apply before proposing OR reinforcing):
- First check whether BOTH claims can be respected as stated. If they can,
  return {"concepts": []}. Difference, contrast and complementarity are not
  conflict. Do not manufacture a trade-off to make a pair interesting.
- Require a concrete incompatibility in a shared situation, scarce choice or
  stated intention versus observed contrary conduct. Different occasions or
  domains can coexist unless the evidence itself establishes a collision.
- Keep every qualifier: when, where, with whom, and whether a statement is
  a preference, request, refusal or actual behaviour. Do not erase context to
  turn a contrast into a conflict. A quiet concept/hint is not proof of neglect.
- Add no missing premise: gentle interaction does not mean no speech; kindly
  phrased honesty does not mean lying; a preference is not an absolute refusal.
  Rationale prose and confidence cannot supply a restriction absent from the
  claims themselves. Do not infer hidden needs or motives to make them clash.
- Only name the specific supported collision, with both sides attributed.
  Otherwise return no tension; there is no obligation to find one in a pair.
- A possible conflict IF an extra condition were true is not evidence that
  condition holds. 'Could', 'may', 'often' or 'in conversational dynamics' in
  your explanation cannot turn compatible statements into an observed clash.
  Example: 'values accurate explanations' + 'explains patiently' -> no tension.
  'Wants to leave the entire afternoon free' + 'keeps scheduling appointments
  in that same afternoon against that intention' -> a supported tension.
- In source_conflict, briefly name the explicit shared situation and which
  stated requirements cannot both be met, or 'none: both can be respected'.
  This factual check comes before proposing a label. If neither claim
  prevents satisfying the other as stated, concepts must be empty.
"""


def aspiration_source_status(memory):
    """Distinguish the time a note was recorded from an event's status/time."""
    temporal = str(getattr(memory, "temporal_type", "") or "unknown")
    kind = str(getattr(memory, "kind", "") or "unknown")
    parts = [f"record type: {kind}", f"temporal type: {temporal}"]
    event_time = str(getattr(memory, "event_time", "") or "")
    if timephrase.parse_iso(event_time) is not None:
        parts.append(f"event time: {event_time}")
    if temporal == "future_plan" or kind == "promise":
        parts.append("plan/promise record, not proof of completed action")
    return " [" + "; ".join(parts) + "]"

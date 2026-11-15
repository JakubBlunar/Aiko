import type { TaskSnapshot } from "@/types";

/** The backend owns objective semantics; older responses remain explicitly unverified. */
export function taskOutcomeLabel(task: TaskSnapshot): string {
  if (task.objective_outcome) return task.objective_outcome.label;
  if (task.status === "done") {
    const outcome = task.result?.outcome;
    if (outcome === "partial") return "Partial result";
    if (outcome === "blocked") return "Blocked";
    if (outcome === "uncertain") return "Needs review";
    if (outcome === "missing_capability") return "Capability unavailable";
    return "Result unverified";
  }
  return task.status === "awaiting_input" ? "Needs your answer" : task.status;
}

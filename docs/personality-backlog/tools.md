# New tools / capabilities

---

## D-approval. Spoken / Aiko-voiced task approvals

**Motivation.** The task-approval framework ([`docs/task-approvals.md`](../task-approvals.md))
ships UI-only: a destructive action (file overwrite today; shell exec /
http post later) parks an `awaiting_input` approval that shows up as a
clickable prompt in the TaskStrip, and Aiko stays silent (background
children are `notify_aiko=False`, so `_on_task_input_needed_event` skips
the chat cue). That's the simple, safe v1. The natural follow-up is to
let Aiko *ask in her own voice* — "I'd like to overwrite your todo list,
that okay?" — so approvals feel conversational instead of a popup, while
the TaskStrip buttons stay as the fast path.

**Key files (existing).**
- [`task_orchestration_mixin.py`](../../app/core/session/task_orchestration_mixin.py)
  `_on_task_input_needed_event` / `_input_needed_should_notify` — the
  gate that currently suppresses the spoken cue for `notify_aiko=False`
  children. This is the single point to flip (per-capability, or per a
  new `agent.spoken_approvals_enabled` flag).
- [`approval.py`](../../app/core/tasks/approval.py) `build_request` — the
  prompt copy Aiko would voice.
- [`prompt_assembler.py`](../../app/core/session/prompt_assembler.py) /
  the T6 task-cue provider — where a spoken approval cue would render.

**Open questions.**
- Per-capability opt-in (voice `file_write` but not a future `payment`)
  vs. one global switch.
- How to keep the chat reply and the TaskStrip in sync when the user
  answers in prose ("yeah go for it") vs. clicks — `parse_decision`
  already handles both, but the answer needs to route back to
  `orchestrator.answer(task_id, ...)` from the chat path.
- Escalation: a spoken approval should probably reuse the existing
  input-needed escalation window so a silent user still gets nudged.

---

## D1. Calendar / reminders tool

**Motivation.** `promise` memories already capture "I'll do X" but they
have no time component. A real reminders tool would let Aiko answer
"remind me about the dentist on Tuesday" and surface it at the right
moment via the existing proactive director. Pairs naturally with the
shipped temporal-memory awareness work (`event_time` /
`relevance_until`); reminders become the user-facing surface for the
same plumbing.

**Key files (new + existing).**
- New: `app/core/reminders_store.py` (SQLite-backed, simple `id, text,
  due_at, fired_at, source_message_id` table).
- New: `app/llm/tools/reminders.py` — `set_reminder(text, when)` and
  `list_reminders()` agent tools.
- Existing: [`app/llm/tools/builtins.py`](../../app/llm/tools/builtins.py)
  `build_default_registry` — register the new tools, gated on a
  config flag.
- Existing: [`app/core/proactive/proactive_director.py`](../../app/core/proactive/proactive_director.py)
  — extend `_pick_topic` to surface a due-but-unfired reminder ahead of
  generic nudges.
- Existing: [`app/core/proactive/follow_up_worker.py`](../../app/core/proactive/follow_up_worker.py)
  — already nudges on overdue `future_plan` memories; reminders are a
  thin formal cousin.

**Sketched approach.**
- Tool: parse `when` as ISO-8601 OR a small natural-language helper
  (`dateparser` or a tiny regex set: "tomorrow at 3pm", "in 2 hours").
  Don't reach for a full NLP stack — keep it boring.
- A periodic check (~60 s) in `SessionController` polls the store for
  reminders whose `due_at <= now` and `fired_at IS NULL`, picks the
  earliest, marks fired, and triggers a proactive turn (reuses C1).
- Visible in the web UI via a small "reminders" panel reading the same
  table over an `/api/reminders` endpoint.

**Open questions.**
- Recurring reminders (every Tuesday)? Out of scope for v1; one-shot is
  the 80% case.
- Notifications when the browser tab is closed? Web Push is heavy; a
  dock badge / system notification via Tauri is cleaner.

---

## D2. Image vision tool — SHIPPED (Part A: local-vision describe task; Part B: in-chat attachments)

**Status.** Shipped in two parts. **Part A** — a **background workflow skill** (`describe_image`), not a fast brain tool, reusing the **single local worker model already in VRAM** (no second model, no cloud image tokens). The `VisionDescribeHandler` ([`app/core/tasks/handlers/vision_describe.py`](../../app/core/tasks/handlers/vision_describe.py)) resolves an image inside a configured file root, base64-encodes it (extension + byte-cap gated), and calls the worker `OllamaClient.chat(images=[...])`. Gated by `agent.vision.enabled`; the worker model must be multimodal (`qwen3.5:27b` / `qwen3.6:27b`). MCP debug: `get_vision_state()` / `describe_image_now(path)`. **Part B** — in-chat file attachments: the composer accepts image + text files (paperclip / drag-drop / paste), they land in a managed read-only `Attachments` sandbox root (`data/attachments/`), persist on the user message (`messages.attachments`, schema v18), and surface as a per-turn hint that routes Aiko to `start_workflow` (`describe_image` for images, `read_file` for text). See [`shipped.md`](shipped.md). The original sketch is kept below for reference.

---

**Motivation.** Ollama supports vision models (`llava`, `qwen2.5-vl`,
etc.). Letting Jacob drop an image into the chat and have Aiko comment
on it ("oh, that's a cute desk setup — what's that on your monitor?")
is a huge presence multiplier and pairs naturally with her curiosity.

**Key files.**
- [`app/llm/ollama_client.py`](../../app/llm/ollama_client.py) —
  `chat_with_tools` would need to accept image attachments. Ollama's
  `/api/chat` already supports `images: [base64]` in the message body.
- New: `app/llm/tools/vision.py` — `describe_image(path)` tool that
  routes to a vision model.
- Existing: web upload path already handles images for documents; would
  need a new branch that doesn't chunk them.

**Sketched approach.**
- Frontend: drag-drop image into the chat composer -> POST to
  `/api/chat/image` -> backend stores it briefly and includes a tool-call
  hint in the next turn ("Jacob just shared an image — call
  `describe_image` to see it").
- Vision tool runs the configured vision model, returns the description
  as the tool result; Aiko's spoken reply uses it naturally.
- Image is NOT persisted to memory by default (privacy). Aiko could tag
  `[[remember:Jacob shared a desk photo]]` if it's notable.

**Open questions.**
- Vision model size — default to a quantised 3-7 B model so it runs on
  the same box as the chat model? Or always cloud-route image calls?
- Fallback when no vision model is available: gracefully skip the tool
  and let Aiko say "I can't actually see that yet, sorry".

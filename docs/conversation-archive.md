# Conversation archive recall (D8)

The first D8 slice adds `conversation_history`, a read-only brain tool backed by
the original SQLite messages. It works without RAG, an embedder or a worker model.
The daily foundation stores **extractive outlines**: conversation counts and
selected, timestamped source quotes. These are reviewable navigation records,
not generated topic summaries or claims about what happened in the user's day.

## What already existed

`recall` returns semantic snippets from messages, memories and documents.
K-time2 adds date-aware historical hits to RAG. Neither is a paged transcript or
evidence that an entire date range was inspected. `SummaryWorker` compacts a
single session for its prompt; thread notes describe a session's current state.
D8 uses the retained `messages` rows across the current user's sessions instead.

## Read paths

`conversation_history` has three modes:

- `messages`: chronological transcript pages with optional full `session_id` and
  literal `topic` substring filters. Selected topic matches are labelled as such.
  Neighboring same-session turns appear separately in `context`; context can
  overlap adjacent pages and never changes the match count.
- `overviews`: a paged directory of up to 14 stored local-day outlines. A
  single-day lookup includes up to 12 source excerpts. Multi-day directories omit
  excerpts; open a day or individual message to inspect the dialogue. Missing
  source days are listed explicitly and do not block the transcript.
- `message`: original message text in character pages. Start with `message_id`;
  follow `next_cursor` for long messages. `content_offset` also permits a direct
  current-source slice. Quote from these rows, not a future generated summary.

Examples:

```json
{"mode":"overviews","when":"last week","timezone":"Europe/Bratislava"}
{"mode":"messages","start":"2026-10-01","end":"2026-10-24","topic":"guitar"}
{"mode":"message","message_id":123}
{"mode":"messages","cursor":"<next_cursor>"}
```

Ranges accept relative expressions through `time_expr.parse_time_window`, or an
inclusive pair of local ISO dates. Responses report half-open UTC bounds, local
bounds/offsets and the timezone policy. The default `system` policy applies the
OS's timezone rules separately at each boundary; an explicit IANA zone makes
historical views reproducible across timezone changes. `tzdata` supplies IANA
rules on Windows. Up to 366 local days may be requested.

Ownership follows the existing `user_id:session_id` namespace. Archive reads
exclude unnamespaced legacy rows rather than guessing their owner. User IDs
containing `:` are rejected because the existing composite key cannot establish
their ownership unambiguously. SQL uses exact owner equality, never `LIKE` for
scope, so `%` and `_` in IDs cannot broaden access. Only user/assistant rows are
conversation sources.

## Budgets and coverage

A lookup inspects at most 5,000 chronological source rows, plus one overflow
sentinel, using an owner/time index. A separate owner/ID index bounds insertion
watermark lookup. Transcript pages contain at most 20 primary rows, 1,600
characters per row and 12,000 source-text characters including context.
Individual-message pages expose up to 12,000 characters with continuation.

When a range exceeds the scan budget, exact totals are `null`, lower bounds are
provided and `scan_complete` is false. Narrow the dates after paging through the
inspected portion. No topic match in a partial scan is `partial_scan`, not proof
that the topic was never discussed. Empty ranges, missing overviews, unavailable
stores and stale cursors have distinct statuses.

Coverage has separate meanings: all retained messages accounted for, all pages
read, source text clipped, and selective highlights. Fully indexed days still
have selective highlights. The current day remains explicitly open, including
when a saved outline is read after its original build time.

Cursors are signed and bind the user, mode, resolved range, filters and source
revision. Transcript cursors freeze the maximum inserted ID, so late-arriving
rows cannot move pages. Edits/deletions expire cursors; process restart also
expires them. Overview cursors expire if the directory changes during browsing.

## Daily data collection and review

`DailyOverviewWorker` joins the existing demand-driven idle scheduler as a
compute-only maintenance worker. Its demand probe checks indexed existence and
cached day bounds; it does not read transcripts. It builds one missing local day
per run from the most recent seven days, newest first, across sessions. It does
not create entries on dates without retained conversation. Failed work remains
missing and retries through the scheduler's existing failure bookkeeping.

Settings under `tools`:

- `conversation_history: true` exposes the brain tool independently of RAG.
- `daily_overviews: true` enables recent-day outline collection during idle time.
- `archive_timezone: "system"` chooses the default local-day policy.
- `enabled: false` disables both the tool and automatic collection.

Existing history is backfilled only with an explicit bounded maintenance command.
These commands do not call a model or touch source messages, memories or config:

```powershell
# Read-only review of stored outlines; one day includes its source excerpts.
.venv/Scripts/python.exe scripts/conversation_archive.py --db data/chat_sessions.db --user default --days 1 --end 2026-10-24 --timezone Europe/Bratislava

# Explicitly create/rebuild the requested derived records, up to 366 days.
.venv/Scripts/python.exe scripts/conversation_archive.py --db data/chat_sessions.db --user default --days 7 --end 2026-10-24 --timezone Europe/Bratislava --write
```

The default review command opens SQLite in read-only mode and never initializes
or migrates it. `--write` installs only the additive archive schema. A normal app
startup also installs this schema, without a general schema-version migration.

## Source lifecycle

SQLite triggers delete affected outlines transactionally on message insertion,
content edit, ownership/timestamp/role change or deletion. Session clearing uses
the same triggers. A deleted private excerpt is removed from the saved archive
record, including when the deleted/edited row was not one of its highlights.
The builder compares the source revision and insertion watermark under a write
lock before saving, so a concurrent edit/deletion cannot resurrect private text.

Each record carries timezone, UTC bounds, generator version, source IDs,
insertion watermark, mutation revision and a hash of the source metadata plus
revision. This fingerprint is for change detection, not a content checksum.
An outline built under a different system-zone boundary is treated as missing.

## Archive review and generated summaries (28 October 2026)

Open **Settings > Archive**. Pick a date range and browse source days, including
days without a saved outline. Open a day to review its selected quotes, read
the paged transcript, or explicitly request **Generate summary**. Search finds
literal phrases in the original dialogue, ignoring case, with separately labelled
neighboring context. **Open source** retrieves the original message, including
continuation pages for long messages. It does not switch or modify the active chat.

Summary requests are durable, content-free rows serviced by `archive_summaries`
in the existing idle scheduler's LLM lane, one day per admission. This works for
historical source days outside the automatic seven-day outline window. Requests
need tools, daily overviews and the idle scheduler enabled; transcript browsing
does not. Generation uses the current `worker_default` model and maintenance
client, resolving the route again for each run. It adds no calls to a chat turn
and does not create memories or proactive cues. Reads through the brain tool
remain synchronous and read-only; an overview miss never silently enqueues work.

Each request considers a bounded chronological prefix: at most 100 messages and
16,000 source characters, with per-message excerpt limits and actual route context
packing. A single model call has a 90-second timeout and a 1,400-token output
reservation. Shortened sources and context reductions are reported; a summary
must not imply the entire day was considered when it was not. The panel refreshes
pending requests only while visible. It offers cancellation, explicit retry and
dismissal of failed requests. A running lease can recover after five minutes;
recovery/source-change retries are bounded to two attempts. At most 32 pending,
running or failed requests are retained per user.

Generated bullets distinguish discussion, user statement, assistant suggestion,
plan, decision and correction. Discussion date and optional event date remain
separate. Worker prompts contain exact source IDs/timestamps, `format_transcript`,
`today_anchor` and `STORED_TEXT_TIME_RULE`. Stored generated text rejects bare
relative deictics; exact source quotations preserve their original wording.
Every bullet must carry one to three literal quotes from the supplied messages.
Attribution checks reject user claims based only on assistant text and assistant
suggestions without assistant evidence. These checks establish provenance,
**not semantic entailment**: the UI and tool explicitly label the output as a
generated interpretation, and plans/suggestions are not proof of events.

The summary is stored inside the existing overview row. Insertion, editing,
deletion, ownership, role or timestamp changes invalidate that row through the
existing triggers. Generation compare-and-saves against the original source
hash/revision and a claimed job receipt, so cancellation, requeue or in-flight
source changes cannot resurrect old text. Regenerating a summary also expires
directory cursors. Multi-day directories return one shortened summary point per
day; open one day for the complete generated summary and exact source drill-down.
Explicit outline rebuilding through the maintenance script replaces any summary
for the rebuilt day.

REST: `GET /api/archive` supports `overviews`, `messages` and `message` with the
existing bounded filters/cursors. `POST /api/archive/summary` queues one local
`day` and optional `timezone`; `DELETE` with the same body cancels/removes it.
User ownership always comes from the session facade, never a request field.
The additive job table is installed at normal backend startup. Restart the
backend and refresh the frontend (or rebuild a packaged desktop frontend) to
load this slice.

Remaining: semantic topic search, automatic historical requests with an explicit
cost policy, cross-day correction links, and real-model comparisons of recap
faithfulness and overview-to-source tool selection. Validation uses synthetic
sources and fake model responses; it does not establish current-model quality.

Validation for this slice: 79 archive/REST/lifecycle tests and 96 related frontend
tests passed; Chrome checks covered day navigation, source continuation,
generation/cancellation, literal search, stale/deleted sources, deletion-event
invalidation and 390px/320px layouts. Lint, the production web build and documentation
link checks passed. A representative shared database/scheduler/session/REST set
had 139 passing tests; its private-access guard reports two existing budget
failures (web: 1 versus 0, MCP: 471 versus 466), confirmed unchanged in committed
HEAD. No live inference, personal-history generation or live settings changes
were used for validation.

## Initial validation, 24 October 2026

The representative database/migration, settings, registry, gate, scheduler,
message-provenance and session-restore set passed 557 tests and 13 subtests.
The final focused archive set passed 43 tests. `npm run lint` passed Python lint
and TypeScript checking. The affected selector reaches hundreds of files through
shared database/settings imports, so the focused set and representative shared
contracts were used instead of a whole-suite run.

A bounded 31-date backfill (24 September through 24 October, Europe/Bratislava)
created 21 source-day outlines; ten dates had no retained conversation. Comparing
every source message before/after confirmed the 9,731 message rows were unchanged.
No inference or embedding requests were made.

On that retained local database, 30 read-only samples measured the first
transcript page at median 1.36 ms / p95 2.13 ms, approximately 2,659 tokens, and
the first daily-directory page at median 2.74 ms / p95 3.21 ms, approximately
3,822 tokens. These are warm local SQLite reads, not end-to-end chat latency or
model-selection measurements. Token counts use the project's estimator.

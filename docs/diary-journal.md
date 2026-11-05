# Aiko's diary

The diary is an append-only view of journal memories, not a second fact store.
It includes `diary`, `reflection`, `shared_moment`, `open_question`,
`pursuit_note` and `curiosity_finding`. The separate concept evolution diary is
a grounded record of changes in understanding; it is not this personal journal.

## Review and changes, 5 November 2026

The running app already exposed `read_diary`. Its persona also described an
invisible `[[diary:...]]` writing tag. There was no explicit agent writing tool
or saved-entry receipt for that action. Diary content was spread across chosen
tags, quiet-window reflection and sleep-continuity workers, with incomplete
authorship information on older rows. The UI primarily distinguished kinds.

The away writer had a cooldown but no source-message watermark. An unchanged
conversation could therefore produce another reflection whenever the timer
elapsed, and an empty composition did not spend the cooldown. Existing prose
frequently returned to broad relational reassurance. That observation is not a
claim that every entry was repetitive or false.

## Reading and choosing to write

`read_diary` remains a synchronous, local tool. It supports newest-first listing,
kind filtering, literal substring search, pagination and inspection by an actual
entry ID. It returns recorded dates, content, source metadata and authorship.
An entry is a perspective from its recorded time, not necessarily a present
feeling. Dreams, reflections and research snippets retain their limitations.

`write_diary` synchronously saves one intentional first-person entry through
`MemoryStore.add`, with a durable SQLite receipt and the normal memory-added
notification. It accepts:

- `content`: 16-2000 characters of journal prose. Concrete dates are required
  wherever the prose names time; relative deictics and inline control tags are
  rejected with a useful error. Creation time is recorded automatically.
- Optional `revisits_entry_id`: a saved diary ID, normally obtained from
  `read_diary`, that the new entry reflects on. The earlier entry is preserved.

Writing does not create a fact, research result, belief or concept. New entries
are `diary` memories with `subject=aiko`, `mode=reflection`, dated `past_event`
semantics and a chosen-entry receipt. Repeating the same content, session,
calendar date and revisit reference returns the saved receipt without adding
another row or sending another notification. Different perspectives remain
separate even if their embeddings are similar. A tool write followed by an
identical inline fallback does not create a second diary entry.

Both tools require memory and recall tools to be enabled; writing additionally
requires the embedder and is refused while asleep. The new writing tool belongs
to the `journal` tool family. It is available on diary/journal requests but is
excluded from the strictly narrowed premise-recall pass. Adding it does not
force a tool round-trip on every ordinary conversation turn. Inline diary tags
remain compatible with spontaneous notes on turns where no tool pass runs.

## Awareness and authorship

`diary_awareness_block` is configuration-stable context at the end of T0, outside
the fixed persona cache head. It is wired independently of the user-editable
persona and disappears when diary reading is unavailable. It states that the
diary exists, survives conversation changes, is readable by the user, and can
hold perspectives that change. Tool schemas own detailed usage guidance.

This block offers no conversational stance: it neither asks Aiko to discuss a
diary topic nor instructs her to write. It therefore is not an `_OFFERS` entry
and creates no new proactive cue or journal quota.

New writes carry `metadata.journal` version 1 with `authorship`, `source` and an
optional `revisits_entry_id`. Intentional tool/tag entries are `chosen`; away and
post-sleep entries are `background`. `memory_origin` continues to identify the
writer and subjective meaning for downstream memory/concept readers. Older
rows are not rewritten or retrospectively certified. A known historical writer
can identify a background entry; absent provenance remains unspecified.

The Diary UI labels chosen entries as "written in conversation", background
entries as "quiet reflection", and unknown earlier sources as "source not
recorded". Follow-up entries show the earlier diary ID they revisit.

## Background quality

The away worker now fingerprints source message IDs, session, roles, content and
timestamps. Rendered age labels are deliberately excluded: time passing alone
must not make an old conversation new material. Readiness and demand are
read-only. A completed composition or intentional abstention consumes its
source revision; failed generation/storage does not certify that revision as
processed. Every composition attempt spends the cooldown. Debug forcing can
still bypass normal scheduling gates.

The prompt includes the last three dated diary entries as subjective context,
asks for a specific new detail, change or uncertainty, and can abstain instead
of paraphrasing earlier affection. Imagined scenes retain that framing. This
improves admission and context; it does not prove the quality of model prose.

## Validation and activation

Focused regressions cover real persistence/read-back, retry receipts, changed
perspectives, invalid references, time validation, sleep gating, notification
failure, tool routing, prompt availability and source-revision scheduling.
Representative affected integration covers tool passes, prompt-cache ordering,
worker demand and sleep continuity. Related frontend checks cover the labels.
Lint is required.

Restart the backend and refresh the UI to activate the tools and awareness.
The review did not send a conversation message, generate a live diary entry or
rewrite existing diary records. Fresh voluntary entries and actual rereading
are the next useful runtime check; no automatic diary prompt is introduced.

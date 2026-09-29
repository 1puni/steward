# Deep task and session inspection: research and implementation proposal

Status: research and proposed interface, **not implemented**. Observation date:
2026-09-29. Attribution: `task:task-9a9f36dbc0965dea9d871351317e285f`, requested
by V in `turn_fb92d886bd49433a9a7810c400b6edcd`.

The dashboard can expose substantially richer evidence without creating a task
store. Keep the overview snapshot, and fetch bounded detail from the owning
harness when a reader opens a task, session, tool result or artifact. Accepted
Git remains task authority; native transcripts remain original evidence. A
session's claim that something shipped never substitutes for controller-observed
publication or target readiness.

## Evidence scope and access

Harness source inspected at `17d1f2a83203dad7032b6dc81862c24b4431db1b`, also the
revision named by `/opt/1puni-steward/current` during inspection. Dashboard source
was readable at `/opt/steward-dashboard/current`, resolving to release
`64b3416a7515a41074f423d1038ea528c0648a7d`. This is inspection of deployed source,
not an authenticated browser test or proof that every running process loaded it.
The dashboard repository's upstream HEAD was not fetched or verified. Later
commits on this research branch preserve interrupted execution records; they do
not change the source baseline above.

The actual `/var/lib/steward-dashboard/snapshot.json` read was permission-denied.
The controller database `/var/lib/1puni-steward/state.db` is root mode 600 and its
accepted Git store `state.db.tasks.git` is root mode 700. Their live contents,
private receipts, task locks and authenticated dashboard responses were not
inspected. No credentials were read, and no production changes, publication or
deployment were performed. Source-level export coverage below is verified;
current exported row counts and freshness are unverified. These access gaps do
not establish that the corresponding evidence is missing.

Metadata-only checks of retained files at the harness source revision found 54
artifact files. Two representative regular JSONL blobs were parsed without
including message bodies or tool output in this report:

| Sample | Immutable blob | Size and shape |
| --- | --- | --- |
| Claude session `e065a78d-f9f5-4637-8670-a139e1e5a653` | `b8544c9a5052e5aef9065856af62cb2169d86008` | 1,072,767 bytes; 349 records, including 71 user and 123 assistant records; `sessionId`, `uuid`, `parentUuid` and auxiliary record fields present |
| Codex session `01a0910a-d0ae-7ea1-a5c4-8e8393236138` | `71cd818ecaca274e8e265fa90ca3c973a17b98b7` | 2,327,668 bytes; 670 records, including 292 `response_item`, 278 `event_msg`, session metadata and turn context |

The Claude file is under `artefacts/claude/projects/` in a task-specific directory;
the Codex file is under `artefacts/codex/sessions/2026/09/11/`. The sampled Claude session also had 67 tool-use IDs and 67 tool-result
references, all matched, plus 289 records with a nonempty parent UUID. Other retained
Claude sessions have companion `tool-results/*.txt` blobs. An additional read of
the growing transcript for this investigation found eight tool-call IDs and seven
result IDs, all seven joined by call ID, with no invalid JSON lines at that
instant. This verifies the join shape, not session completeness: the read occurred
while a tool call was executing. Counts of native records are not counts of
visible chat messages. No GLM record was independently sampled.

## Source map

Harness references below are relative to this repository; the baseline revision
above pins the findings. Dashboard references use repository-relative paths in
`1puni/steward-dashboard` at the deployed revision above.

| Key | Source and inspected responsibility |
| --- | --- |
| H1 | [task_store.py](../src/steward_harness/task_store.py): `Definition`, `Task`, `GitTaskStore.checkpoints`, `_history`, `finish_slice`, repair decisions |
| H2 | [task_runner.py](../src/steward_harness/task_runner.py): `_work` lineage binding and slice execution; [state.py](../src/steward_harness/state.py): schema, conversation lineage, turn records, result receipts |
| H3 | [native_workspace.py](../src/steward_harness/runtime/native_workspace.py), [retention.py](../src/steward_harness/retention.py), [native record contract](native-record-provenance.md), [runtime storage boundary](native-provider-runtime.md#native-workflows-and-storage-boundary) |
| H4 | [tasks.py](../src/steward_harness/web/tasks.py): `TaskBoard.detail`, board and Telegram authentication; [task board contract](task-mini-app.md) |
| H5 | [receipts.py](../src/steward_harness/receipts.py), [targets.py](../src/steward_harness/targets.py), [gates.py](../src/steward_harness/landing/gates.py), [merger.py](../src/steward_harness/landing/merger.py), [process.py](../src/steward_harness/runtime/process.py) |
| D1 | `src/steward_dashboard/export.py`: `Exporter.write`; `sources.py`: document limits, source reads and snapshot route |
| D2 | `src/steward_dashboard/rhythms.py`: stable SQLite copy, execution projection and `task_provenance`; `history.py`: regular Markdown blobs, bounded document histories/diffs |
| D3 | `src/steward_dashboard/access.py`, `deploy/steward-dashboard-export.service`, `docs/deployment.md`: authentication, process identities and exporter sandbox |
| D4 | `frontend/src/navigation.js`, `work.js`, `reading.js`, `style.css`: current hash routes, work/document views and reduced-motion rules |

## Inventory: authority, identity, retention and export

| Evidence | Authoritative location and joins | Completeness and access | Current dashboard coverage |
| --- | --- | --- | --- |
| Task account and history | Controller `<state_db>.tasks.git`, `refs/heads/tasks/<task_id>`, accepted `task.md` and decision commits. Key by repository, full task ID and accepted revision. Input commit SHA plus `Steward-Source` identifies attributed input. H1. | Private controller Git; accepted refs retain history. Walk the accepted decision line with the task store's boundary rules: a procedure's product-input parent is not an accepted decision. Agent-readable work refs are evidence, not authority over current acceptance. | Brief, findings, status, pending count and summary fields via `TaskBoard.detail`. Full accepted input/account history is not exported. |
| Slices, repair attempts and provider retries | Retained slice commit SHA and disposition/reason trailers; `Definition.repair` has base, work, attempt count and reason. H1/H2. | A slice has no unique identity until it commits; execution ID names the task conversation. Repair count is scoped to its repair base. Provider fallback/retry is not a durable universal attempt ledger. Do not invent a single attempt number combining these. | Checkpoint time/disposition/question only; no checkpoint SHA, complete attempt history or retry ledger. |
| Sessions and lineage | Native `artefacts/<provider>/...` files in retained work; `conversations` stores current provider/generation/session ID. Task conversation is `task:<task_id>`. `turns` has provider/session/generation for world execution rows. H2/H3. | Current lineage can change; it is not historical per-slice linkage. A reused session may span slices. Native files may predate a slice or include imported records. Exact task-to-session links require recorded metadata; path association alone is weaker. Private runtime databases/homes are outside Git. | No session index or transcript export. Narrow turn projection is not full lineage export. |
| Messages and native event records | Native JSONL; Claude `sessionId`, message `uuid` and `parentUuid`; Codex session metadata, response items and context. Use native identity where present plus immutable blob and record position. H3 and samples. | Native compaction, branches, duplicate representations and auxiliary events require provider-specific readers. Git preserves retained versions, not hypothetical missing runtime data. Preserve original order and distinguish native parent edges from chronological neighbours. | Not exported. Availability verified in representative retained Git blobs, not for every task. |
| Tool calls and results | Native content blocks/response items; Codex call/result `call_id`, Claude tool-use ID/tool-result reference; companion tool-result files where retained. Session and branch scope the join. | Some output is inline, some references files, some is provider-truncated or never completed. Orphan calls/results are valid observations. Do not execute tools or follow arbitrary transcript paths to obtain missing output. | Not exported. Complete original output cannot be promised even when a result record exists. |
| Artifacts | Regular blobs under a selected retained revision, including native companions; explicit record references can associate them with a tool. Revision + path + blob OID identifies bytes. H3. | Accepted retention is distinct from dirty worktree candidates. Ignored files, external URLs and launch-home caches have no guaranteed Git retention. Read-only launches do not imply all records were committed. | Manifest-selected documents only; no generic artifact browser or safe artifact endpoint. |
| Commits and diffs | Work ancestry retained by accepted task Git; integrated outcome and controller-observed remote ancestry establish landing. Compare named immutable base/head SHAs, retain merge parent choice. H1/H5. | Work commits, accepted decisions, integrated outcome, remote observation and target readiness are different facts. A worktree can disappear after accepted custody; Git objects remain while reachable. | Task landing field, limited checkpoints; document history/diffs are available, but not a general task code-diff API. |
| Execution, delivery and target receipts | `turns`: turn ID, source-event key, execution-turn link, times, status, candidate/base, reply/output. Result receipts at `<state_db>.task-results/<sha256(source_key)>.json`; target receipts include target, sequence and observation identity. H2/H5. | Controller-private. Conversation turns are not a universal task execution ledger. Delivery receipt is not proof of task success; target observation is not a transcript claim. Current runtime receipt availability was not verified. Pending world completion custody is not permanent history. | D2 projects selected world runs and narrow provenance. No general delivery, gate-output, target-receipt or task-execution detail browser. |
| Recorded ancestry | Task `Definition.source` -> `turns.turn_id` -> `source_event_key`; dashboard recognizes `task_result:<task-id>:` and `rhythm:<name>:`. Native parent-message or subagent metadata is a separate edge type. H1/H2/D2. | Missing source, deleted/unavailable row, unmatched event or absent native parent is unknown ancestry. Do not derive parenthood from titles, timing, neighbouring tasks or shared files. No verified universal delegation graph. | `source`, `origin_turn`, `parent_task_id`, `origin_rhythm`; parent task is populated only for recognized recorded result-event provenance. |

Native records are model/provider-controlled content. Controller receipt custody
and accepted Git provenance must remain distinguishable in every view. Credentials,
launch configuration and provider runtime databases must not become artifacts
merely because they sit near a transcript.

Retention is conditional, not a promised TTL. H3 removes clean task worktrees only
after accepted custody checks for completed/cancelled work, and retires native
owner homes. World-session cleanup has separate inactivity and pending-work
checks. Owner-scoped runtime homes can persist between launches in the inspected source;
anonymous launch homes are removed. This differs from older documentation that
describes every launch home as disposable. Owner-home persistence is still not
Git custody: retirement removes that runtime state. Native databases, sockets,
caches, ignored files and background jobs do not gain durable retention from a
foreground transcript. No end-to-end retention
SLA was established in this inspection.

## Exact limits in the existing projection

- Mini App board reads at most 200 summaries. The independent dashboard exporter
  instead calls `store.all()` and `TaskBoard.detail` for every task; the 200-row
  board ceiling does **not** bound its export (H4/D1).
- Detail asks for five checkpoints, but `checkpoints` scans at most five commits
  and then filters for disposition trailers. It can return fewer than five slices
  without reaching the beginning. It exports no checkpoint commit identifier and
  no `has_more` marker. Brief/findings have no per-field cap here.
- The whole dashboard snapshot has an 8,000,000-byte limit. Oversize export fails
  atomically and retains the previous snapshot; HTTP source ingestion also
  rejects an oversized snapshot. This is stale-data risk, not silent paging.
- World runs: latest eight execution rows per configured world rhythm, ordered by
  start time; findings capped at 24,000 characters with a truncation flag. There
  is no cursor or tie-breaker for equal timestamps (D2).
- World manifest documents: 160,000 bytes each. Repository documents: up to 80
  selected regular Markdown blobs per selection, 100,000 characters each, total
  content budget 1,500,000 characters; six first-parent history entries per file;
  each diff 18,000 characters, total diff budget 1,000,000. Content/diff truncation
  is marked; omitted files/history do not have pagination (D1/D2).
- Runtime process capture is a different limit from native retention: stdout
  exceeding 64 MiB raises an execution error; stderr retains its last 1 MiB (H5).
  These limits do not prove native tool output is complete. Gate runner results
  hold stdout/stderr, but merger failure reporting uses redacted tails (1,000 characters per stream) and success
  returns tested base/candidate identity. A durable full gate-log archive was not
  found in that path; do not advertise one without further evidence.
- Export cache keys include accepted revision, observed status, tip, reason and
  running flag. Export interval is 15 seconds **after** a read completes; deployment
  docs report a first read around 20 seconds. These are not measured latency
  guarantees for this execution. Snapshot responses use `no-store` (D1/D3).

“Not exported” means a source category is omitted by the current adapter.
“Unavailable to this reader” means access failed or was not granted. “Not found
at revision X” is a bounded negative lookup. “Not retained” requires evidence of
retention behaviour or loss; it cannot be inferred from an empty snapshot.

## Minimal read-only serving design

This is a proposed contract. Reuse the overview snapshot and existing dashboard
Basic/Bearer admission. Add a small harness-owned, allowlisted evidence reader
behind a local Unix socket with peer-UID admission; the authenticated dashboard
server proxies its bounded reads. The HTTP UID must continue to lack controller
Git/database/world filesystem access. The reader's privileged surface is a fixed
set of operations, not arbitrary Git, SQL, shell, path or URL execution. Keep it
outside the controller's scheduling/mutation API. Deployment of this boundary
requires an implementation review; the present export service does not already
supply such a socket.

A harness library owns canonical task selection, provenance, object authorization
and record adapters. The dashboard owns the socket client, HTTP representation
and UI. A disposable cache may hold parsed pages and indices; losing it changes
latency only. Never add a task table, copy authoritative task state into it, or
let a parser update controller lineage. SQLite observations must avoid migration
and source-directory side effects; the existing stable-copy adapter is a starting
point, with bounded retries when concurrent changes invalidate a read.

Suggested GET resources (names are proposals):

| Resource | First payload and subsequent work |
| --- | --- |
| `/api/tasks/{id}/detail` | Overview, accepted revision, observed status/time, evidence capabilities and availability; no transcript bodies |
| `/api/tasks/{id}/history?cursor=...` | Accepted decisions or retained slices as explicitly typed events, with commit IDs and source links |
| `/api/tasks/{id}/sessions?cursor=...` | Recorded session descriptors and association evidence; separate unverified associations |
| `/api/tasks/{id}/sessions/{session}/events?cursor=...` | Role/type, time, stable event ID, short preview, tool state and result links |
| `/api/tasks/{id}/evidence/{evidence}` | Expanded message or result, bounded text chunks and provenance |
| `/api/tasks/{id}/diffs/{comparison}?cursor=...` | Changed-file metadata first, then selected file hunks |
| `/api/tasks/{id}/artifacts/{artifact}` | Metadata, then allowed preview or bounded download of authorized bytes |

Namespace IDs by source/controller instance, repository and task. Do not make a
short display reference a global key. Session descriptors include provider,
native session ID, source revision and association kind. An event with no native
ID uses `(blob OID, record ordinal)`; content hashes alone collapse repeated
identical messages. Retain native call IDs and branch context. A cross-page
call/result join points to the original event ID rather than duplicating content.

Every envelope includes schema/parser version, source revision/blob where
applicable, observation time, availability, returned count, next cursor and
explicit truncation reason. Suggested availability vocabulary: `available`,
`not_exported`, `not_found_at_revision`, `access_denied`, `partial`, `unsupported`,
`temporarily_unavailable`. Counts are unknown unless calculated over the pinned
source. No empty success response for a failed upstream read.

Pagination pins a source before reading. Accepted decisions follow their bounded
first-parent decision line; work history uses a deterministic topological walk
with a documented tie-breaker and explicit parent edges. Session events follow
native file order, not wall-clock sort. Across sessions use `(start_time, provider,
session_id, blob_oid)`, with a defined missing-time bucket. Opaque signed cursors
bind task, source revision/blob, parser version, filters, ordering and last key.
Never use offset pagination on a moving session. Pin active files to a verified
byte prefix ending at a complete record, identify that observation, and mark it
candidate evidence; replacement/truncation forces a new observation. Phase one
can restrict itself to committed blobs.

Initial proposed budgets: default 50 rows, maximum 100; JSON pages at most 256 KiB;
previews 2 KiB; text expansion chunks 64 KiB; an individual source record above
1 MiB gets a bounded preview and explicit oversize state; direct preview/download
limit 10 MiB. Enforce limits while streaming, not after constructing an unbounded
Git result. Give each request a 5-second work deadline and each instance two
concurrent detail reads initially; queue saturation returns retryable busy state.
Make these tunable internal limits after measurements, not commitments to a new
configuration surface. Cap index/cache bytes and evict by LRU; no recursive
whole-repository scan on a board refresh. Large sources can return “indexing” and
resume a bounded, deduplicated cache job; they cannot monopolize the controller.

Cache immutable pages by authorized source scope, blob/revision, parser version,
projection/redaction policy and cursor. Use response-byte ETags; authentication
and authorization precede cache/304 responses. Keep sensitive browser responses
`private, no-store` by default and reuse the server-side cache. Refresh the small
mutable overview no faster than the existing observation cadence; separate
`observed_at` from serve time and display stale age. New source revisions offer
“New evidence available” without splicing records into a reader's pinned page.
Failures retain the last observation labelled stale, with retry, never a false
current status. No service worker or persistent browser transcript cache in the
first version.

Artifact IDs resolve through a server-generated manifest scoped to an authorized
task and pinned revision. Accept regular Git blobs only initially; reject symlinks,
submodules, path traversal and arbitrary absolute paths. A raw SHA is not authority
to read unrelated objects. Git reads must disable hooks, external diffs, textconv,
lazy fetch and credential prompts. Do not dereference transcript URLs or launch
homes. Render escaped text and sanitized Markdown; inline only bounded raster
images with pixel/dimension limits. HTML, SVG and executable content get metadata
or an attachment, never same-origin execution. Use MIME allowlists, `nosniff`,
attachment disposition where appropriate and restrictive CSP. Never inject
transcript HTML, terminal escapes or tool-provided navigation into the app shell.
Apply the existing authenticated scope to every range, thumbnail and download;
no token-bearing URL. Redaction is defense in depth, not a substitute for scoped
source selection. Authenticated transcripts can themselves contain secrets:
exclude known credential/config paths and raw hidden reasoning by default, and
label any withheld evidence explicitly.

## First task-to-session-to-evidence journey

Use the existing work board and typography. Opening a task reveals its title,
status, last observation and accepted revision in a calm header; the accepted
account and latest findings are readable immediately. Put a compact evidence
rail beneath it: History, Sessions, Changes, Artifacts. Each shows availability,
not a fabricated zero. Keep publication and target observations visually separate
from model-reported results.

Selecting Sessions loads descriptors only. A session row shows provider, recorded
identity, time range when known, retention/source badge and how it is linked to
the task. Selecting one opens a readable timeline with date separators, role
labels, subdued metadata and monospaced code. A tool call is a collapsed row with
name, short argument preview and recorded state. Expand arguments and result
independently; load large result chunks only on request. Display unmatched or
truncated results as such. Do not infer success from the absence of an error.

Changes opens a file list before loading hunks. Desktop can offer split diffs;
mobile defaults to unified diffs with line numbers and explicit additions/removals
that do not rely on colour. Artifacts open a metadata card, then a safe image/text
preview and authenticated download when allowed. Show source revision, size,
retention state and originating tool link when recorded. No preview should execute
content or fetch external resources.

Extend existing hash routes with session/event/artifact/comparison and source
revision parameters. A copied deep link must reopen the same retained evidence,
including an event outside the first page. Back restores board filters, list/board
mode, selected task, expanded rows, focused control and scroll anchor. Store
navigation state in memory/history state, not transcript text in local storage.
A direct link whose source is unavailable explains that limitation without
silently jumping to a different revision.

Desktop uses a narrow task/evidence navigation column beside the reading pane.
At 320 CSS pixels and 200% zoom, switch to a single pane with breadcrumbs and a
sticky back control; only code/diff regions may scroll horizontally. Use native
buttons/details semantics, visible focus, labelled expansion controls, at least
44-pixel touch targets and text status labels with adequate contrast. Announce
page-load completion politely, not every streaming event. Never force scroll on
refresh. Respect `prefers-reduced-motion`: no pulsing execution animation,
auto-scrolling or animated pane transitions. Existing reduced-motion styles and
hash-route helpers are a foundation, not proof of this unbuilt journey.

## Phased implementation brief and acceptance gates

| Phase | Harness ownership | Dashboard ownership | Acceptance criteria |
| --- | --- | --- | --- |
| 0: contract and representative access | Define versioned read envelopes, canonical task/source authorization and association confidence. Obtain authorized metadata-only fixtures from accepted Git, world/task sessions, receipts and missing-link cases. | Inventory current snapshot against schema; agree availability states and source identity. | An operator-authorized read verifies actual snapshot freshness and joins without exposing credentials. Fixture matrix covers Codex, Claude, GLM or explicitly unsupported GLM, missing/compacted records, shared sessions and orphan results. Document what cannot be reconstructed. No production rollout required to approve contract. |
| 1: task history and session index | Implement bounded reader and local peer-UID boundary; committed-source reads only. Expose exact checkpoint/decision IDs and verified session associations without altering store semantics. | Authenticated proxy, overview/evidence rail, paged history and session list, stale/error states and route extension. | Opening board fetches no session bodies. Opening task fetches only first detail pages. Equal timestamps and changing refs produce no duplicate/skipped items in a pinned walk. Unknown task/source and unauthorized object requests leak no contents. Cold/warm latency and memory measured on representative large histories; budgets enforced. |
| 2: message and tool timeline | Provider readers preserve native identities/order/parent edges; deduplicate only known duplicated representations. Bounded result chunks, stable event lookup and disposable indexes. | Timeline, independent expansion, load-more, event deep links and navigation restoration. | Golden fixtures prove call/result joins across pages and interrupted calls; malformed/oversize lines yield explicit partial states. No hidden reasoning appears in default projection. Opening one result does not download the session. Back/forward and copied off-page event links resolve correctly. |
| 3: diffs, artifacts and receipts | Scoped blob manifests, explicit base/head comparisons, safe MIME/size policy and narrow receipt projection. Retain unavailable receipt/log states. | File-first diff view, safe previews, authenticated download, source badges and typed ancestry links. | Reject traversal, symlinks, unrelated OIDs, malicious filenames/HTML/SVG, huge image dimensions and forged cursors. Verify no lazy fetch, hooks, textconv or writes. A target receipt and model completion remain distinct. Missing full gate logs are labelled unavailable, not successful empty logs. |
| 4: validation and optional live evidence | Validate reader isolation under production UID setup, time/concurrency limits, cache eviction and stable SQLite observations; add live-prefix reads only if needed. | Keyboard/screen-reader/mobile/reduced-motion checks; preserve reading position on refresh and upstream failure. | End-to-end task -> session -> result -> diff/artifact -> back works at 320px and 200% zoom. Auth expiry and source outage show actionable states. Source hashes/mtimes and controller behaviour remain unchanged by reads. Runtime validation uses existing auth, with no browser/upstream-token disclosure. Deployment is a separate authorized implementation task. |

Harness tests should target authority, source joins, record parsing, cursor
stability, bounds and read-only side effects. Dashboard tests should target
request-on-expansion behaviour, authentication, safe rendering, accessibility and
navigation restoration. Share schema/fixture conformance tests across both repos;
do not couple the browser to private database columns or native transcript formats.

Remaining implementation decisions are bounded: whether live candidate evidence
is needed beyond committed records; which receipt categories to expose first;
and whether every provider supplies sufficient historical task/session linkage.
Missing historical joins should remain unknown. Recording additional provenance
for future executions, if needed, is a separate harness contract change; a UI
must not backfill invented lineage. The research is complete with the access
gaps above; no claim is made that the proposed endpoints or journey exist today.

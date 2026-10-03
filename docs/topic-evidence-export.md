# Bounded Telegram topic evidence export

[`scripts/export-topic-evidence.py`](../scripts/export-topic-evidence.py) is a
standalone Python 3.11+ operator utility for the Adctx evidence prerequisite.
It has no network client, harness initialization, message operations or deployment
step. Run it only under the controller/local operator identity that already has
read access. The worker must not retry privileged reads or change permissions.
The repository CLI and documented execution boundary expose no existing permitted
metadata export route for these records.

## Operator handoff

From the reviewed checkout, set `ADCTX_STATE_DB` to the installed configuration's
`provider.state_db` absolute path and `ADCTX_UNIT` to that controller's systemd
unit. These installation-specific values are intentionally not guessed. Then run
this exact command in the already-authorized operator shell:

```sh
python3 scripts/export-topic-evidence.py \
  --db "${ADCTX_STATE_DB:?set the installed provider.state_db path}" \
  --unit "${ADCTX_UNIT:?set the installed controller systemd unit}"
```

Only the JSON on stdout is the shareable artifact. Return it through the existing
authorized evidence handoff to the waiting investigation
`task-6ddb4949812e5c4d925136844d23654d`. Attribution is
`task:task-6fc1029e3928576bba0dbefcde301a2a`. No raw receipt, database, journal,
configuration or provider transcript should accompany it. The investigation
remains waiting until real incident metadata arrives; this utility and its tests
are not incident evidence or a routing diagnosis.

The fixed selection is `tg_19352392` / `turn_9116c804cec9444eb9256e466742063d`
and `tg_19352394` / `turn_96154a271b9448f4a08eb921cba6c31a`.
Time bounds are **2026-10-03 17:12:00 through 17:18:00 UTC inclusive**.
Database rows require both identifiers and a matching start time. Incoming receipt
messages require the matching update ID and Telegram message date in that interval.
Journal records require the interval and exact update or receipt identity.
Receipt confirmations lack timestamps: they are associated with the selected
incident receipt, but cannot establish when delivery occurred. No other receipt
files or conversation rows are exported.

The operator should review the script against the installed revision before
execution if it differs from this checkout. Schema/log mismatches fail closed or
produce no matching records; they must not be interpreted as proof of no activity.
Use the configured database path spelling used by the controller, because journal
receipt association checks the full path exactly.

## Retention and meaning

| Output | Meaning |
| --- | --- |
| `absent` | File, record or optional key was not found at extraction time. Does not prove it never existed. |
| `pruned` | Requires independent affirmative retention/deletion evidence. The utility never infers this from absence; the current format has no pruning tombstones. If an operator has that evidence, report the affected receipt IDs separately as pruned. |
| `inaccessible` | A filesystem read was denied. No permission change or escalation is attempted. |
| `inaccessible_or_incomplete` | Journal command failed or warned; empty output is not accepted as successful coverage. |
| `never-recorded` | The identified format does not retain this field; it makes no claim about other sources. |
| `invalid`, `unreadable`, `unreadable_or_schema_mismatch` | Input cannot safely be projected. Errors and their potentially sensitive text are suppressed. |
| `identity_mismatch`, `outside_window_or_missing_date`, `absent_or_outside_window` | Selection cannot establish the requested ID/time association; contents are omitted. |
| `changed_during_copy`, `busy_or_rollback_journal` | A consistent database copy was not established. An operator may rerun during a quiet interval; no result is fabricated. |
| `unknown` | Journal retention completeness cannot be established from its selected records. |

Each optional incoming field has its own availability marker. Missing
`message_thread_id` remains absent; it is not silently turned into zero.
`reply_to_message` and `external_reply` project only message/chat/thread IDs and
`is_topic_message` where present. Sender identities, names, bodies, URLs, tokens,
attachments and arbitrary JSON keys are excluded.

`current_lineage` is the conversation's **current** provider-session association,
not its incident-time association. The separate turn `provider_session_id` is the
retained historical association. An `execution_turn_id` is exported as an ID only;
no unrelated execution's row or provider transcript is followed.

Confirmed outbound entries require a positive integer `text:N` or `artifact:N`
receipt result, or the exact confirmed-reply log format. A frozen reply or `done`
flag alone is insufficient. `pin:*` boolean results are not message IDs. An
attachment-alert uses a different destination and is not inferred from the
normal reply destination. `requested_topic_id` is the controller destination;
`response_message_thread_id` is never recorded by this API wrapper. Topic values
0 and 1 cause the wrapper to omit the wire thread parameter, so these receipt
values must not be presented as returned Telegram thread IDs. Journal confirmations
may describe replay of an already-confirmed piece, not a new send.

## Format provenance and read boundary

- [`state.py`](../src/steward_harness/state.py): `_DDL`, `_LINEAGE_DDL` define
  `turns` and `conversations`. The utility uses explicit selected columns and
  parameterized predicates, never `SELECT *` or `StateDatabase` initialization.
- [`telegram/service.py`](../src/steward_harness/telegram/service.py):
  `_receipt_path`, `_enqueue`, `send_reply` and `_send_piece` define
  `<state_db>.telegram-receipts/<update_id>.json`, the raw `update`, frozen
  `[chat, topic, text]` reply and `pieces`. `_poll_updates` deletes completed
  receipts after polling acknowledgment. `_route_update` and `send_reply` define
  the two log messages accepted by the whitelist. Ingress logs never record
  `is_topic_message` or reply identifiers; SQLite never records raw wire metadata.
- [`telegram/api.py`](../src/steward_harness/telegram/api.py): `send_message`,
  `send_photo` and `send_document` retain message IDs, not complete response
  messages. [`cli.py`](../src/steward_harness/cli.py) defines the logging prefix.

The utility opens live database/WAL files only as binary files for copying. It
checks inode, size, modification and change timestamps across the complete copy
and rejects detected concurrent writes or any rollback journal. It never opens
live SQLite, reads live SHM, initializes the harness or runs a writable live DB
connection. SQLite reads the private temporary copy with `mode=ro` and
`query_only`; any copied-WAL shared-memory work occurs there. The private copy
contains sensitive data temporarily and is removed on normal completion; keep
the operator's temporary directory private. This is a bounded stable-file capture,
not a replacement for the installation's backup procedure.

Journal extraction uses `journalctl --unit … --since … --until … --output=json
--no-pager`; raw messages stay in operator process memory. Only exact known log
formats are projected; errors, unrelated logs and full receipt paths are omitted.
No read here establishes journal completeness, receipt survival or live routing.

Synthetic validation:

```sh
uv run --extra dev python -m pytest -q -p no:cacheprovider tests/test_topic_evidence_export.py
```

Fixtures use the repository's actual SQLite DDL and representative receipt/log
formats. They verify body/credential redaction, exact ID/time association,
missing/access-denied distinctions, delivery confirmation filtering, and unchanged
source DB/WAL/SHM bytes. They do not access the incident system.

# Slack conversations

Slack is an optional Socket Mode transport on the same durable inbox, conversation
service and result receipts as Telegram and desk. Install it with
`uv sync --frozen --extra slack` (add `--extra dev` for tests). Keep the extra enabled
when building future releases for a Slack-enabled instance.

## Configure and provision

Create a workspace app using [the example manifest](../config/slack-app-manifest.json).
The manifest subscribes to private-channel messages. Invite the bot to the one
configured private channel. For a public channel, replace `groups:history` and
`message.groups` with `channels:history` and `message.channels`. Generate an
app-level token with `connections:write` and install the app for its bot token.
Keep both tokens in separate controller-owned files, unreadable and unwritable by
the execution identity. The normal broker startup audit checks both files and
their directories. Tokens never belong in the world, provider environment or Git.

Add this block to the trusted controller YAML after replacing every placeholder:

```yaml
slack:
  team_id: TPLACEHOLDER
  channel_id: CPLACEHOLDER
  bot_token_path: /etc/steward/slack-bot-token
  app_token_path: /etc/steward/slack-app-token
  users:
    UOPERATORPLACEHOLDER: operator
    UCONTRIBUTORPLACEHOLDER: contributor
    UOBSERVERPLACEHOLDER: observer
  user_names:
    UCONTRIBUTORPLACEHOLDER: Alice
  # Optional existing root messages, with timestamps quoted as strings:
  notifications:
    operator: "TPLACEHOLDER:CPLACEHOLDER:1234567890.000001"
    incidents: "TPLACEHOLDER:CPLACEHOLDER:1234567890.000002"
  delivery_roots:
    - /var/lib/steward-agent/world/delivery
  max_file_bytes: 20000000
```

Placeholders intentionally fail validation. At least one explicit operator is
required. Startup checks that the bot token authenticates in the configured
workspace. `steward check` also requires the normal separate execution identity;
local fixture tests do not prove this host boundary or a Slack login.

Ordinary top-level messages start distinct conversations; replies continue the
root thread. The complete identity is `slack:TEAM:CHANNEL:ROOT_TS`, preserving all
six timestamp decimal digits. Use that identity as a rhythm's explicit `owner`
or the CLI's `--owner`. Existing task owners never move when notification routes
change. Optional Slack notification destinations take precedence for new unowned
notices only; absent those destinations, Telegram/desk selection remains available.

## Authority and messages

Only configured users in the configured workspace/channel are admitted. Bot,
foreign-workspace, edit, delete and other message subtypes are ignored. Authority
is checked again before retained input runs, so removing a grant takes effect on
queued input as well.

Operators and contributors can converse and use `!help` plus the shared builtins (`!status`,
`!tasks`, `!task`, `!pause`, `!resume`, `!model`, `!model_family`, `!clear`,
`!cancel`, `!rhythm`, `!git`). These are ordinary channel messages, not Slack slash
commands. Telegram-specific adapter commands and administrative actions are not
exposed. Observers can use only `!help`, `!status`, `!tasks` and
`!task show <id>`; their text never starts a provider turn or a state mutation.
Task/status details are visible to channel members, so use a private channel when
those details are private.

Contributors can create documents in the shared world and request, implement, test
and manage repository work. They use the same working capabilities as operators.
Deployment is controlled separately: configure `deployment_operators` with the
explicit Slack and/or Telegram identities allowed to approve an exact revision.
The controller refuses a contributor-enabled configuration without these identities
and an explicit `publish_requires_approval` choice on every repository. Set it to
`true` wherever a push triggers deployment; use `false` only where publication
does not release software. See [deployment consent](automatic-deployment.md#operator-deployment-consent).

```yaml
deployment_operators:
  - slack:TPLACEHOLDER:UOPERATORPLACEHOLDER
  # Optional second identity for the same human, explicitly allowed in Telegram:
  # - telegram:123456789
repositories:
  app:
    path: /var/lib/steward-agent/repos/app
    remote_url: https://github.com/example/app.git
    publish_requires_approval: true
```

Each ordinary Slack input carries controller-generated workspace, user ID and
current role attribution into both the model input and retained turn. Optional
`user_names` are readable labels for configured users; names never grant authority.
Changing roles or promoting a contributor is an operator edit to controller
configuration. Queued messages are checked against current grants before running.
Telegram topics and Slack threads share the world and tasks while retaining their
own conversation histories and result destinations; enabling Slack does not copy
or mirror Telegram history.

One shared inbox drain serializes ordinary input per full thread identity while
allowing different threads to proceed. The Socket Mode receiver acknowledges retained control commands, then answers them
through the same receipt boundary so `!cancel` can interrupt a running turn even
when the conversation worker pool is full. Recovered commands use a separate
in-flight key in the shared drain. Queued events are ordered by their exact Slack
timestamps; already-running work cannot wait for events that have not arrived.
Slack does not guarantee event order, and no global chronological delivery is promised.

Inbound files receive a text refusal and do not start a turn. Outbound
`[[send_file:/absolute/path]]`, `[[send_document:/absolute/path]]` and
`[[send_image:/absolute/path]]` markers upload files within `delivery_roots`.
All path components must be real directories/files, without symlinks. Paths
outside these roots, special files and oversized output are refused before any
part of that reply is sent. At most ten files and 200,000 text characters are
allowed; `max_file_bytes` bounds the aggregate file bytes. Text is sent without
Markdown parsing or link/media unfurls. File bytes are frozen in the owning
receipt before sending, so retries cannot silently pick up edited artifacts.

## Durability and recovery

Incoming events are saved and fsynced before Socket Mode acknowledgment. Each
admitted event ID has one file in `<state_db>.slack-inbox`: queued `.json`,
claimed `.json.claimed`, completed `.json.done`, or failed `.json.failed`.
Restart requeues orphaned claims. Completed and failed receipts remain durable
deduplication tombstones; do not prune them while events may be replayed. This
retention grows with traffic. There is no second transport database or outbox.

Accepted turns replay through the conversation service's source-event receipt.
Mutating builtins record `command_started` before execution. If interrupted
before retaining their reply, they report the interruption and require inspection
of the effect instead of automatically repeating the command.

Replies retain their parts and delivery progress in the inbound file. Task and
notification delivery stores the same fields on the shared result receipt under
`<state_db>.task-results`. Each confirmed text part records a Slack timestamp and each
confirmed upload records file IDs. Result assessment never adds a second final
reply.

An explicit rate limit retains a retry time and resumes from the first unsent
part. Authentication/permission refusals remain pending for repair. Permanent
content/destination refusals settle result receipts with a rejection diagnostic;
inbound refusals park their input. `!status` reports result delivery errors and
recent permanent refusals.

Before every send, `slack_sending` records the part index. A lost response, server
failure or process interruption at this boundary leaves delivery **unknown**.
The controller will not automatically send that part again. Unknown inbound
replies park in `.failed`; unknown results remain pending, blocking later results
for that owner until resolved. A `client_msg_id` is not treated as an exactly-once
guarantee: Slack documents that [server failures can follow partial success](https://docs.slack.dev/reference/methods/chat.postMessage/).
Uploads use the SDK's sequenced upload helper, including
[completion of the external upload](https://docs.slack.dev/reference/methods/files.completeUploadExternal/).

Recovery requires a controller operator to stop the daemon, back up the owning
receipt, and inspect the destination thread and file list. If the uncertain part
was delivered, append its observed evidence to `slack_sent`, then remove
`slack_sending` and `delivery_error`. If absence is established, remove those two
fields to permit a retry. If the outcome remains unknown, leave it parked.
Requeue a repaired inbound receipt by renaming `.json.failed` to `.json`; keep its
payload, reply and event ID intact. Shared result receipts stay pending and are
picked up on restart. Never delete the event tombstone to retry cognition, or mark
an unobserved send as delivered.

## Validation and activation

The command below exercises admission,
permissions, duplicate/crash recovery, thread concurrency, cancellation,
attachments, uncertain sends, result ownership and shutdown with fake Slack API
responses:

```sh
uv run --extra dev --extra slack pytest -q tests/test_slack*.py
```

Existing inbox, conversation, result, configuration and broker tests
cover the shared primitives. These establish local behavior, not a deployed Slack
journey. Provision real IDs and controller token files, rehearse the upgrade, then
verify an operator conversation, an observer refusal, task completion in its
original thread and an attachment before selecting this transport for live work.

Back up controller state, inboxes, result receipts and accepted task refs before
activation. Older versions cannot interpret Slack conversation identities; rollback
requires the rehearsed state backup as well as the previous executable.

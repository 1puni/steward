# Upgrade an existing steward

An upgrade carries the steward's unfinished obligations as well as its code.
An empty database that boots is not continuity.

The harness opens the current schema or creates a fresh database. It preserves
an incompatible database and refuses startup. There are no automatic historical
migrations. The upgrading operator or agent inspects the actual instance and
performs a bounded, rehearsed conversion when needed.

## Identify what is running

Record the running release SHA, loaded config, service identity and launch path.
A checkout's HEAD or a moved `current` symlink does not identify an already-running
process. Use its health response and service/process evidence.

Read the target [kernel contract](docs/kernel-contract.md),
[runtime setup](docs/native-provider-runtime.md),
[execution boundary](docs/execution-boundary.md) and
[usage review](docs/usage-regressions.md). Compare the source schemas directly.
The current schema declaration is in [state.py](src/steward_harness/state.py);
an epoch number from a fork is not enough to establish compatibility.

The old schema-change notes explain historical
representations. They do not enumerate the current schema and must not be run as
a chronological recipe. Internal Python APIs are not compatibility promises.

## Upgrading onto the demolished harness (branch `demolish/primitives`)

These representations changed. Each is a bounded conversion or a deliberate
loss; none is performed automatically.

- **Accepted task documents** no longer have a `publication` field. A task ref
  whose `task.md` frontmatter still carries it fails validation and stops task
  enumeration. Before starting the new code, commit a new revision of each such
  ref without that key (the task store's own `_commit` shape; old revision as
  parent). Landing is now read from the `Steward-Work` trailer on the remote.
- **Procedure verdicts** are read from the review's findings (`VERDICT:`
  line), not stored: a procedure task whose frontmatter carries
  `procedure.result` fails validation. Commit a revision without that key,
  as for `publication`.
- **Task prose** is the accepted `task.md` body only. Product `tasks/<id>.md`
  files already on branches or `main` are inert history.
- **Provider lineage** moves from `lineage.json` into `conversations`. The explicit
  converter preserves provider, profile, generation and native session ID for every
  owner. Native homes and retained workspaces keep their original paths.
- **World acceptance** uses closing commit trailers for new turns. Historical
  accepted candidates remain identified by completed turn rows for safe workspace
  refresh. Resolve pending `world_turns` and completion files on the old code first.
  Keep old world Git stores and `episodes.md` as retained history during cutover.
- **Rhythms** now execute as procedure tasks. Historical `rhythm:<name>` identities,
  turns, lineages and workspace names remain readable; do not delete them.
- **Task locks** moved from `<workdir>/task-locks/` to
  `<state_db>.tasks.git.locks/`. Stop the old controller first; nothing to copy.
- **Task history** is read in NUL framing; nothing to convert.
- **Repository `modes`** is gone: configuring a repository is the authority
  to work in it. Delete every `modes:` key from the instance YAML, or
  validation refuses the file.
- **`orientation_repository`** is gone: a worldless steward always orients
  in its `provider.workdir` checkout. Delete the key from the instance YAML.
- **`http` and `systemd` probe types** are gone: write them as `command`
  probes (`curl --fail --silent --max-time N URL`, `systemctl is-active --quiet
  UNIT`). No instance in this repository used them.
- **`world.lease_timeout_seconds`** is gone (every instance set the default,
  5s). Delete the key.
- **One row per turn** (epoch 50): `world_turns` and the
  `<state stem>.world-completions/` files are folded into `turns`. See below.

## Inventory the actual state

Inspect without publishing or restarting. Locate:

- controller config and secrets, service units and protected source;
- SQLite, its journal/WAL state and schema definitions;
- adjacent lineage, pause/withdrawal markers, receipts and transport state;
- private controller Git stores, candidate refs and deployed refs;
- agent repositories, task branches, world history and rhythm target refs;
- native provider homes, original transcripts, memories and unfinished checkouts;
- release artifacts, receipts, current pointers and failed markers;
- ignored environments, non-Git databases and files required by the installation.

Task status combines accepted Git decisions with checkpoint trailers, observed
remote ancestry and live locks. SQL alone cannot classify whether all task work is done. Do not
flatten a task's questions, pending notes, retained branch or provider lineage
into a guessed terminal status.

Identify prepared world updates, uncertain publication, pending operator replies
and incomplete result assessments. Historical output without retained candidate
objects does not prove recoverable world edits. The [result-selection gap](docs/usage-regressions.md#result-return-can-stop-after-assessment-starts)
requires particular care when accounting for outcomes still owed to an operator.

## Rehearse the conversion

Take a consistent, recoverable copy before changing the instance. SQLite's backup
API can copy a running database, but it does not atomically capture adjacent files,
Git refs and active writers. Establish a quiescent boundary for the complete
snapshot. `/pause` is insufficient: repository convergence, result assessment
and already-submitted jobs remain active.

Work on an isolated copy. Prevent copied controller credentials and endpoints
from granting accidental production effects. Preserve source IDs and exact Git
revisions where the target representation needs them. Classify anything the new
representation cannot carry instead of silently discarding it.

The rehearsal must establish:

1. The target opens the converted database without resetting it.
2. Native session originals and the correct provider homes can be resumed under
   the real execution identity; an empty directory is not a login.
3. Prepared world work applies or remains explicitly unresolved, with no repeated
   dependent admission. Interrupted unprepared work retains its actual files.
4. Task branches, pending inputs, withdrawal and origin relationships survive.
5. Publication observes trusted remote state before claiming a result, including
   the rebased-push boundary where relevant.
6. Release artifacts remain verifiable and the prior release/absence can be
   restored. The target code and database have a compatible rollback path.
7. Ingress and result delivery resume without silently losing an obligation or
   treating a repeated receipt as fresh work.

Use the existing behavioral tests and installation acceptance paths. Record the
revision, command, identity and observed result. A suite passing in a temporary
repository does not prove authentication or a real systemd restart.

## Cut over once

After rehearsal, stop the old writers and retain exclusive controller ownership.
Make the final consistent snapshot and apply the tested conversion to that state.
Install the target config, code and permissions, run `steward check` under the
service identity, then start exactly one controller.

Verify actual running identity, prepared-work recovery, provider continuity and
an operator exchange. Exercise a small authorized task through its real gates,
publication and result return. Where deployment is owned by an external platform,
observe that platform's exact release separately. Watch configured rhythms for
accepted work; process health alone does not prove Light, Sleep or REM completed.

Keep the recovery copy until these boundaries are established. Rollback must
restore compatible code **and state**, not merely flip to an older binary. An old
binary that discards an unfamiliar schema is not a safe rollback target.

## Epoch 49: completion without a message

Historical epoch 49 stored world acceptance separately from execution turns and
retained finished native output in adjacent completion files. Epoch 50 combines
these records. Existing completion obligations must be recovered before conversion;
they are not disposable cache files.

## Epoch 50: one row per turn

Use [the explicit stopped-copy converter](scripts/upgrade-epoch49.py) under the
candidate code. It never edits its source or launches a controller:

```sh
uv run python scripts/upgrade-epoch49.py \
  --source /private/quiescent-copy/state.db \
  --destination /private/converted-state
```

The destination must be new and outside the source directory. The converter
refuses running turns, pending world acceptance, unresolved completion files and
incompatible source epochs. It preserves the old database in the destination,
all adjacent files and task history, every turn/source identity, and native lineage.
It folds accepted world output into its turn and records any reconciliation of an
old interrupted marker with stronger accepted-world evidence in a private audit.

Task document conversion retains the old revision as a parent. Historical review
verdicts missing from checkpoint messages become explicit checkpoint commits with
the same product tree and the original work as parent; they are never guessed from
prose. Existing verdicts, task holds, pending inputs and outcome receipt identities
must compare equal in the actual-state rehearsal. Retained publication metadata
remains in the private conversion report and original Git history.

Inspect `epoch50-conversion.json` and require `complete: true`; a partial destination
is not deployable. Compare task classifications against the same copied controller
remote refs. Verify native lineage fields and original provider homes, run the target
schema's integrity checks, then rehearse startup and retained-work recovery. A
rehearsal recorded for one installation is not permission to reuse an old snapshot for
a later cutover.

Apply the proven conversion to a fresh final quiescent copy. Update configuration
keys listed above separately. Preserve original workspace paths, native homes,
Git objects and receipts. Rollback requires compatible code **and** the complete
pre-conversion state, not merely the old release pointer.

## Report the outcome

State what version now runs, what was converted, what evidence survived, what
acceptance passed and what remains unresolved. Distinguish a local rehearsal,
a staged release and production cutover. Do not carry a historical test total
forward as evidence for a different revision.

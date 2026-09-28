# Named targets and executable drivers

Landing a commit and running it are different facts, and this page keeps them
apart. Publication and target satisfaction are independent. An accepted task lands one
single-parent outcome on its repository's default branch. Each configured target
then follows its own ref and uses an installed executable to observe and apply an
exact revision. A task remains landed if a target is unavailable.

```yaml
procedures:
  security-one:
    instructions: /etc/steward/procedures/security-review.md
    provider: glm
    model: {model: glm-5.3}
    access: read-only
  security-two:
    instructions: /etc/steward/procedures/security-review.md
    provider: codex
    model: {model: gpt-5.6-sol, effort: high}
    access: read-only
rhythms:
  weekly-security:
    schedule: 604800
    procedure: security-one
    input: repositories/app/main
    owner: telegram:0  # Must be a configured topic; null means retained evidence only.
targets:
  production:
    ref: repositories/app/main
    driver: /usr/local/libexec/steward-targets/app-production
    requires: [security-one, security-two]
  preview:
    ref: repositories/app/staging
    driver: /usr/local/libexec/steward-targets/app-preview
```

The instructions and drivers must be installed outside model-writable roots and
protected by operating-system permissions. Provider credentials stay in their
configured native homes. Driver credentials stay with installed driver operations.
Product source cannot name a new target, replace a driver, or waive requirements.

A procedure run is a Git-backed task with captured instructions, model settings,
configuration digest and exact input commits. Required review procedures are
read-only and must inspect the **complete candidate tree**, including defects
introduced before its last commit. For target reviews, base equals candidate to
mean a full-tree review without a diff baseline. For repository publication,
the final integrated candidate and its actual parent are supplied; the complete
tree remains in scope. This avoids overlooking a defect in B when a target moves
from A to C through B. Evidence is accepted with the checkpoint, not in a later
receipt. A changed candidate, parent or procedure definition requires new evidence.

`repositories.<name>.requires` enforces the same procedures before publication.
`targets.<name>.requires` enforces them before application and before reporting
satisfaction. Any number of independent procedures is allowed. Periodic reviews
are retained as ordinary tasks, but a periodic result never authorizes another
input. Missing capability or a review question remains visible on its task.

## Git quiet periods and intervals

A procedure rhythm runs only on new input. Before admission the controller
reads the observed refs of what the rhythm reads, [fetched by targets or once
per bucket](rhythms.md), and compares them with the commits its finished
runs captured. A repository rhythm reads its `input` ref: its input is that
candidate commit. A rhythm with a `workdir` reads the organisation: its input is
also every observed `refs/steward/remote/*` head of every configured repository
and the accepted work commit of every ordinary task, narrowed to its
[`paths`](rhythms.md) when it sets them. If every observed commit
was captured by a finished run, the poll admits nothing, calls no model and
logs at debug level. The runs' own `candidate` and `activity` fields are the
cursor; nothing else records what a rhythm has seen.

An integer `schedule: 86400` is an interval: at most one run per interval
bucket, admitted on the first poll in the bucket that sees new input. A quiet
schedule instead waits until the new input has stopped moving, then runs once
over exactly that input:

```yaml
rhythms:
  light:
    schedule: {quiet: 300}
    procedure: reflect
    input: repositories/app/main
    owner: telegram:0
```

Each newly observed commit in the rhythm's input restarts the five-minute quiet
window. Unchanged ticks, removing a ref and aliases for an already observed
commit do not. The quiet run's event identity derives from its input, so the
same input never becomes two tasks. A completed failure verdict is still
covered input. Manual requests remain explicit new work, and unfinished runs
prevent overlap. A blocked run is not unfinished in that sense and covers no
input: nothing retries it, so the rhythm's next admission cancels it as
superseded. A procedure's `provider` and `model` name a preference, not a pin:
its run leads with that provider and falls back through `provider.family_order`,
each fallback using its own configured model for the profile. `fallback: false`
makes it a pin; a procedure may instead give an ordered `models` list of provider, model and
effort entries walked in order. See [model preference](rhythms.md#model-preference).

The controller measures elapsed quiet using its monotonic observation clock,
not author or committer dates. After a restart, input no finished run captured
waits a fresh full quiet period; input already captured stays covered, because
coverage is read from accepted task Git. A controller with no finished run has
covered nothing, so its first quiet period ends in one run over the current input.

Steward bookkeeping is not input. Task documents, `steward: accept task`
commits, holds and procedure evidence live in the controller's task store, not
in any repository remote, and every procedure task, the rhythm's own runs
included, is excluded from task work. A rhythm's result therefore cannot
trigger the rhythm. A product task's outcome landing on the input branch does
count: it changes what the rhythm reads. The Git world is not a procedure
rhythm's input; the world is read by its owner's assessment, not observed for
admission. A failed fetch skips that rhythm for the poll without changing its
quiet state; other rhythms remain eligible.

A finished read-only rhythm run without findings is retained evidence only. Its
checkpoint commit names the outcome, and the result lane never selects it for
assessment or delivery; see [rhythms](rhythms.md).

## Driver protocol

The controller invokes `[driver, "observe"]` or `[driver, "apply"]` with this JSON
on stdin:

```json
{"target":"production","repository":"app","revision":"<full SHA-1>","git_dir":"<controller-owned bare repository>"}
```

Both operations have a finite timeout (`timeout_seconds`, default 300). `observe`
returns one JSON document:

```json
{"revision":"<full SHA-1 or null>","ready":true,"busy":false,"blocked":false,"details":"external evidence"}
```

`apply` exits finitely; its exit status alone never satisfies the target. It may
handoff to an independently supervised worker. The controller observes again,
and later polls repeat observation and application when still needed. Drivers
must make repeated application of the same revision safe, own external mutation
serialization, and retain any rollback evidence they need. Two target names must
not compete for the same external endpoint. Driver descendants are killed when
the finite invocation ends; long-running work belongs in a supervisor, not an
orphaned background child. Failures and timeouts remain local to that target.

`blocked` defaults to false. It reports that applying this
requested revision cannot usefully advance the target; `details` explains the
required intervention. The controller keeps observing but does not apply or
start prerequisite reviews while blocked. Readiness still describes the actual
serving revision, which may be a healthy rollback. Blocking is recomputed by
the driver, so repairing the artifact or changing the desired ref can clear it.

The source object store contains fetched branch heads and retained candidates.
A driver may export the exact requested tree. Builds must use the existing
untrusted execution broker; never execute candidate scripts with driver credentials.
Remote platforms can instead consume an immutable artifact built by their own
isolated builders. Observation must identify what the destination is actually
serving or has durably received, rather than echo a requested revision.

## Observation feedback

Only outcomes are delivered, never progress. A target has two outcomes at a
revision: **live** (observed satisfied) and **failing** (`failed`, `blocked` or
`failed-evidence`). `pending`, `busy`, `awaiting-evidence` and a moved ref are the
loop at work and produce no receipt. A failure becomes an outcome only after it
has stood for five minutes (`FAILURE_PERSISTS_SECONDS`), timed from the first
failure since the target was last live at that revision; busy passes in between
do not reset it. The controller's shutdown drain fails every observe for a pass
or two, and those failures, like any that recover within the window, are never
told. The clock is in memory: a restart looks afresh.

Each outcome is delivered once per revision. A failure that was told and then
recovers produces one "recovered" message. A failure that recovered before it
was told stays silent, because no one acted on it and a recovery notice would
describe a non-event. Transient failures that come and go within the window do
not add up to messages.

A target outcome returns to an existing conversation only when its desired
revision is exactly the commit that landed that conversation's task.
The controller retains the dated driver observation and desired SHA in an ordinary
result receipt. A live outcome carries its plain reply ("`<target>` is live at
`<sha>`"), because the observation already says everything the owner's model
could. A failing outcome carries no reply and goes through the same assessment
and world-acceptance path as task results, where the owner can act or propose an
authorized follow-up. Readiness still requires external observation and exact
evidence; the assessment does not confer deployment authority.

Driver prose and error text vary between polls and are not new outcomes.
Target locking serializes receipt selection with observation. Existing receipt
replay retains order and retries transport without repeating accepted cognition.
Full evidence remains in the receipt and the accepted world turn's commit; task
completion remains publication, independently of deployment.

The recipient rule deliberately excludes historical ancestor candidates and tasks
without an owner/publication. Coalesced or skipped intermediate candidates do not
each receive task feedback. An outcome without an exact task recipient is
retained in the same receipt store with no task ID and a plain report: live,
recovered, or not reaching the revision with the driver's reason.
The delivery lane assigns the configured `incidents` or `operator` Telegram
topic, or the operator desk route. Without a configured route it remains pending
and `/status` explains the delivery problem. No synthetic task or model assessment
is needed to report an external target's state. Ref-resolution failures likewise
remain visible without pretending that a desired SHA was established.

Result delivery eligibility is derived before dispatch. An invalid configured
topic retains its receipt and a delivery diagnostic; it is not marked delivered
or repeatedly assessed. Repairing the route allows the existing receipts to drain
in order, without repeating accepted assessment. Transport errors on a valid
route still retry through ordinary receipt replay.

A satisfied target is omitted from the dispatch queue while its local mirrored
ref is unchanged and its sixty-second observation interval has not elapsed.
A changed local ref is immediately eligible; otherwise the interval bounds the
next remote refresh and health observation. Explicit `/git target` forces a fresh
observation. Blocked, pending and failed states remain observable because external
repair can happen at the same revision. Observation verifies protected deployment
evidence without probing build-workspace readiness; apply and build require the
full execution boundary.

## Included systemd release driver

[Installed wrapper examples](../config/targets/steward-harness) invoke
`python -m steward_harness.deploy.cli --config <controller.yaml> --settings
<target-settings.yaml> observe|apply`. The [settings example](../config/targets/steward-harness.yaml)
is driver-owned configuration, outside the kernel's vocabulary.

This driver stages an immutable tree, imports optional build output through the
credential-free broker, records file-integrity receipts, atomically moves the
release pointer, and requires external HTTP health to report the full exact SHA.
Status-only health cannot satisfy this protocol. It retains the last healthy
revision under a target-scoped Git ref. A preexisting symlink is adopted as healthy
only after external health and artifact integrity agree. Rollback verifies the
previous artifact and health, or establishes explicit stopped absence.

Release manifests exclude real `__pycache__` directories and regular `.pyc`
files directly inside them. Python imports can create or refresh those caches
without blocking observation, reapplication or rollback. Source files, installed
dependencies, standalone `.pyc` files, symlinks (including cache symlinks), and
other files inside cache directories remain covered. Cache bytecode is executable
runtime output, not authenticated release content; release write access must
remain restricted to trusted identities.

For operator inspection, use the release's configured interpreter with `-B`,
for example, from `/opt/steward-harness/current`:

```sh
python3.14 -B -m steward_harness.cli --help
```

`PYTHONDONTWRITEBYTECODE=1` is equivalent and should remain set in service units.
Both prevent cache writes; they do not prevent Python from reading existing
caches. Old receipts staged without caches remain valid. Old receipts that
included caches require a freshly staged release through the normal deployment
path; do not overwrite a receipt to bless an existing tree.

Every application runs in a stable transient systemd unit. Its arguments retain
the target, exact revision and a digest of accepted controller/driver settings.
The worker rejects settings changed before it starts. It uses its own lease and
survives the controller service restarting. Install its Python environment at a
stable versioned path outside the service's mutable current pointer. A failed
revision remains condemned: subsequent apply calls may dispatch a recovery worker, but the
worker checks the failure marker under the pointer lease before any forward work.
It resumes rollback to the recorded last healthy revision without staging or
activating the condemned release. A restored pointer alone is insufficient: the
worker checks exact-revision health and artifact integrity, and restarts the prior
service if recovery is still needed. With no prior release it removes the pointer
and synchronously stops the service. An unrelated
active pointer is never overwritten. If a newer healthy release has already been
recorded and is active, recovery verifies it without restarting it.

Observation blocks a condemned revision once rollback is externally healthy,
or the absent pointer and an inactive service prove stopped absence. It also
blocks when there is no safe rollback target or a different active pointer
must be preserved. An intact pointer without service-health evidence still
permits recovery. Active artifact corruption blocks reapplication until repaired;
the original integrity receipt remains authoritative. Apply repeats this check
before dispatch, so direct calls also avoid spawning futile workers.
Health returning on the condemned revision does not make it ready or cancel
the obligation to finish rollback.

The failure marker is retained after recovery. Apply exit zero means supervisor
dispatch was accepted, a worker was already active, or observation made it
unnecessary (blocked or already satisfied);
it does not mean recovery ran or
passed. A worker recovering a condemned revision still exits nonzero for the
requested deployment; its log distinguishes verified rollback from recovery
failure. Observe reports the actual serving revision and readiness, which will
not match the condemned desired revision. Prefer moving the desired ref to a
corrected or rollback revision. Retrying the same condemned SHA requires an
operator to deliberately remove its `.<sha>.failed` marker after diagnosis,
with target convergence quiesced, before running apply; a running worker alone
does not bypass condemnation.

Artifact pruning keeps the five newest release directories plus the active
release and the explicitly retained healthy rollback release. It removes older
eligible directories and their integrity receipts, but never failure markers.
Condemnation survives artifact removal and repeated pruning, so requesting an
old failed SHA still requires the explicit operator clearance described above.
Failure markers have no age or count limit tied to artifact retention.

### Operating the installed driver

Four things read the controller configuration, and a deploy advances only one of
them. The service runs the new release. The driver's environment is pinned to the
harness it was built from and rejects unknown keys like any other loader. The
configuration file and the schema-epoch check live outside the release entirely. So a
new configuration field deploys cleanly to the service and then fails every
`observe`: self-deploy stops while health stays green. The order that works is push,
install the configuration, restart, rebuild the driver environment at the new release,
then verify with `observe`. Where a source constant would do, prefer it to a new key.
Once accepted records carry a new field, an older strict reader cannot read them, so
upgrade the driver before you introduce one.

A schema-epoch change cannot self-deploy. The new release refuses the old database,
health fails, and the driver rolls back. Convert the database deliberately; see
[upgrading](upgrading.md).

Build the driver environment from a copy of the release source, never by installing
against the release directory itself. The worker's policy digest covers the
controller and settings files, not the driver environment, so pause target
convergence before you replace it.

The driver only recognises release directories named by a full 40-hex SHA that carry
an integrity receipt. On an installation that predates it there is no previous
release to fall back to, and a failed first apply removes the pointer and stops the
service. Stage a verified full-SHA release and point `current` at it before you enable
self-deployment. The service must import the harness from `<release>/src`, not from a
per-release environment, or health identity breaks and every deploy rolls back.

The worker starts through plain `systemd-run` and inherits only `PATH`; it has no
`HOME`. Git and `gh` cannot find the controller's credentials there, and a private
remote's fetch fails with exit 128, which reads like a network fault. Configure the
credential helper in `/etc/gitconfig`. The driver's `systemctl` calls carry no client
timeout on purpose: systemd owns the job and its deadlines, and a timed-out client
leaves the job running while a rollback races a controller that is still draining.

Anything that must outlive a release lives outside it: configuration in `/etc`,
backup and monitor units under `/usr/local/libexec` or similar. A companion unit that
runs a script from inside the release fails at the first cutover, and a monitor
inside `current/` disappears exactly when a bad deploy is what you need to see.

### Running the driver from the release

Instead of installing the driver as a separate Python package, a protected stable
wrapper can execute `scripts/systemd-target.py` from the current release. Resolve the
script to its immutable release path **before** executing it, for example
`entry=$(readlink -f /opt/INSTANCE/current/scripts/systemd-target.py)`, then
`exec /usr/bin/python3 -B "$entry" --config /etc/INSTANCE/steward.yaml
--settings /etc/INSTANCE/target.yaml "$@"` (on one shell line), with your
controller interpreter and protected configuration paths.

The script prepends that release's `src` and `vendor` to the import path and calls the
ordinary deployment CLI with its resolved `worker_entry`. The supervised worker
therefore runs the same immutable script, core and vendored dependencies even if
`current` moves after dispatch, does not depend on `PYTHONPATH` surviving
`systemd-run`, and is not a second deployment implementation. Keep that release on
disk while its workers run. Installing or switching the wrapper is a protected
operator step; publishing source does not do it.

## Proving a driver

Run `scripts/linux-boundary-acceptance.sh` on the intended host before trusting the
systemd driver there. It exercises real split identities, build import, HTTP
identity and rollback in a disposable environment, and it needs Linux and root.
Any other platform driver needs its own proof of external observation and of what
happens when it is interrupted halfway. A driver you have never seen roll back is a
driver that cannot roll back.

Configuration keys from older versions (`repositories.*.deploy`, `executor`,
`health_reports_sha`, `work_branch`) fail validation; platform settings belong in
the installed driver's own configuration, bound through a named target. Use
`/git target <name>` for an immediate convergence attempt. Moving an instance onto a
new harness revision is a deliberate migration; see [upgrading](upgrading.md).

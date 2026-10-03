# Watching a live steward

A failing steward can look exactly like an idle one. It keeps answering its operator
while a rhythm has not accepted a run in days, restarts hundreds of times behind a
green health check, or defers the same world update at INFO forever. Being responsive
is not being healthy, and none of those failures announce themselves.

## Ask whether it happened

Ask positive questions: did the rhythm accept a run, did the task land, did the
target observe the revision? "Is anything wrong?" is answered by silence, and silence
is also what a broken monitor produces.

- Check pause first. A paused steward and a quiet one are indistinguishable from
  outside.
- Derive what is due from the steward's own configured rhythms, and list configured
  rhythms that have never run. A quiet rhythm with no new activity is not overdue.
  A monitor must not carry a second schedule evaluator with its own idea of age.
- Count occurrences inside a window, not rows in a state. Supported replay can move
  a row back out of a failed state, so a current-state count is not monotonic.
- Count repeated identical lines at INFO too. A deferral that never clears is an
  outage with a heartbeat.
- Count restarts.
- Count what rhythms recorded without sending. Rhythms are silent unless they
  call `notify` ([what a rhythm sends](rhythms.md#what-a-rhythm-sends)), so a
  quiet topic proves nothing either way. `/status` gives the 24-hour count, and each
  silent world-rhythm reply logs `reply recorded, not delivered`. This proves
  recorded execution, not useful reflection. Absence from a bounded count or
  transport history does not prove a missed execution: inspect the current
  admission state, input guard and canonical turns.

The desk probe prefers the controller's timestamped `world_rhythms` health
observation over offline estimates. It reports observation age and worker
pressure even when task-store reads fail. The watch reports stale or missing
admission evidence separately from execution failures. With fresh evidence and
admission unpaused, it names held obligations, waiting chains and eligible unstarted intervals
older than five minutes; this is a visibility threshold, not another schedule.
A world turn is identified by its source key, not a nonexistent task revision.
When reading retained probes, parse anchored `rhythm_progress=` records and
retain their observation timestamp; prose quoting that string is not a probe. For
world Git evidence, use Git's `%(trailers:key=Steward-Source,valueonly)` formatter
and match the complete attempt source: `rhythm:<name>:<interval>` or its
`:continue:<previous turn id>` suffix. The admission observation names the
logical obligation separately from its latest attempt. A body grep can select
reflection prose; even a genuine commit timestamp is acceptance evidence, not
an execution start timestamp.

## Build a monitor that cannot lie quietly

- Hard thresholds are computed in code and fire even when no model is reachable. A
  model may interpret numbers the monitor already measured; it never measures.
- A failed probe, an unreachable provider or an unparsable verdict is reported,
  never swallowed. A monitor that printed nothing is broken, not healthy.
- Notification silence starts only after a send succeeds.
- Every count carries the window it was measured over. A monitor resumed after a gap
  reports its whole absence as one interval; a stale window feeds on itself.
- A paging rule that has never been right goes to the report, not to the phone.
- A monitor's reads can be writes. Run Git and SQLite reads as described in
  [inventory the actual state](upgrading.md#inventory-the-actual-state),
  and check that its unit name is not already taken.
- The watch stays a liveness guard. It alerts while the controller is down; repairs
  arrive as ordinary admitted tasks, not through a second executor.

## Know what a number measures

- A service's cgroup memory includes file cache, which Git I/O dominates. Measure the
  controller as process RSS. A systemd memory peak is a lifetime high-water mark and
  cannot be compared across restarts.
- Measure CPU as a rate between two samples of the same PID, not as a lifetime
  average. Cgroup CPU keeps the time of children that already exited, while process
  counts are one instant: high CPU with no live children means spawn churn, visible
  in the controller's `cutime` and `cstime`.
- Model-spawned processes reparent to PID 1 but stay in the service's cgroup, so they
  inflate every cgroup figure until they exit.
- `smaps` can locate memory growth but cannot attribute it. Delayed cyclic garbage
  collection and allocator retention look like a leak; `gc.collect()` and
  `malloc_trim` tell them apart. Compare baselines only on the same release.
- A curve fitted to one aggregate with two free parameters proves nothing, however
  small its error.

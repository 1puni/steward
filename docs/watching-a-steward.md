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

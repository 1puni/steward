# Durable understanding during native work

Agreed direction, 2026-09-23. This is the initial implementation brief and
acceptance baseline, not a claim that the behavior is already implemented.

## Objective

A native task may work for days with its own subagents and tools. Steward needs
its understanding and decisions to become durable throughout that work. It does
not need the agent to stop merely to produce a tick or a decision log.

Separate three boundaries: accepting understanding, ending native execution,
and authorizing publication. Controller polling observes and derives work; it
does not manufacture a semantic stopping point. A clock can motivate observation
or a request for an update. Routine task continuity must not depend on timed
interruption, process teardown, autosave and resume.

## The account and its owner

The existing Git task document remains the canonical account. Useful updates
explain current understanding, decisions and rationale, supporting evidence,
unresolved questions, delegated investigations still running, and what should
happen next if execution is lost. Do not require a new rigid taxonomy for prose
the existing document can already express.

The native parent owns consolidation. Child findings are evidence for it, not
independent authority for the controller to rewrite the task. Steward accepts
explicit proposals; it does not infer decisions from tool chatter or heartbeats.
Silence means the last accepted account is old, not that the task has failed.

## Required live handoff

The parent offers an explicit immutable version of its task account while its
native turn remains active. Steward validates and retains that version in the
existing accepted Git history, then acknowledges acceptance with an identity
that recovery can rely on. Reuse existing task authorship and concurrency rules.

Accepting this account must not stage arbitrary product files, move the native
worktree's HEAD or index, terminate children, consume unrelated operator input,
change protected task authority, or make a publication eligible. The account
update does not certify that a concurrently edited product tree is coherent.
It must not overwrite a concurrent operator edit, withdrawal, or newer accepted
understanding. Failed or conflicting acceptance must be visible; a request or a
local file write is not an acknowledgement.

Native commits and artifacts remain deliberate work retention. Native session
records remain recovery context. Neither an accepted account nor a saved session
ID promises that killed processes or unwritten child state can be reconstructed.

## Execution and closure

Allow ongoing task cognition to outlive the ordinary provider turn deadline.
Do not replace it with a larger arbitrary timer or an automatic restart loop.
Keep finite limits on genuinely finite external operations, and keep explicit
operator cancellation and emergency containment distinct from continuity.
State precisely which execution kinds retain deadlines; do not silently change
gates, deployments, probes or interactive conversation policy.

Natural native completion can still produce the existing continue/ask/idle
closure. Publication still requires the existing settled-work, exact-input gate
and authority checks. Live acceptance supplies no substitute for those checks.
The parent need not recall working children just to record understanding.
Existing Linux process ownership must still contain work after controller death.

## Smallest sufficient implementation

Use Git, the task document, existing acceptance operations and native input/output
boundaries. Select one explicit proposal representation and one acceptance path.
Avoid a progress database, event-bus framework, general scheduler, independent
child registry, hidden periodic commits, or parallel task state machine. An
ephemeral routing mechanism may transport a proposal; it must not become a
second durable authority. Document any new primitive and why existing ones
cannot provide its invariant.

## Acceptance evidence

The primary journey is a running native parent with a child actively working.
The parent offers a meaningful understanding update; Steward accepts it and
acknowledges it while both processes remain alive. Product files can still be
mid-edit and are not swept into the account update. An operator can inspect the
accepted account while the task still runs. A fresh controller/store recovers
that account after the original execution is lost, without claiming the child
survived. Later natural closure still follows ordinary gates and publication.

Also establish:

- Duplicate offers do not produce duplicate accepted decisions.
- A stale or conflicting offer cannot erase operator changes or cancellation.
- Malformed offers and attempted authority changes fail without killing healthy
  native work or granting publication.
- Healthy task work continues past the former routine deadline with its child
  alive; explicit cancellation still works and containment still holds.
- Both built-in native adapter paths can perform the handoff, or any unsupported
  path is stated explicitly rather than silently claiming provider neutrality.
- Acceptance is visible through existing task inspection, including the operator
  surface when configured; no new reporting subsystem is necessary.

Use real process/adapter fixtures and native Linux ownership checks where they
establish the boundary. Test totals alone do not prove this journey.

## Independent review and server execution

An independent reviewer checks the implementation against this initial brief and
the [engineering doctrine](engineering-doctrine.md), especially authority,
representation, failure boundaries and unnecessary mechanisms. Keep the initial
requirements distinguishable from subsequent implementation and evidence notes;
do not rewrite the requirements merely to fit the result.

The user authorized moving this work to a separate server workspace so it can
continue without the laptop connection, optionally with operator Telegram
check-ins. Use a uniquely named, isolated workspace and durable server-side logs.
Do not replace or restart the live steward as part of setting up development.
Never reuse or remove existing test projects, state stores, containers or volumes.
Record the host, workspace, supervisor/session identity, check-in route, and
actual start/verification status. A copied brief or a running shell is not proof
that an autonomous worker is making progress.

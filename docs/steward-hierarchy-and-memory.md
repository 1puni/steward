# Steward hierarchy, worlds and durable knowledge

See [ways to use and connect stewards](stewardship-arrangements.md) for the
arrangements this architecture serves and their current implementation limits.
The hierarchy describes ownership scopes; it does not require a separately
running controller at every level or a personal steward above every organisation.

## One kernel, different scopes

Personal stewards, organisational stewards, and repository stewards use the same steward kernel.
Their roles differ by scope, authority, and the children to which they may delegate—not
by runtime or memory technology.

```text
personal steward (meta-meta)
└── organisational steward (meta)
    └── repository steward
```

Each steward has an identity, a charter, an ordinary Git world, bounded capabilities
and durable provider sessions. Children are a matter of ownership scope; automated
delegation between stewards is not built yet (see [kernel boundary](#kernel-boundary)).

One ongoing steward conversation can own multiple tasks. Each task keeps its execution
identity and returns results to its originating conversation; starting or finishing a
task does not take over the thread. Conversation controls address the steward session,
and task controls identify one task.

## Git worlds are the external memory

Native providers keep writing their own memory and session records. Their readable
artifacts go into the Git world, where any agent, from any provider, can inspect them. The
[native runtime](native-provider-runtime.md) maps native memory and original provider
session records into the current worktree. Private authentication and runtime databases
remain outside Git. See [native record provenance](native-record-provenance.md).

Every Git boundary uses `docs/` for consolidated, human-readable truth at that scope.
Models orient themselves from the files and Git history in that world. The kernel does
not provide a semantic retrieval or memory synchronisation layer.

```text
personal world/docs/      personal and cross-organisation truth
organisation world/docs/ organisation policy and cross-repository truth
repository/docs/         project truth
```

Provider sessions supply conversational continuity. Episodes, transcripts, and receipts
supply chronological evidence. `docs/` supplies durable truth. Git history supplies
provenance. These forms are related, but they are not interchangeable authorities.

## Ownership law

> A durable fact belongs to the lowest steward that owns everything the fact
> concerns.

Its canonical home is the lowest common ancestor of its subjects:

- repository-local architecture, operations, and decisions belong to the
  repository;
- relationships or decisions spanning repositories in one organisation belong
  to that organisation's world;
- personal intent and decisions spanning organisations belong to the personal
  world.

Where a fact was discovered does not determine where it belongs. Parents store their own
relationships, policies, priorities, and references. They query children for current
status instead of maintaining copied project truth. Generated summaries may restate
child facts when they carry a source and `as_of`, but they are disposable views rather
than editable authorities.

## Conduct

> Intent flows down. Work happens at the owning scope. Evidence and references
> flow up. Truth stays with its owner.

Consequently:

- A personal steward delegates to organisational stewards instead of editing their
  repositories or acquiring their credentials.
- Organisational stewards delegate repository work into the owning repository
  context.
- Repository tasks change code and repository documentation together in the
  same gated worktree.
- Cross-scope discoveries return in ordinary task results with source
  references; the receiving owner decides what becomes durable truth.
- There is no automatic bidirectional memory synchronisation.

## Steward self-maintenance

A stewardship may include the harness that runs it within its own managed scope. Its
identity, charter, episodes, and cross-repository operating knowledge remain in the
steward Git world. Its executable harness source and deployment configuration are
registered as an ordinary managed repository, with the same gates and deployment policy
as any other service.

This makes self-maintenance compositional rather than privileged:

```text
rhythm observation or confirmed incident
  → ordinary task in the harness repository
  → isolated checkpoint and gates
  → exact-SHA landing
  → deployment convergence
  → release activation and systemd restart
  → health verification or rollback
```

A rhythm may maintain the steward world directly and may propose harness work; probe
policy retains self-healing authority. Merely placing executable files in `world.root`
does not grant a rhythm permission to publish or restart the service. The harness
repository must be configured under `repositories`, and an operator-triggered rollout
uses `/git target <name>`. The observed default branch drives automatic
deployment when that repository has a deployment policy.

This pattern cannot resurrect a completely stopped controller by itself. systemd or
another external supervisor must first restart the process; retained task/world work and
observed release state then supply the evidence for continuation.

## Kernel boundary

The shared kernel owns deterministic plumbing: provider-neutral session lifecycle,
scoped execution, leases, idempotency, Git checkpoints, publication, task admission,
result correlation, gates, landing, deployment, and scheduled activation.

Cross-scope delegation is architectural intent, not a current harness feature. Any
future transport must authenticate explicit capabilities without sharing repository or
deployment credentials. A shared database between stewards does not qualify: it was
tried, and it could not enforce the authority boundaries this hierarchy implies.

The steward and its files own interpretation, search, consolidation, and documentation.
There is no harness-owned memory corpus, append marker, reflection pass, semantic index,
or memory synchronisation protocol.

Reflection passes (the bundled
[reflection skill](../src/steward_harness/skills/steward-reflection/SKILL.md) knows
three shapes: light, sleep and REM) are ordinary procedure tasks triggered by
[rhythms](rhythms.md) the instance configures. They use the same provider runtime,
retained worktrees and acceptance boundaries as any other task. Their meaning lives in
procedure instructions and files; their schedule lives in controller configuration.
There is no separate memory engine.

# Git, Sleep, and Executive Function for Non-Executive Fucks

> This is the opinionated why. It is not the normative kernel contract.

Here is the whole fucking trick: Git is a filesystem with infinite history.

That's it. That's the most powerful thing in the world.

You can put code in it. You can put knowledge in it. You can branch reality,
let some intelligence fuck around in the branch, inspect exactly what changed,
test that exact universe, and then decide whether it deserves to become real.

We keep building elaborate memory systems, orchestration fabrics, agent graphs,
event buses, and semantic goo because apparently a directory full of readable
files with decades of battle-tested time travel was too obvious.

Steward Harness is what happens when you take Git seriously—and admit that
intelligence, human or artificial, could use some help with executive function.
Heyo, you non-executive fucks. Including myself.

## Git is the world

A normal filesystem tells you what exists now. Git gives you what exists now,
everything that existed before, and cheap alternate futures branching from any
point in that history.

That is an absurdly powerful primitive:

- files are current, human-readable truth;
- commits are immutable snapshots with provenance;
- diffs are the precise meaning of a change;
- branches are possible futures;
- merges reconcile those futures;
- remotes replicate and transport them;
- checkout and revert make recovery ordinary rather than heroic.

Git does not merely remember the past. It makes alternate futures cheap enough
to explore safely.

That is almost exactly what autonomous intelligence needs. Give it a real
world made of files. Give it a private possible future. Let it propose a
change. Do not confuse its proposal with authority to declare reality.

The model proposes; the harness disposes.

## Models do not need magical memory

A model does not need a proprietary memory palace full of embeddings and
half-recalled summaries. It needs somewhere durable to wake up.

In Steward Harness, that place is an ordinary Git world. Durable knowledge is
written as ordinary files under `docs/`, at the lowest scope that owns the
truth. Repository facts live with the repository. Organisation facts live in
the organisation world. Cross-organisation intent lives above them.

The current files say what we believe now. Git history says how we got there.
Humans can read both. Models can read both. Standard tools can read both. A
future model from a provider that does not exist yet can read both.

There is no bespoke memory format to migrate and no hidden synchronisation
layer deciding which version of a thought is canonical. Knowledge changes the
same way code changes: visibly, reviewably, and with provenance.

This does not mean shoving every operational detail into Git. SQLite owns the
small amount of scheduler state needed to resume exact work. Trusted remotes
and live releases own their external truth. Git owns the durable worlds and
the proposed changes to them. Each thing gets to be what it is good at.

## Executive function is a harness problem

Humans and models can be astonishing at association, language, invention, and
recognising that something is deeply fucked.

We are less reliably astonishing at choosing one thing, inhibiting twelve
other interesting things, remembering the exact sequence, checking whether we
actually finished, and resuming honestly after being interrupted.

That cluster of boring abilities is roughly what we mean here by executive
function. This is an engineering analogy, not a claim that the harness is a
brain or that our rhythm names are computational neuroscience.

The harness externalises the boring parts:

1. Admit one bounded intention.
2. Give it one isolated branch and worktree.
3. Let cognition work inside that world.
4. Checkpoint whatever actually happened.
5. Test the exact result.
6. Publish exactly that tested result—or do not publish it.
7. Observe external truth and resume honestly after interruption.

The intelligence gets room to be intelligent. The deterministic harness keeps
the keys, remembers the sequence, and refuses to hallucinate completion.

This is not a limitation on autonomy. It is what makes serious autonomy
possible.

## A task is intentionally boring

A task is not an agent, a worker, a workflow, a persona, a graph node, or a
tiny distributed system wearing a hat.

A task is one admitted intention attached to one retained Git branch.

The model works and closes a turn with one of three dispositions:

- `continue`: there is more bounded work to do;
- `ask`: an answer is required before honest progress can continue;
- `idle`: cognition is finished, so the harness may evaluate the work.

That is enough.

The harness checkpoints the tree the model actually produced. It rebases the
candidate in a scratch integration worktree, runs the declared gates, records
the exact tested SHA, and publishes only that SHA if the external base still
permits it. Deployment and rollback follow the same evidence-bearing path.

If the process dies, nothing needs to invent a story. The retained branch,
checkpoint, test evidence, remote refs, and live release say what is true.

Extremely simple task execution is not an absence of capability. It is the
compression that makes the capability legible.

## Why the steward sleeps

Useful cognition does not happen in one permanent mode.

Humans notice things, focus on immediate work, consolidate experience, prune
stale beliefs, and sometimes make strange cross-cutting connections when the
obvious linear process shuts up for a minute. Steward rhythms borrow that
shape. They do not pretend the machine is literally asleep or dreaming.

### Light

Light is cheap, frequent awareness. What changed? What arrived? What deserves
attention after the world has settled long enough to avoid reacting to every
filesystem twitch?

It is the steward looking around without turning every observation into a
project.

### Sleep

Sleep consolidates. It revisits current documentation, operational health,
stale work, and contradictions. It compresses episodes into durable truth and
removes knowledge that no longer deserves to survive.

This is how a world stays coherent instead of becoming an attic full of every
thought anyone ever had.

### REM

REM loosens the associations. It looks across distant parts of the world for
patterns, strategic opportunities, obsolete machinery, and the surprising
useful shit nobody explicitly requested.

Not every REM thought should become work. That is the point of separating
cognition from authority. A rhythm may update its owning Git world or propose
an ordinary task. It cannot manufacture permission to land code, deploy a
service, or declare an incident.

The same tiny kernel still disposes.

## The loop

The pieces reinforce one another:

```text
Git world
  → light notices change
  → sleep consolidates experience
  → REM discovers a cross-cutting possibility
  → ordinary task proposes one bounded future
  → isolated worktree contains the experiment
  → gates prove one exact SHA
  → publication makes that future real
  → Git remembers the new world forever
```

There is no separate intelligence architecture hiding between these steps.
There are different prompts, scopes, schedules, and authorities using the same
world and the same task machinery.

The simplicity is the power.

## Security is the separation between imagination and authority

An autonomous system should be able to imagine aggressively and act
conservatively.

Providers, model-controlled Git, candidate gates, probes, and adapters run on
the untrusted side. Push credentials, Telegram tokens, signing authority,
release mutation, service control, and rollback remain on the controller side.

The model may create a brilliant future or a terrible one. Either way, it
created a branch.

The controller decides whether the exact candidate passed the exact gates
against the exact expected world. This is why the abstraction can be both
powerful and secure: possibility is cheap; authority is narrow.

## Live systems write the next roadmap

Downstream stewards are heavy live users of this harness. They exercise it
under real organisational load, including using the harness to maintain the
harness itself.

When they find a defect, omission, or unexpectedly powerful generalisation,
they can make a bounded fix and send it back as an ordinary pull request. We
can inspect the exact proposed future, separate portable kernel truth from
local policy, demand evidence, and choose whether it becomes shared reality.

That is the roadmap now. Not a speculative feature inventory. Real worlds find
the gaps; Git carries the lessons home.

## Real-world examples will live here

We will expand this document with concrete stories from live stewards: failures recovered, knowledge consolidated, surprising REM
connections, self-healing pull requests, and cases where one small abstraction
removed an unreasonable amount of machinery.

Those examples should be real. Until we have them, we will not manufacture
parables to make the architecture sound clever.

For now, the claim is simple:

> Put truth in files. Put the files in Git. Let intelligence propose changes
> to a branch. Put a tiny deterministic bastard in charge of deciding what
> becomes real.

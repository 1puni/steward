# Engineering doctrine

This is how the harness is built, and how anything working on it, human or
model, is expected to think. It is opinionated on purpose. Parts of it
contradict conventional "best practice", and they mean to. Do not quietly
swap the common habit back in because it feels safer. It isn't. It is just
more familiar.

> The smallest system that correctly represents reality is usually the most
> robust one.

Complexity is not robustness. Generality is not robustness. Defensive code is
not automatically robustness, and an abstraction is not automatically
architecture. Every concept you add has a cost, so it carries the burden of
proof.

## Do not model the mess

Model the structure that generated the mess.

The goal is not to reproduce accidental complexity faithfully. The goal is to
understand the underlying system well enough that the unnecessary complexity
disappears. A good solution feels more inevitable the better you understand
the problem. If it feels more *clever*, be suspicious.

Here is the test we like best. Write the whole machine as pseudocode, without
borrowing a single class, module or table name from the implementation. For
this harness it comes out at roughly a hundred lines: observe, derive the
desired state, execute, verify, commit. Now compare that with what is
actually implemented. Fifteen thousand lines implementing fifteen thousand
lines' worth of irreducible behaviour is fine. Fifteen thousand lines
implementing a hundred lines of idea is entropy.

And the uncomfortable corollary: **if 90% of a system is translating the same
underlying data into different shapes, the ratio itself tells you the system is
broken.** Nobody wrote that on purpose. It is what overzealous writing without
understanding looks like once it has had a few months to compound.

## Twelve principles

### 1. Find the natural taxonomy first

Before you implement anything, find out what kinds of things actually exist in
the problem. What are the fundamental entities? Which distinctions between them
are real, and which were invented by the current code? What belongs at the same
level, and what does not?

Existing code is evidence, not truth. If the problem has five meaningful ideas
and the implementation exposes fifty equally important concepts, assume the
abstraction is wrong until proven otherwise.

### 2. Reduce the state space before handling edge cases

For every branch and edge-case handler, ask: *can this situation physically or
logically occur, given the system's invariants?* If it cannot, delete the state
instead of handling it.

Prefer impossible states made unrepresentable, invalid transitions structurally
prevented, narrower types, narrower interfaces and smaller authority surfaces
over ever more sophisticated defensive logic. Robustness comes mostly from
reducing what the system is *capable* of doing wrong.

### 3. Complexity must justify itself

When code becomes hard to reason about, do not first ask how to organise the
complexity. Ask whether it should exist. Is the problem modelled wrongly? Are
impossible cases being represented? Are two concepts actually one? Is one
concept doing several unrelated jobs? Is historical baggage posing as a
requirement? Is the code reproducing the shape of an earlier implementation
rather than the shape of the problem? Can an invariant replace a branch, a
constraint replace an instruction, a deletion replace a subsystem?

Do not refactor bloat into more aesthetically pleasing bloat.

### 4. Keep essential complexity, attack the accidental kind

Some complexity belongs to reality: real concurrency, external failure modes,
protocol semantics, distributed state, security boundaries. Keep it. Pretending
it away is not simplification, it is a bug with good manners.

Everything else, attack: duplicated representations, adapters between things
that should already speak the same language, speculative abstractions,
defensive handling of impossible states, configuration that configures
configuration, and wrappers with no semantic responsibility. The goal is not
minimal code at any cost. It is minimal *accidental* complexity.

### 5. Prefer invariants over instructions

If something must never happen, do not rely on documentation, convention,
prompts or discipline to prevent it. Make it structurally impossible.
Permissions over warnings. Schemas over prose. Types over comments. Separate
processes over behavioural instructions. A rule that has to be remembered is
weaker than a system that cannot break it.

### 6. Separate intelligence from authority

> The model proposes. The harness disposes.

Reasoning capability and execution authority are different things. The model
may reason as broadly as it likes; its authority stays narrow, explicit and
enforced from outside. Gates, permissions, repository protection and deployment
controls live outside the model's discretion, and the model does not get to
weaken them because it can argue convincingly about them. Intelligence works
inside strong boundaries. It does not replace them.

### 7. Decompose by meaning, not by file size

A component deserves to exist when it owns one coherent concept, invariant or
transition. You should be able to describe it in one precise sentence. If the
description needs a paragraph of unrelated "and also"s, it is several
components wearing one coat. Many small files that leave the conceptual
coupling untouched are not decomposition. They are confetti.

### 8. Search upstream for the shared cause

When several symptoms appear, do not assume several bugs. Ask: *what is the
smallest upstream cause that explains the largest number of downstream
observations?* Several awkward implementations usually mean one wrong
abstraction. Several failures usually mean one broken invariant. If component B
is awkward because component A exposes the wrong concept, fix A and delete the
compensation in B.

### 9. Make the representation do the work

Choose data structures, schemas and state machines that make correct behaviour
the natural behaviour. A good representation *removes* code. If a lot of logic
exists only to interpret or repair the representation, the representation is
wrong. Keep one canonical form. Keep derived information derived. If two pieces
of state must be kept in sync, first ask why there are two, because the best
synchronisation mechanism is usually deleting one of them.

### 10. Do not add generality without evidence

No plugin systems for plugins nobody has written. No interface for its one
implementation. No configuration for a case nobody has. Generalise when two
real instances reveal their shared structure, not when one imagined future
consumer might appreciate it.

### 11. Treat existing code adversarially

Existing *behaviour* may be required. Existing *structure* is not. Separate
externally observable behaviour and genuine invariants from historical
workarounds and obsolete assumptions. Every existing abstraction has to keep
earning its place. Sunk effort is not an argument. Neither is a passing test,
because a test verifies the implementation we happened to build. It does not
prove that implementation should exist.

### 12. Global simplicity beats local elegance

A helper can be beautiful while the system is incoherent. The recurring failure
in software is making every component locally tidy, defensive, reusable and
complete while the whole becomes incomprehensible. Judge each decision at
repository scale. Does it add a concept, a source of truth, a lifecycle, an
authority holder, another place state can diverge? Or does it just move the
complexity somewhere you are not looking?

## The operating system already solved this

A mature operating system, filesystem, language runtime and version-control
system embody decades of work on composition, isolation, persistence,
concurrency and observability. Use them before building weaker copies inside
the codebase.

**The filesystem is a state primitive.** A file can be human-readable
configuration, machine-readable configuration, durable state, an audit
surface, a diff, a trigger and a synchronisation boundary, all at once, and
every ordinary tool already knows how to handle it. Do not hide a direct
filesystem transition behind three layers of repository classes because it
feels "low level". Read the file, transform the data, write the file, and
respect atomicity where it actually matters.

**Git is part of the state machine.** It is not a developer tool that sits
outside the application. A branch holds proposed state. A commit is an
immutable description of a transition. The diff says exactly what changed.
Integration is acceptance. The default branch is accepted desired state, and
deployment realises it. That gives you provenance, rollback, inspection,
ordering and synchronisation from machinery that already exists and already
works. Do not rebuild it in an application database because someone once said
"Git is not a database". Understand the guarantees and use them deliberately.

**Change the source of truth, not every consumer of it.** If changing the model
for a rhythm is conceptually a configuration change, it is a configuration
change: edit the YAML, commit it, let the normal path apply it. Not a runtime
override table, a model registry or a parallel control plane.

**Prefer the language.** In Python that means `functools`, `itertools`,
`collections`, `pathlib`, generators, context managers and the sort you
already have, before any home-made infrastructure. Think in transformations
where you can (input, filter, map, reduce, output) and keep explicit state for
the parts of reality that are actually stateful.

The [representation example](engineering-doctrine-example.md) walks through the
classic case: a task that wanted to be a database row, an ORM model, a status
column and a sync job, and turned out to be a file in Git.

## Before you delete anything, ask two questions

"If I delete this, what concrete required behaviour becomes impossible?" is the
right question. It comes too late if you skip these two.

**Is it an invariant at all?** Much of what a codebase enforces is a performance
wish, a tidiness habit or a fear, written in the grammar of a rule so nobody
argues with it. "A withdrawn task must stop its running gate" reads like
correctness. It isn't. The requirement is that withdrawn work must not *land*.
Whether the gate finishes first is a question about wasted CPU, and wasted CPU
is not an invariant.

**Does the representation already hold it?** An invariant can be held by
design or by code, and only one of those needs maintenance. If withdrawal is
checked by the one writer that can push, nothing else has to check it. The
same invariant held by a status column needs a predicate at every stage that
might act, plus a recovery pass for rows whose process died. That code is the
price of choosing a representation that does not hold the invariant.

An invariant that survives both questions gets code. One that fails the first
was never real. One that fails the second is telling you the fact lives in the
wrong place, so move the fact instead of writing the check.

## Demolish, then fill

We call this *stormbeslå*. It is the one method here we will defend against
anyone.

Do not refactor by editing small pieces. The failure is structural, not a lack
of discipline: a small edit has no constraint on its own size, so each one is
locally defensible while the total grows. That is exactly how a well-behaved
session leaves every commit green, every decision reasonable, and the source
seven hundred lines *larger* than it started.

So invert it. Pick a whole concept, not a convenient file. Delete it
everywhere: the store, the module, its callers, its synchronisation, its
recovery path and the tests that only protect that machinery. Leave a hole with
the replacement primitive written on the rim. Do that everywhere in the concept
before you fill anything. Now the holes are visible, the line count has already
fallen, and no fill can quietly cost more than what it replaced, because what
it replaced is a number you can read.

- A hole is filled by a primitive, or it is not filled. If the fill turns out to
  be another module, name the primitive it embodies or revert it. If it
  recreates the subsystem you just removed, the representation is still wrong.
- Never leave half a workflow on the old representation and half on the new,
  joined by a synchroniser. That synchroniser is the bug.
- Do not polish code that is about to be deleted.
- Do not take a line budget. A budget is an instruction, and instructions get
  satisfied by compressing syntax, which is not the same as removing a concept.

The measure is semantic compression: fewer ideas needed to explain the same
correct system. Count concepts, independent facts, state transitions, authority
holders and execution paths. Report source size as a symptom, never as the goal.
A fact moved from SQLite into a pile of JSON files has not disappeared, it has
just lost its transactions. A move into Git pays only when Git's own
representation removes a second truth and the code that maintained it.

### The convergence check

During a structural refactor, every few commits hand the diff to a separate,
read-only reviewer (a model is fine) along with this page. It must not edit or
run the suite. It answers from the diff, not from the implementer's summary:
which concept disappeared, which primitive now owns the behaviour, and how the
number of independent representations changed. `scripts/converging.sh` prints
the arithmetic.

Call it **drifting** when source rises, a new module names no primitive, a
responsibility merely moved, one subsystem replaced another, or a green suite is
the only evidence offered. Two of those at once means **stop**, and name the
first change to reconsider.

## Tests protect invariants, not implementations

Tests should establish externally meaningful behaviour. A test whose only job is
to preserve incidental structure makes improvement harder, and when you delete
something and a test fails, first decide which kind it was.

A passing suite must say what it establishes. Local Git and SQLite fixtures do
not prove that a deployed steward can authenticate, prepare its native
workspace and complete a rhythm under its real service identity. A green macOS
run says nothing whatsoever about a Linux UID boundary. A failure test must
reach the boundary it names and fail for *that* reason; an unrelated
`TypeError` under an expected-failure marker proves nothing. Test totals are not
evidence. Carrying an old total forward to a new revision is worse than
reporting none.

Work from the operator journey downward: message in, work retained, candidate
accepted, exact revision gated and published, release healthy, result
delivered. Assert those facts at their real boundaries, with focused checks for
the failure and authority edges that remain.

## Challenge the premise

A task description is not necessarily a correct decomposition of the problem.
When asked to add another handler, state, service, option or edge-case patch,
first find out whether the mechanism should exist. It is allowed, and expected,
to answer:

> This should not be built as requested, because a simpler upstream change
> removes the need for it.

Then explain the structural reason and build the simpler thing, if it preserves
what was actually wanted.

## Warning signs

Treat these as signs the model is wrong, not as the cost of doing business:

- several modules translate between near-identical shapes of the same data;
- comments explain why apparently impossible things can happen;
- abstractions exist mainly to coordinate other abstractions;
- configuration options interact combinatorially;
- tests need extensive mocking of internal machinery;
- several components can mutate the same state;
- correctness depends on an execution order nothing enforces;
- security depends on model obedience;
- a simple feature touches many unrelated places;
- deleting one component requires understanding all of them.

## When unsure, prefer

Fewer concepts, states, sources of truth, authority holders, lifecycles and
irreversible transitions. Stronger invariants, narrower interfaces, clearer
ownership. Not necessarily fewer lines: a ten-line abstraction that introduces
three new concepts can be worse than twenty explicit lines using concepts the
system already has.

## The final question

> What can I understand more deeply so that I no longer need to build this?

The system is finished when it is easier to explain than the pile of details it
is made from. If understanding it means memorising exceptions, it is not
finished. If a newcomer must first learn hundreds of incidental facts, the model
is probably wrong. And if improving the model makes a lot of code disappear,
improve the model.

The ideas above have ancestors, and we are happy to name them:
[a nod to the ancestors](doctrine-ancestry.md).

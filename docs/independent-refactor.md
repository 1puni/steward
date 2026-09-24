You are entering this repository for the first time.

Your task is to refactor it according to the engineering philosophy already documented in the repository, with the additional interpretation below. Read the repository documentation, charter, architecture notes, configuration, tests, git history where useful, and implementation before making substantial changes.

Do not begin by fixing files one at a time.

First understand what the system is.

I have a very opinionated view of software engineering, and it differs materially from conventional “best practice”. You must not silently substitute common engineering practice for the principles below.

The governing idea is:

The smallest system that correctly represents reality is usually the most robust system.

Complexity is not robustness. Generality is not robustness. Defensive code is not automatically robustness. Abstractions are not automatically good architecture.

Every additional concept in the system has a cost and therefore carries the burden of proof.

That includes abstractions, interfaces, wrappers, indirection, configuration, explicit state, branches, fallback paths, retries, compatibility layers, error hierarchies, helper frameworks, validation, defensive handling, extension points, factories, dependency injection, persistence layers, caches, queues, registries and representations of information that can instead be derived.

For each such construct, ask:

What concrete, reachable state of this system requires this to exist?

If there is no convincing answer, prefer deleting it.

Do not engineer for hypothetical situations that the architecture, environment, process, protocol, filesystem, git history or surrounding system already makes impossible.

A recurring failure mode in software is to make each component locally beautiful, defensive, reusable and complete while making the system as a whole incomprehensible.

I consider that bad engineering.

Global simplicity dominates local elegance.

A slightly less general component that makes the whole system obvious is preferable to a beautifully abstract component that increases the number of concepts required to understand the whole.

Prefer constraints over checks.

Prefer making invalid states impossible over handling them.

Prefer one authoritative path over several equivalent paths.

Prefer deriving information over storing another representation of it.

Prefer deleting a state transition over handling it correctly.

Prefer eliminating an edge case structurally over adding code for it.

Prefer a direct call over an abstraction whose only justification is conventional decoupling.

Prefer language-native machinery over hand-built frameworks.

Prefer boring code whose purpose is immediately obvious.

Do not preserve an abstraction merely because substantial work has already been invested in it.

Do not preserve code because tests exist for it.

Tests verify an implementation we happened to build. They do not prove that the implementation should exist.

Preserve required behaviour and externally meaningful contracts. Internal machinery is disposable.

Think in data transformations before objects and state machines

My preferred mental model is strongly data-driven.

Where possible, understand computation as transformation:

input → filter → map → reduce/apply → output

This is not a demand for functional-programming theatre. It is a preference for representing computations directly as transformations over data rather than creating unnecessary intermediate entities and mutable state.

Before introducing or preserving a stateful object, ask whether the same behaviour can be expressed as:

* a pure transformation;
* a projection of existing data;
* a filter;
* a mapping;
* a reduction;
* a lookup;
* a composition of small functions;
* a value derived from an authoritative source;
* a language-native iterator, collection operation or standard-library primitive.

Explicit state should exist only when reality itself is stateful.

If a value can cheaply and reliably be derived, it generally should not become independently stored state.

If two pieces of state must remain synchronized, first ask why two representations exist.

The best synchronization mechanism is often deletion of one representation.

Optimisation begins at the mathematical model

Do not think of optimisation only as profiling hot loops after implementation.

The largest optimisations often come from choosing a representation that requires less work in the first place.

Look for:

* repeated transformations that are mathematically identical;
* values that can be memoized because their inputs determine them completely;
* computations that can become lookups;
* repeated parsing or discovery that can happen once at a natural boundary;
* unnecessary materialisation of intermediate collections;
* repeated filesystem traversal;
* unnecessary serialization/deserialization;
* duplicated indexing;
* recomputation caused by poor decomposition;
* algorithms whose complexity comes from representing too much state.

Use caching where the semantics make caching obviously correct.

Prefer native mechanisms when they exist. In Python, for example, understand and use the standard library deeply before inventing infrastructure: functools, itertools, collections, pathlib, generators, comprehensions, immutable/hashable values, native sorting/grouping primitives, context managers and other built-in machinery are frequently both clearer and better optimized than bespoke equivalents.

Likewise in every language: know what the runtime and standard library already do exceptionally well.

Do not reproduce language-native behaviour behind “cleaner” wrappers unless there is a concrete domain reason.

An optimisation that removes a concept is generally preferable to an optimisation that adds machinery.

Compositionality lives in useful functions

Good compositionality is not having many layers.

It is having small, semantically meaningful transformations that combine naturally.

Utilities should represent genuine operations in the problem domain or genuinely reusable transformations — not fragments extracted solely to make files look tidy.

A good utility:

* has an obvious meaning;
* has a narrow data contract;
* introduces little or no hidden state;
* composes naturally with other operations;
* can often be reused across parts of the system without those parts becoming coupled;
* makes duplicate state or duplicate representations unnecessary.

When several areas of the codebase manipulate the same information differently, look for the underlying transformation they actually share.

Extract that truth, not a generic framework around the call sites.

Good compositionality should reduce total system state.

It should allow the same authoritative data to flow through several transformations rather than requiring each subsystem to maintain its own interpretation of reality.

The goal is not maximum reuse.

The goal is minimum independent knowledge.

Treat the filesystem as a powerful state primitive

Do not assume that state must live behind a database, repository class, service layer or bespoke persistence abstraction.

For this harness, the filesystem is often an exceptionally useful direct representation of state.

A file can simultaneously be:

* human-readable configuration;
* machine-readable configuration;
* durable state;
* an input to deployment;
* an audit surface;
* a diffable representation;
* a synchronization boundary;
* a trigger;
* something naturally manipulated by ordinary tools.

That is powerful.

Do not wrap filesystem operations merely because inline filesystem handling feels “low level”.

Direct filesystem interaction can be the clearest possible expression of a state transition.

Use pathlib or equivalent native primitives where appropriate. Read the file. Transform the data. Write the file. Let the surrounding system observe the resulting state.

If that is the actual architecture, do not hide it behind three layers of abstractions.

At the same time, respect atomicity and failure semantics where they genuinely matter. A simple state model does not mean careless I/O.

Git is part of the state machine

Git is not merely a developer tool sitting outside the application.

In this harness it may be part of the runtime control architecture.

Treat branches, commits, diffs, merges and repository state as potentially meaningful primitives.

For example, a valid workflow may be:

task instruction
→ change config.yaml
→ commit that change to the steward branch
→ reconcile steward into main
→ main changes
→ deployment machinery observes main
→ deployment occurs according to the configuration already defined in the repository.

That is already a coherent state machine.

Do not replace it with an internal orchestration framework merely because “production systems should not use git as a database”.

Understand the actual guarantees.

The branch contains proposed state.

The commit is an immutable description of a transition.

The diff explains exactly what changed.

Reconciliation represents acceptance.

Main represents accepted desired state.

The deployment mechanism realizes that state.

This gives us provenance, rollback, human inspection, auditability, temporal ordering and synchronization using machinery that already exists.

That is extremely powerful.

Use those properties deliberately.

Do not duplicate them in an application-level state system unless a real requirement exists that git cannot satisfy.

Configuration should be executable desired state where appropriate

If changing the model used for a particular harness rhythm is conceptually a configuration change, then represent it as a configuration change.

For example:

config.yaml defines which base model is used for a particular rhythm.

A task that decides to alter that model should update the authoritative configuration, commit the change on the steward branch, and allow the normal reconciliation/deployment path to realize it.

Do not create:

* a runtime override database;
* a model-selection registry;
* an internal synchronization service;
* a parallel control plane;

unless the actual requirements demand them.

Prefer changing the authoritative representation and letting the architecture propagate that state.

The same principle applies elsewhere:

change the source of truth, not every consumer of the truth.

Filesystem + git + processes can replace enormous amounts of orchestration code

Look carefully for areas where conventional application architecture has been used to reproduce semantics that already exist in:

* files;
* directories;
* process boundaries;
* exit codes;
* stdin/stdout;
* environment variables;
* permissions;
* git commits;
* branches;
* merges;
* diffs;
* hooks;
* deployment triggers;
* operating-system primitives.

These are not crude substitutes for “real architecture”.

They are often the architecture.

A mature operating system, filesystem, language runtime and version-control system already embody decades of work on composition, isolation, persistence, concurrency and observability.

Use them before building weaker versions inside the codebase.

This does not mean turning everything into shell scripts.

It means recognizing existing primitives and composing them directly when they already match the problem.

Take inspiration from strong agent harnesses without copying their complexity

Modern coding-agent systems demonstrate something important: the filesystem itself is an extraordinarily effective interface between model reasoning and software state.

The model can inspect concrete artifacts, make bounded changes, run commands, observe consequences, use diffs as feedback and progressively alter a real environment.

Preserve that directness.

The important lesson is not to imitate another coding agent’s internal architecture.

The useful lesson is that tools should expose reality rather than inventing an elaborate mirror of it.

The harness should allow the model to operate on real files, real git state, real processes and real outputs while the harness itself controls authority.

Model proposes.

Harness disposes.

Do not introduce a shadow world of internal objects representing filesystem state, repository state, process state or deployment state unless doing so solves a demonstrated problem.

Work from the whole toward the parts

Before refactoring, reconstruct the repository’s actual problem taxonomy.

Determine:

1. What is this system fundamentally responsible for?
2. What are its true external boundaries?
3. What entities or concepts actually exist in the problem domain?
4. What data is authoritative?
5. What data can be derived?
6. Which pieces of state exist only because implementation layers duplicated information?
7. Which invariants are guaranteed by the harness, environment, filesystem, git, process model, protocol or another layer?
8. Which states can actually occur?
9. Which states does the implementation merely imagine can occur?
10. Where is the same responsibility represented more than once?
11. Which abstractions exist because of historical implementation decisions rather than the underlying problem?
12. Which application-level mechanisms duplicate capabilities already supplied by the language, filesystem, git or operating system?
13. Which computations can be expressed as simple compositions of transformations?
14. Which repeated computations can safely disappear through memoization, indexing or better data representation?
15. Which pieces of code would disappear if the system were designed today with complete knowledge of its present requirements?

Build your understanding from that before deciding where to edit.

Do not perform a mechanical cleanup pass.

Do not go file by file asking how each file can be improved.

Ask instead whether each file, module, abstraction, representation and state deserves to exist at all.

Refactoring direction

Bias strongly toward subtraction.

A successful refactor should generally result in fewer:

* lines;
* files;
* concepts;
* states;
* mutable values;
* abstractions;
* execution paths;
* representations of the same fact;
* configuration knobs;
* synchronization mechanisms;
* custom infrastructure primitives;
* places where a future engineer must look to understand behaviour.

Do not chase LOC reduction mechanically. Dense, clever code is worse than clear code.

The objective is semantic compression: fewer ideas are required to explain the same correct system.

If ten functions can become three because seven represented distinctions that do not exist in reality, excellent.

If ten clear functions genuinely represent ten distinct operations, leave them alone.

If a 300-line service can become a 30-line data transformation because persistence and orchestration were imaginary complexity, excellent.

If removing state makes three subsystems derive the same answer from one source of truth, excellent.

If a direct filesystem operation is clearer than a persistence layer, use the filesystem.

Do not over-engineer the refactor itself

Do not introduce infrastructure whose purpose is to make the refactoring cleaner.

Do not invent a new architecture simply because the old one is messy.

Do not respond to complexity by adding another abstraction layer over it.

Trace complexity to its source and remove the source where possible.

When you encounter duplicated or awkward code, first ask whether the duplicated concept itself should disappear before extracting a helper or abstraction.

When you encounter extensive validation, determine whether the caller or architecture already guarantees the invariant.

When you encounter defensive branches, identify an actual path by which they can execute.

When you encounter configuration, determine whether multiple valid configurations genuinely exist.

When you encounter an interface with one implementation, do not assume a second implementation is a meaningful future requirement.

When you encounter retries, recovery logic or fallbacks, understand the failure semantics before preserving them.

When you encounter stored state, determine whether it can be derived.

When you encounter a custom cache, determine whether native function caching or a simpler representation is enough.

When you encounter an internal state machine, determine whether the filesystem, git or process lifecycle already implements it.

When you encounter an orchestration layer, determine whether it is coordinating reality or merely mirroring it.

Repository-wide reasoning

Follow dependencies across module and package boundaries.

A local change is not necessarily a local problem.

If awkwardness in component B exists because component A exposes the wrong concept, fix A rather than making B more sophisticated.

If several downstream components compensate for an upstream design mistake, correct the upstream mistake and delete the compensation.

If several modules independently derive equivalent state, find the common authoritative input and compose shared transformations over it.

Seek the point in the system where the invariant naturally belongs.

The architecture should increasingly make downstream code inevitable.

Behaviour and safety

Do not recklessly delete things merely because they look unnecessary.

Prove your understanding.

Use repository evidence: callers, tests, types, runtime structure, filesystem layout, git workflow, configuration, deployment assumptions, permissions, documentation and history where useful.

Distinguish:

* reachable from hypothetical;
* required from accidental;
* authoritative data from derived data;
* externally observable behaviour from internal implementation;
* architectural invariants from defensive assumptions;
* current requirements from imagined future requirements;
* actual state from duplicated representations of state.

Run relevant tests and checks as you work.

When existing tests encode accidental implementation detail, change or remove those tests rather than preserving bad architecture solely to satisfy them.

Do not weaken genuine security or trust boundaries in the name of simplicity. Security invariants are part of the real system and therefore belong in the model.

The model does not get authority merely because direct filesystem and git operations are powerful. The harness remains the authority boundary.

How to work

Take this in coherent, comprehensible slices.

For each meaningful slice:

* understand the complete responsibility involved;
* identify its authoritative inputs;
* identify derived versus independently stored state;
* identify unnecessary concepts or states;
* identify where language-native, filesystem, git or OS primitives already solve part of the problem;
* simplify the underlying model;
* make the smallest coherent change;
* run the relevant verification;
* inspect the resulting whole again.

Do not spray hundreds of unrelated cosmetic edits across the repository.

Do not spend time polishing code that is likely to disappear.

Do not create TODO architecture.

Do not add speculative extensibility.

Do not stop at surface cleanup when the complexity has an identifiable structural cause.

You have permission to make substantial changes when the evidence supports them.

The standard

At every point, ask:

Can the system know less and still do everything it is actually required to do?

Can this value be derived instead of stored?

Can this state be eliminated rather than managed?

Can this distinction disappear?

Can this computation become a composition of obvious transformations?

Can a native language primitive do this better than our code?

Can one layer guarantee this so every other layer stops checking it?

Does the filesystem already represent this state?

Does git already represent this transition?

Are we building machinery to mirror something the environment already knows?

Is this code representing reality, or representing possibilities invented by the implementation?

If I delete this concept, what concrete required behaviour becomes impossible?

If the answer to the last question is “none”, deletion should be the default.

The end state should not merely have cleaner code.

It should contain a smaller explanation of the system.

A good refactor should make it possible to describe more of the system as:

authoritative data → small composable transformations → observable state transition

and less of it as a network of objects coordinating duplicated knowledge.

Begin by reading and understanding the repository.

Then produce a concise architectural assessment covering:

* what the system actually is;
* its authoritative sources of state;
* its real state transitions;
* where derived information has become stored information;
* where application code duplicates language/filesystem/git/OS primitives;
* where compositional data transformations could replace explicit machinery;
* where complexity is concentrated;
* which complexity appears accidental.

After that, proceed with the refactor.

Do not wait for approval between ordinary steps unless you uncover genuine ambiguity about externally required behaviour that cannot be resolved from the repository itself.

Two questions before the deletion question

“If I delete this concept, what concrete required behaviour becomes impossible?”
is the right question, and it is asked too late if the two below are skipped.

First: is it an invariant at all?

Much of what a codebase enforces is not a requirement. It is a performance
wish, a tidiness habit, or a fear, written in the grammar of a rule so that
nobody argues with it. “A withdrawn task must terminate its running gate” reads
like a correctness requirement. It is not one. The requirement is that a
withdrawn task’s work must not land. Whether the gate finishes first is a
question about wasted CPU, and wasted CPU is not an invariant.

Second: does the representation already hold it?

An invariant can be held by design or by code, and only one of those has a
maintenance cost. If a task is a file in `tasks/`, then withdrawing it is moving
the file — to `archived/`, or out of existence. After that, nothing observes it,
nothing chooses it, and nothing derives a publication from it. The invariant
holds because the thing that would have violated it is gone. No check runs. No
check *can* run, because there is nothing left to check.

The same invariant, with the task stored as a row, needs a status column, a
predicate consulted at every stage that could act on it, and a recovery pass for
rows whose process died. All of that is the cost of having chosen the
representation that does not hold the invariant.

So: an invariant that survives both questions gets code. One that fails the
first was never real. One that fails the second is telling you the fact is in
the wrong place, and the correct response is to move the fact, not to write the
check.

Delete first, then fill

Do not refactor by editing small pieces. It does not work, and the failure is
structural rather than a matter of discipline: a small edit has no constraint on
its own size, so each one can be locally justified while the total grows. That
is precisely how a session can leave every commit green, every decision
defensible, and the source 702 lines larger than it started.

Invert it. Delete the whole concept — the table, the module, the callers, the
tests of the mechanism — and leave a hole with the primitive that will replace it
written on the rim. Do that everywhere before filling anything. Then the holes
are visible, the line count has already fallen, and no fill can quietly cost
more than what it replaced, because what it replaced is a number you can read.

A hole is filled by a primitive or it is not filled. If the fill turns out to be
another module, name the primitive it embodies or revert it.

And do not take a line budget. A budget is an instruction, and instructions get
satisfied by compressing syntax, which is not the same thing as removing a
concept and is explicitly not what this asks for.

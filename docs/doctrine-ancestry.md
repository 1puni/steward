# A Nod to the Ancestors

The doctrine and the refactor mandate were written without citations. This
page records where the ideas come from, so the lineage is neither mistaken
for novelty nor defended as arbitrary taste.

Two things in the stack are ours alone and are recorded as such below:
*stormbeslå* and the pseudocode ratio.

## The doctrine's ancestors

- **Fred Brooks** — the essential/accidental complexity split (*No Silver
  Bullet*) and conceptual integrity as the first concern of design (*The
  Mythical Man-Month*). "Fewer concepts" is his program, and "show me your
  tables and I won't need your flowcharts" is §10's grandfather.
- **C. A. R. Hoare** — robustness by reduction: make it so simple there are
  obviously no deficiencies, not so complicated there are no obvious ones
  (Turing lecture, 1980). The doctrine's core stance.
- **The Unix tradition** (Kernighan, Pike, McIlroy, Plan 9), codified by
  Eric Raymond — compose the primitives the OS already solved: files,
  processes, pipes, exit codes, signals. The mandate's
  filesystem-as-state and git-as-state-machine is this applied literally.
- **Richard Gabriel** — "worse is better": simplicity of design over
  completeness of function. The mandate's stated departure from
  conventional best practice.
- **Niklaus Wirth** — *Algorithms + Data Structures = Programs*: choose the
  representation and the code follows. §10 in one title.
- **Linus Torvalds** (as quoted by Raymond) — "bad programmers worry about
  the code; good programmers worry about data structures and their
  relationships."
- **Rich Hickey** — simplicity as uncomplecting, values over places,
  derive-don't-store (*Simple Made Easy*, *The Value of Values*), and
  "guarding is worse than fixing" (*Maybe Not*), which is
  invariants-by-representation exactly.
- **Yaron Minsky and Alexis King** — "make illegal states unrepresentable"
  (*Effective ML*) and "parse, don't validate": types over comments,
  schemas over prose.
- **Casey Muratori** — "semantic compression," used by name in the mandate:
  fewer ideas to explain the same correct system, not fewer characters.
- **David Parnas** — decomposition by semantic responsibility; information
  hiding; generality earned only from a second real case.
- **Mark Miller and the capability tradition** — least authority. "The
  model proposes. The harness disposes." descends from it.

## The calibration canon

These systems provide reference points for the
[structural review](demolition.md):

- **Git's early core** — blobs, trees, commits, refs: the idea-to-code
  ratio benchmark, and the sharpest judge, because this harness sits on
  Git and rebuilt its object model in SQLite anyway.
- **SQLite** — complexity that is almost never accidental; narrow
  interface, controlled state, exhaustive testing.
- **musl libc** — high signal-to-noise, small functions, brutally explicit
  invariants, no framework-building.
- **djb (qmail, djbdns, daemontools)** — tiny cooperating programs,
  filesystem-as-state, supervision as composition; most of the code unable
  to hurt you.
- **suckless (dwm, dmenu, st)** — the configuration is the source;
  comprehensible in a sitting.
- **OpenBSD base** — removal of historical garbage rather than abstraction
  around it.
- **Redis (older)** — one human retains the system in their head;
  abstractions correspond to actual things, not aspirations.
- **Lua, TinyCC** — how much a small footprint can carry; attack the
  actual problem instead of reconstructing the industry-standard
  architecture.
- **Fossil** — DVCS, wiki, tickets, web UI and sync in one executable.
  This harness is Fossil-shaped in ambition.
- **WireGuard** — security from a reduced state space, not a bigger stack.

## Ours: stormbeslå

An axiom for large-scale refactoring of a live system, when starting from
scratch is not available:

> Delete whole concepts first — the module, its callers, the tests of its
> mechanism — leaving holes with the replacement primitive written on the
> rim. Only when every hole exists, fill. A fill may not cost more than
> what it replaced, because the hole has a number on it.

The property it fixes is structural, not disciplinary: a small edit has no
constraint on its own size, so each is locally defensible while the total
grows; a green suite of defensible commits can still leave the source
larger than it started (2026-09-09: +702 lines). A hole cannot lie about
its size.

Corollaries, as practiced in `demolition.md`:

- A hole is filled by a *primitive*, or it is not filled.
- Never take a line budget; budgets are satisfied by compressing syntax.
- Before deleting anything, the two questions: is it an invariant at all,
  and does the representation already hold it for free?
- A hole's mirror must fail for the reason it names, or it guarantees
  nothing.

## Ours: the pseudocode ratio

The metric is not LOC but **implementation volume ÷ the conceptual
machine**: write the harness as pseudocode without referring to any
existing class or module — if the complete machine fits in 100–300 lines
against 15,000 implemented, the ratio tells you something (operator,
2026-09-10). Performed blind, the machine is 96 lines, ~75 of them
executable statements; the operating target is ratio < 150. Fifteen
thousand lines implementing fifteen thousand lines' worth of irreducible
behaviour is fine; fifteen thousand implementing
observe → derive → execute → verify → commit is entropy.

Nearest public ancestors: Brooks' essential/accidental split for the
denominator, and the formal-methods spec-to-code ratio for the arithmetic.
The pseudocode test itself is ours.

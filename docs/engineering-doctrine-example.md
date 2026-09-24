Concrete example: a task may simply be a file

Do not assume that a domain concept requires a corresponding application object, database record, repository abstraction and persistence lifecycle.

For example, consider a task.

A conventional implementation might represent a task across:

* a database row;
* an ORM model;
* a task identifier;
* status fields;
* metadata columns;
* serialization;
* repository methods;
* query functions;
* update methods;
* synchronization with whatever human-readable representation is shown elsewhere.

But a task may already have a much smaller natural representation:

/tasks/<clear-task-identifier>.md

The filename is the identifier.

Its presence in /tasks establishes that it is a task.

Its Markdown body is the human-readable instruction.

Its frontmatter contains whatever small amount of structured metadata is genuinely required.

The filesystem gives us enumeration.

The path gives us identity and location.

The file contents give us state.

Ordinary file operations give us creation, mutation and deletion.

Git gives us history, attribution, diffs, rollback and synchronization.

The same artifact is directly readable by humans and models.

In such a system, introducing a database representation of the same task may not improve the architecture. It may merely create a second truth that now has to be maintained.

This is the kind of substitution you should actively search for during the refactor.

Do not ask only:

How can this database-backed task system be simplified?

Also ask:

What is the minimum natural representation of a task in this system?

The correct answer may genuinely be a database.

But that decision must come from requirements such as query patterns, concurrency, transactional semantics, scale or external integration — not from the assumption that persistent application state belongs in a database.

Prefer the representation that contains the least independent knowledge while still satisfying the real requirements.

# Reading a world at one commit

`steward_harness.world_read` provides a shared Git reader for consumer adapters.
GuruGee's Oracle and the independent steward dashboard retain their presentation,
transport, access, source configuration and caches. Installing this module does
not migrate either consumer or add a document feature to the task Mini App.

## Pins and observation

`pin_ref(repo, ref)` resolves a fetched ref once. `pin_sha(repo, sha)` requires a
full SHA naming an existing commit. Clones, linked worktrees and bare repositories
are supported. Every read takes the resulting immutable `Pin` and uses that exact
commit, even if the ref or checkout changes later. A missing ref returns `None`;
an unavailable repository or other Git failure raises an error.

`Pin.provenance` is data supplied by the caller: `fetched-ref`, `exact`, or
`controller-accepted`. `pin_ref` also retains the ref name. An adapter using
`pin_sha(..., provenance="controller-accepted")` must have independent evidence
of acceptance. The label grants no authority, and a fetched/published ref is not
controller acceptance.

`Freshness(observed_at, attempted_at, error).after(attempted_at, error)` advances
`observed_at` only on success (`error is None`). An adapter keeps its previous
successful pin and observation when refresh fails; a never-observed source stays
unobserved. `fetch(repo, remote, freshness)` is an optional explicit convenience
for a caller-authorized remote. It updates refs, records failed attempts and
returns categorical errors without transport credentials. It does not schedule,
cache or select sources. Controller transport stays outside this leaf.

Content change time is separate from observation time. In particular,
`ControllerGitTransport.observed_tip()` returns a committer date alongside its
SHA; adapters must not call that date a successful observation timestamp.

## Reading and discovery

| Call | Result |
| --- | --- |
| `tree(pin, suffixes=None)` | Visible regular files and sizes; no symlinks or gitlinks |
| `read(pin, path)` | One regular blob; missing or excluded paths raise `ValueError` |
| `read_many(pin, paths)` | Regular blobs by exact object ID; absent paths omitted, excluded paths refused |
| `changes(pin, window=200)` | Newest first-parent change per current regular file: full `sha`, `at`, `subject`, `added`, `removed` |
| `history(pin, path, limit=6, max_diff=18000)` | First-parent changes with author, diff, full counts and explicit `truncated` flag |
| `select(paths, include)` | Component globs: `*` stays in one directory, `**` may cross directories |
| `packages(pin, include=None, skip=())` | Nested README folders, heading titles and newest content dates, within the configured selection |
| `title`, `resolver`, `references`, `link_edges` | Plain document metadata and links confined to one source |
| `owner_url`, `blob_url` | Credential-free GitHub links, with encoded paths at the exact commit |

Package dates are compared as instants across time zones while retaining their
original timestamp strings. Merges are dated when their first-parent branch
receives the change. Binary line
counts, and counts outside the requested change window, are `None`, not zero.
Rename detection is disabled. Tabs and newlines in filenames remain literal.
Results are caller-owned; any consumer cache must include repository, SHA and
selection parameters. A skipped package's descendants cannot leak into its parent.
The root README is not a package. Include patterns must include package READMEs
for those packages to be discovered.

## Bounds and failure behavior

Reads disable hooks, replacement objects, fsmonitor, external diff/textconv and
ambient Git configuration/environment routing. All Git commands have a deadline
(30 seconds by default, 60 for fetch) and a 16,000,000-byte combined stdout/stderr
limit enforced during capture. A blob is limited to 1,500,000 bytes; a batch must
fit the command budget including framing. A limit failure raises `ValueError`;
Git failures and timeouts remain errors. History accepts at most 200 entries and
clips displayed diffs only after a bounded successful read. A larger command is
refused, never returned as a silently incomplete successful index.

Metadata/path framing requires UTF-8; blob text replaces invalid UTF-8. Hidden
paths, trees, symlinks and gitlinks are excluded from document reads. Only object
IDs from the pinned tree enter batch requests. Git's metadata directory is an
explicit caller-selected source, not a filesystem authorization boundary.
Consumers still own which repositories and documents a user may access.

## Consumer migration

Oracle adapters map full `changes[...]["sha"]` to their display commit field and
add their source-name prefix to paths. Rendering, ranking, background refresh,
local dirty/untracked documents and source state remain in Oracle. Dashboard
adapters map the same data into their document/history projection and preserve
response-level content/diff budgets and unavailable-source handling. Neither
consumer should resolve a moving ref again midway through a response.

Before upgrading a consumer's harness dependency, verify its projection against
one paper SHA: content, title, revision, content date and first-parent history
must match. Adding a README below a configured root must discover the package
without UI path lists. Confirm stale-source labeling after failed refresh,
regular-blob/root restrictions and unknown binary counts. Consumer migrations
and runtime activation require their own accepted changes and normal gates.

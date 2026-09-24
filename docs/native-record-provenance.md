---
kind: design-note
scope: repository:steward-harness
updated_by:
  agent: codex
updated_at: 2026-09-06
sources:
  - path: src/steward_harness/runtime/native_workspace.py
  - path: experiments/native_sessions/direct_storage_probe.py
  - path: docs/native-provider-runtime.md
---

# Native records, memories, and source provenance

Operator sketch, 2026-09-06: keep original sessions in `artefacts/`, analysed
content in `memories/`, and make the owning scope, sources, and most recent
editor visible. Prefer native writes and formats over a harness transcript
exporter. This document records that direction and the implementation evidence;
it does not assert that the production runtime has adopted every mapping.

## Reading from source to interpretation

| Location | Meaning | How to assess it |
| --- | --- | --- |
| `artefacts/<provider>/…` | Original provider session files in their native format | What that session was told, observed, attempted, or reported. Tool output can be incomplete or wrong; a model's success statement is not publication truth. |
| `memories/<provider>/…` | Native memory, including extracted facts and interpretations | Follow its source references. Summarisation and inference introduce distance from the original observation. |
| `docs/…` | Consolidated knowledge at the owning scope | Follow the documentation map, charter, and cited evidence. Moving a claim here does not increase the authority of its source. |

Scope and distance from source are separate. A repository fact remains owned by
that repository even when an organisation steward reads it. A first-hand claim
can be wrong; a later synthesis can be better supported. Keep direct observations,
inferences, and unresolved contradictions distinguishable. Reference the actual
sources rather than assigning one confidence or distance number to a document
with mixed evidence.

Native session files remain native. Do not insert YAML frontmatter into JSONL
or rewrite messages to fit a harness schema. Their session/message identities
and the Git revision that retains them locate the original evidence. Native
compaction may change the working record; Git preserves earlier accepted versions.
An active worktree's files are current candidate evidence. A committed revision
identifies the version that crossed the world's acceptance boundary.

## Attribution on derived documents

Authored Markdown can use small frontmatter where the native format permits it.
This file supplies a concrete example: scope, cited sources and latest editor.
Paths without a revision refer to the containing Git revision; cross-revision
citations should name the source revision explicitly. The harness does not enforce
or automatically restamp these fields. A more detailed memory entry can use:

```yaml
---
kind: memory
scope: repository:example
sources:
  - path: artefacts/claude/projects/<native-project-key>/<session-id>.jsonl
    revision: <full-git-sha-containing-the-source>
    message_id: <native-message-id>
updated_by:
  steward: example-repository
  provider: claude
  session_id: <session-id>
updated_at: <UTC-timestamp>
---
```

Paths are relative to the owning Git world's root. An interpretation based on
another memory should cite that memory and its revision, retaining the reference
chain to original records. Add a native message ID when available to locate a
specific observation in a long session. Explain inference and uncertainty in
the prose; metadata does not replace that explanation.

`updated_by` identifies the actor claiming the latest semantic edit. Git records
the actual file changes, author/committer attribution, and accepted revision;
controller execution provenance supplies the provider identity where available.
These facts can differ: the harness may make the checkpoint commit for an agent.
Frontmatter is readable attribution, not authenticated proof. No metadata parser,
automatic restamping pass, or second provenance database is introduced here.

## Native path controls and isolated writers

Claude supports `CLAUDE_CONFIG_DIR` for its private configuration and session
root, and `autoMemoryDirectory` for memory independently. Its JSONL sessions live
under `projects/<native-project-key>/`. Newer releases also document
`CLAUDE_CODE_PROJECT_DIR_NAME`, but that control requires 2.1.234; the inspected
2.1.220 binary does not supply it. See [Claude session locations](https://code.claude.com/docs/en/sessions#where-transcripts-are-stored)
and [memory configuration](https://code.claude.com/docs/en/memory#storage-location).

Codex supports `CODEX_HOME` for configuration and state, including native
`sessions/` rollouts and `memories/`. `sqlite_home` separately locates runtime
SQLite state. The inspected configuration reference does not expose separate
session-directory and memory-directory overrides. See [Codex configuration](https://learn.chatgpt.com/docs/config-file/config-advanced#config-and-state-locations)
and [configuration keys](https://learn.chatgpt.com/docs/config-file/config-reference).

The direct-storage experiment gives each worktree a separate private runtime
home. Its filesystem mappings are:

```text
private Codex home/sessions  → worktree/artefacts/codex/sessions
private Codex home/memories  → worktree/memories/codex
private Claude home/projects → worktree/artefacts/claude/projects
Claude autoMemoryDirectory  = worktree/memories/claude
```

GLM uses the Claude transport with `glm` as its provider directory. The symlinks
live in private runtime storage; the tracked worktree contains regular native
files. Credentials, native databases, caches, and sockets remain outside Git.
An export is unnecessary for these directly mapped files.

Never retarget one shared home's link between concurrent worktrees. Native
session continuity must also survive the next isolated worktree: the experiment
checkpoints the original records, clones them, creates a fresh private runtime,
and resumes from the native transcript path. Its live evidence and remaining
production work belong in the [probe record](../experiments/native_sessions/README.md).

The production runtime now uses these direct mappings; Codex thread-read
snapshots have been removed. See [runtime setup](native-provider-runtime.md) for
configuration and existing-session import. Unprepared event worktrees retain
originals and partial edits across interruption/startup; they require explicit
recovery and must not be confused with the accepted world. Codex background
memory consolidation still needs its own quiescence evidence. A successful
foreground transcript exercise alone does not prove that boundary.

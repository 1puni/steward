# The world's Git store: what is expensive, and what only looks expensive

**The short version.** The world is cheap. Session transcripts are cheap. The only
thing that has ever made a world store expensive is a repack run with
`--max-pack-size`, which defeats the delta compression that keeps it small. Do not use
that flag here.

## The flag is the cause, not the cure

Git's delta compression works *within* a pack. Cap the pack size and Git cannot delta
across the boundary, so every chunk must carry its own base objects. On a live world,
a capped repack turned a 650 MB repository into about 10.7 GB of exactly-512 MiB packs,
twice, filling the disk it was invoked to relieve. Exactly equal file sizes are the
tell: a crash produces varied sizes; a hard ceiling produces identical ones.

## What `git gc` does, and does not do

It **compacts**: packs loose objects, recomputes deltas, drops objects that are
genuinely unreachable past a grace period, and removes stale temporary files.

It does **not** clean. Reachable history is reachable; `gc` cannot remove it, so it is
not an answer to "something is committing junk". For that, measure what is in the
history (recipe below). Run it with **no flags**, under the world lease.

## Append-only files are nearly free

The world commits provider session transcripts — under `artefacts/codex/sessions/`
and `artefacts/claude/projects/` — so a session can be resumed after a restart. A long
session is re-committed on every checkpoint, so one file can appear in dozens of
commits. That looks alarming and is not. Measured on a live world:

| | Logical bytes | Packed on disk |
| --- | --- | --- |
| All blobs | 858.6 MB | 645.7 MB |
| 415 session blobs | 377.5 MB | **223.7 MB** |
| An append-only log, 838 versions | 3.8 MB | **0.6 MB** |
| One transcript committed 42 times — largest version | 11.45 MB | **1.18 MB** |
| …each subsequent re-commit of it | ~11 MB each | **~0.00 MB** |

Each version differs from the last by a few appended lines, and Git stores the
difference. **The deduplicated history is what a packfile already is.** There is no
restructuring to do, no chunking to write, and no reason to stop versioning session
data.

Forty-two commits touching one file is also the signature of a retry loop, so open
the log before deciding. Distinct turn IDs minutes apart, each appending a few lines,
is the harness working; identical state seconds apart is a loop. The remaining cost is
`git log` readability, not storage, and it does not justify a subsystem.

## Diagnosing a store

```sh
W=/path/to/world

# Is anything actually wrong? `size-garbage` is the field that matters.
git -C $W count-objects -vH

# Logical bytes per top-level path, across all history.
git -C $W rev-list --objects --all \
  | git -C $W cat-file --batch-check='%(objecttype) %(objectname) %(objectsize) %(rest)' \
  | awk '$1=="blob" && $4!=""' > /tmp/blobs.txt
awk '{split($4,p,"/"); s[p[1]]+=$3} END{for(k in s) printf "%9.1f MB  %s\n", s[k]/1048576, k}' \
  /tmp/blobs.txt | sort -rn

# What it costs on disk. Do this before believing the numbers above:
# logical bytes overstate append-only files by an order of magnitude.
git -C $W verify-pack -v .git/objects/pack/pack-*.pack | awk '$2=="blob"'
```

A large `size-garbage` means abandoned temporary packs, usually from a service stopped
mid-transfer. Delete a `.git/objects/pack/tmp_pack_*` file only when it is a regular
file older than 48 hours with no open descriptors, and its inode, size and mtime are
unchanged on a second check. Then run plain `git gc`, with `fsck --connectivity-only`
before and after and refs unchanged; dangling objects afterwards are normal.

## Rules

1. **Never repack a world with `--max-pack-size`.**
2. **Run `git gc` with no flags**, under the world lease.
3. **Measure packed bytes, not logical bytes**, before concluding anything is large.
   The gap is 20× for append-only files.
4. **Open the log before calling repeated commits a defect.**

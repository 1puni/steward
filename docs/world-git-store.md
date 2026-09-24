# The world's Git store: what is expensive, and what only looks expensive

Written 2026-09-12, after the world's `.git` reached 12 GB for the second time
in six days and was recovered to 652 MB by deleting files Git itself had already
classified as garbage.

**The short version.** The world is cheap. Session transcripts are cheap. The
only thing that has ever made this store expensive is a repack run with
`--max-pack-size`, which defeats the delta compression that keeps it small. Do
not use that flag here.

## What happened, twice

On 2026-09-06 the disk filled and a repack recovered ~4 GB; the record is in
[`instances/1puni/upstream-rollout-2026-09-06.md`](../instances/1puni/upstream-rollout-2026-09-06.md),
which names "redundant packs and abandoned temporary pack files" without saying
what abandoned them. On 2026-09-10, between 19:00 and 19:14, it happened again
and left **21 files of exactly 536,870,912 bytes** — precisely 512 MiB — plus
one partial, totalling **10.64 GiB**, in `.git/objects/pack/`.

Exactly-equal sizes are the tell. A crash produces varied sizes; a hard ceiling
produces identical ones. No config on the box sets a pack size limit (`--system`,
`--global`, `/etc/gitconfig` and the agent's own `.gitconfig` are all clean), so
it was typed on a command line, almost certainly to relieve memory pressure on
what was believed to be a 4 GB box. The box has 15.6 GB.

**Why the flag is the cause and not the cure.** Git's delta compression works
*within* a pack. Cap the pack size and Git cannot delta across the boundary, so
every chunk must carry its own base objects. A store whose deltas collapse
858 MB of blobs into 646 MB explodes when those deltas are forbidden. The flag
produced ~10.7 GB of output from a 650 MB repository — it caused the disk
pressure it was invoked to avoid.

## What `git gc` does, and does not do

It **compacts**: packs loose objects, recomputes deltas, drops objects that are
genuinely unreachable past a grace period, and removes stale temporary files.

It does **not** clean. Reachable history is reachable; `gc` cannot remove it. So
`gc` would have swept those temp packs, and it is worth running periodically
under the world lease — but it is not an answer to "something is committing
junk". For that, measure what is in the history (recipe below) rather than
reaching for `gc`.

Run it with **no flags**. Every incident here came from a flag.

## Append-only files are nearly free, and this is measured

The world commits provider session transcripts —
`artefacts/codex/sessions/**/rollout-*.jsonl`, mapped there by
`runtime/providers/codex_app_server.py` so a session can be resumed after a
restart, and the equivalent for Claude under `artefacts/claude/projects`. A long
session is re-committed on every checkpoint, so one file can appear in dozens of
commits. That looks alarming and is not.

Measured on 2026-09-12:

| | Logical bytes | Packed on disk |
| --- | --- | --- |
| All blobs | 858.6 MB | 645.7 MB |
| 415 session blobs | 377.5 MB | **223.7 MB** |
| `episodes.md`, **838 versions** | 3.8 MB | **0.6 MB** |
| One transcript committed 42 times — largest version | 11.45 MB | **1.18 MB** |
| …each subsequent re-commit of it | ~11 MB each | **~0.00 MB** |

`episodes.md` is the clearest case: 838 versions of a file cost 0.6 MB, because
each version differs from the last by two appended lines and Git stores the
difference. **The deduplicated history is what a packfile already is.** There is
no restructuring to do, no chunking to write, and no reason to stop versioning
session data. An organisation-wide session archive costs about 224 MB packed for
296 sessions, and it gets *relatively* cheaper the longer a session runs.

## The re-commits are real work, not a defect

Checked before concluding: the transcript committed 42 times has a **distinct
turn id in every commit**, two to three minutes apart, and every one of those
commits has the same shape —

```
<the session transcript>  | 12 ++++++++++++
episodes.md               |  2 ++
```

One turn, twelve lines of provider events appended to the session it resumed,
two lines of episode recorded. That is the harness working. "Forty-two commits
touching one file" is equally the signature of a retry loop, which is why it is
worth opening the log rather than assuming either way — but here it was not one.

The remaining cost is `git log` readability, not storage. Adding machinery to
tidy that would be adding a subsystem to fix a cosmetic problem.

## Diagnosing this store

```sh
W=/var/lib/gurugee-agent/world

# Is anything actually wrong? `garbage` is the field that matters.
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

`size-garbage` in the first command is the number that found both incidents.
Anything non-zero and large means abandoned temporary packs: check nothing is
repacking, then delete `.git/objects/pack/tmp_pack_*`. Git does not reference
them — that is what "garbage" means — and `fsck --connectivity-only` should
return clean afterwards, reporting only dangling objects, which are normal.

## Rules

1. **Never repack this world with `--max-pack-size`.** It is the whole of both
   incidents.
2. **Run `git gc` with no flags**, under the world lease, and let it prune stale
   temporary files on its own schedule.
3. **Measure packed bytes, not logical bytes**, before concluding anything is
   large. The gap is 20× for append-only files.
4. **Open the log before calling repeated commits a defect.** Distinct turn ids
   minutes apart are work; identical state seconds apart is a loop.

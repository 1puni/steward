"""Read a Git world at one pinned commit: the common core of the desks that show worlds.

This leaf provides pinned Git reads for consumer adapters. Each consumer keeps
its own presentation, transport and access:

- a **pin** names one exact commit, so a response never mixes reads from a
  moving ref;
- **freshness** keeps a successful observation, the last attempt and its error
  apart, so an attempted fetch is never mistaken for fresh evidence;
- **tree, read, changes and history** come only from the pinned commit; dates
  are when content changed on the branch, never a checkout's mtimes;
- **discovery** is configuration-driven (include patterns) plus convention:
  any folder with a README is a package, newest change first;
- **links** resolve the way authors write them, inside one world only.

No task state, admission or orchestration lives here. Reads do not mutate Git;
only the explicit fetch convenience updates refs. Standard library, Git CLI and
the harness's pure Git argument and object-ID helpers only.
"""

from __future__ import annotations

import fnmatch
import os
import posixpath
import re
import selectors
import signal
import tempfile
import time
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from urllib.parse import quote, unquote, urlsplit

from steward_harness.git import hardened_git_argv, validate_object_id

MAX_DIFF = 18_000
CHANGE_WINDOW = 200
MAX_OUTPUT = 16_000_000
MAX_BLOB = 1_500_000
_MD_LINK = re.compile(r"\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)")
_CODE_SPAN = re.compile(r"`([^`\s]+)`")
_PATH_LIKE = re.compile(r"^[\w.@+-]+(?:/[\w.@+-]+)*\.(?:md|txt|json|toml|ya?ml|html|csv)(?:#[\w-]*)?$")


def git_dir(repository: str | Path) -> Path:
    """Resolve a clone, linked worktree, or explicitly supplied bare Git directory."""
    path = Path(repository).resolve()
    marker = path / ".git"
    if marker.is_file():
        with marker.open() as stream:
            line = stream.read(4096).strip()
        if not line.startswith("gitdir: ") or "\n" in line:
            raise ValueError("Invalid worktree Git directory")
        return (path / line[8:]).resolve()
    return marker if marker.is_dir() else path


def _git_bytes(directory, *args, timeout=30, stdin=None, max_output=MAX_OUTPUT):
    """Bound both output streams while Git runs; never buffer an unlimited response."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_NO_LAZY_FETCH="1", GIT_TERMINAL_PROMPT="0",
               GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1")
    argv = hardened_git_argv("--git-dir", str(git_dir(directory)),
                             "-c", "diff.external=", *args)
    if stdin is not None and len(stdin) > MAX_OUTPUT:
        raise ValueError("Git input exceeds byte limit")
    with tempfile.TemporaryFile() as source, selectors.DefaultSelector() as selector:
        source.write(stdin or b"")
        source.seek(0)
        with subprocess.Popen(argv, stdin=source, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              env=env, start_new_session=True) as process:
            output, errors = bytearray(), bytearray()
            selector.register(process.stdout, selectors.EVENT_READ, output)
            selector.register(process.stderr, selectors.EVENT_READ, errors)
            deadline = time.monotonic() + timeout
            try:
                while selector.get_map():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise subprocess.TimeoutExpired(argv, timeout)
                    for key, _ in selector.select(remaining):
                        chunk = os.read(key.fd, 65536)
                        if not chunk:
                            selector.unregister(key.fileobj)
                        elif len(output) + len(errors) + len(chunk) > max_output:
                            raise ValueError("Git output exceeds byte limit")
                        else:
                            key.data.extend(chunk)
                process.wait(timeout=max(0.001, deadline - time.monotonic()))
            except BaseException:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
                raise
            if process.returncode:
                raise subprocess.CalledProcessError(process.returncode, argv, bytes(output), bytes(errors))
    return bytes(output)


def git(directory: str | Path, *args: str, timeout: int = 30, stdin: bytes | None = None) -> str:
    """Hardened, bounded non-interactive Git; metadata must be valid UTF-8."""
    return _git_bytes(directory, *args, timeout=timeout, stdin=stdin).decode("utf-8")


# ── Pins and freshness ──────────────────────────────────────────────────────

@dataclass(frozen=True)
class Pin:
    """One world at one exact commit. Every read below takes a pin, never a ref."""

    repository: Path
    sha: str
    provenance: str = "exact"
    ref: str | None = None

    def __post_init__(self):
        object.__setattr__(self, "repository", Path(self.repository).resolve())
        validate_object_id(self.sha)
        if self.provenance not in ("exact", "fetched-ref", "controller-accepted"):
            raise ValueError("Unknown pin provenance")
        if git(self.repository, "cat-file", "-t", self.sha).strip() != "commit":
            raise ValueError("Pin must name a commit object")


def pin_ref(repository: str | Path, ref: str) -> Pin | None:
    """Pin whatever commit ref names now (a Mac clone's fetched origin/main), or None if it names none."""
    try:
        sha = git(repository, "rev-parse", "--verify", "-q", "--end-of-options", f"{ref}^{{commit}}").strip()
    except subprocess.CalledProcessError as failure:
        if failure.returncode == 1:  # --verify -q: the ref is absent.
            return None
        raise
    return Pin(Path(repository), sha, "fetched-ref", ref)


def pin_sha(repository: str | Path, sha: str, *, provenance: str = "exact") -> Pin:
    """Pin an exact commit already chosen elsewhere (a controller's observed tip)."""
    return Pin(Path(repository), sha, provenance)


@dataclass(frozen=True)
class Freshness:
    """When the world was last observed successfully, last attempted, and why that attempt failed."""

    observed_at: datetime | None = None
    attempted_at: datetime | None = None
    error: str | None = None

    def after(self, attempted_at: datetime, error: str | None) -> Freshness:
        """The record after one attempt: only a success advances observed_at."""
        return Freshness(self.observed_at if error is not None else attempted_at, attempted_at, error)


def fetch(repository: str | Path, remote: str, freshness: Freshness = Freshness(), timeout: int = 60) -> Freshness:
    """Fetch one remote and return the freshness it leaves behind."""
    attempted = datetime.now(UTC)
    try:
        git(repository, "fetch", "-q", "--", remote, timeout=timeout)
        return freshness.after(attempted, None)
    except subprocess.CalledProcessError as failure:
        return freshness.after(attempted, f"fetch failed (exit {failure.returncode})")
    except (OSError, ValueError, subprocess.SubprocessError) as failure:
        # Raw command/transport errors can contain credentials or private paths.
        return freshness.after(attempted, f"fetch failed ({type(failure).__name__})")


# ── Reading one commit ──────────────────────────────────────────────────────

def _visible(path: str) -> bool:
    return bool(path) and all(part and not part.startswith(".") for part in path.split("/"))


def _tree(pin: Pin) -> dict[str, tuple[str, int]]:
    found = {}
    for entry in git(pin.repository, "ls-tree", "-r", "-l", "-z", "--full-tree", pin.sha).split("\0"):
        meta, _, path = entry.partition("\t")
        parts = meta.split()
        if (len(parts) == 4 and parts[0] in ("100644", "100755")
                and parts[1] == "blob" and _visible(path)):
            found[path] = (parts[2], int(parts[3]))
    return found


def tree(pin: Pin, suffixes: tuple[str, ...] | None = None) -> dict[str, int]:
    """Regular visible files at the exact pin; symlinks and gitlinks are excluded."""
    return {p: size for p, (_, size) in _tree(pin).items()
            if suffixes is None or PurePosixPath(p).suffix.lower() in suffixes}


def read(pin: Pin, path: str) -> str:
    """Read one visible regular blob; missing or excluded paths raise ValueError."""
    texts = read_many(pin, [path])
    if path not in texts:
        raise ValueError("No regular document at this pin")
    return texts[path]


def read_many(pin: Pin, paths: list[str]) -> dict[str, str]:
    """Read regular blobs by object ID; absent files are omitted, excluded paths refused."""
    if not paths:
        return {}
    entries = _tree(pin)
    wanted = []
    for path in dict.fromkeys(paths):
        if not _visible(path) or "\0" in path:
            raise ValueError("Document path must be visible and root-relative")
        if path not in entries:
            # Distinguish an absent file from a tree, symlink or gitlink.
            if git(pin.repository, "ls-tree", "-z", pin.sha, "--", path):
                raise ValueError("Document is not a visible regular blob")
            continue
        oid, size = entries[path]
        if size > MAX_BLOB:
            raise ValueError("Document exceeds byte limit")
        wanted.append((path, oid, size))
    if sum(size + 100 for _, _, size in wanted) > MAX_OUTPUT:
        raise ValueError("Documents exceed batch byte limit")
    if not wanted:
        return {}
    out = _git_bytes(pin.repository, "cat-file", "--batch",
                     stdin="".join(oid + "\n" for _, oid, _ in wanted).encode())
    texts, at = {}, 0
    for path, oid, size in wanted:
        end = out.index(b"\n", at)
        if out[at:end] != f"{oid} blob {size}".encode():
            raise ValueError("Unexpected blob response")
        at = end + 1
        texts[path] = out[at:at + size].decode("utf-8", errors="replace")
        at += size + 1
    if at != len(out):
        raise ValueError("Unexpected batch response length")
    return texts


def _numstat(value: str) -> int | None:
    return int(value) if value.isdigit() else None  # "-" for binary files


def _log(pin: Pin, *args: str):
    """NUL-framed metadata and numstat paths, including tabs/newlines in filenames."""
    fields = iter(git(pin.repository, "log", pin.sha, "--first-parent", "--diff-merges=first-parent",
                      "--no-renames", "--no-ext-diff", "--no-textconv", "--numstat", "-z",
                      "--format=%x00%H%x00%an%x00%cI%x00%s", *args).split("\0"))
    field = next(fields)
    while field == "":
        sha = next(fields, None)
        if not sha:
            return
        author, at, subject = next(fields), next(fields), next(fields)
        stats = []
        field = next(fields, "")
        if field.startswith("\n"):
            field = field[1:]  # Git's separator, not part of the first numstat record.
        while field:
            added, removed, path = field.split("\t", 2)
            stats.append((path, _numstat(added), _numstat(removed)))
            field = next(fields, "")
        yield sha, author, at, subject, stats


def changes(pin: Pin, window: int = CHANGE_WINDOW) -> dict[str, dict]:
    """Newest first-parent change for each visible regular file; binary counts are unknown.

    Counts cover the newest window commits. Results are caller-owned; caching belongs
    to the consumer and must include the pin and window in its key.
    """
    if window < 0:
        raise ValueError("Negative change window")
    paths = tree(pin)
    found = {}
    for index, (sha, _author, at, subject, stats) in enumerate(_log(pin)):
        for path, added, removed in stats:
            if path in paths and path not in found:
                found[path] = dict(sha=sha, at=at, subject=subject,
                                   added=added if index < window else None,
                                   removed=removed if index < window else None)
    return found


def history(pin: Pin, path: str, limit: int = 6, max_diff: int = MAX_DIFF) -> list[dict]:
    """First-parent changes of a regular document; full numstat counts, bounded diff display."""
    if path not in tree(pin):
        raise ValueError("No regular document at this pin")
    if not 0 <= limit <= 200 or not 0 <= max_diff <= MAX_OUTPUT:
        raise ValueError("Invalid history bounds")
    result = []
    for sha, author, at, subject, stats in _log(pin, f"-n{limit}", "--", path):
        diff = git(pin.repository, "show", "--format=", "--first-parent", "--diff-merges=first-parent",
                   "--root", "--no-renames", "--no-ext-diff", "--no-textconv", "--unified=3", sha, "--", path)
        _, added, removed = next(item for item in stats if item[0] == path)
        result.append(dict(sha=sha, author=author, at=at, subject=subject, added=added, removed=removed,
                           diff=diff[:max_diff], truncated=len(diff) > max_diff))
    return result


# ── Discovery ───────────────────────────────────────────────────────────────

def matches_path(path: str, pattern: str) -> bool:
    """Match path components: * stays in one directory; ** may cross directories."""
    def match(parts, patterns):
        if not patterns:
            return not parts
        if patterns[0] == "**":
            return match(parts, patterns[1:]) or bool(parts and match(parts[1:], patterns))
        return bool(parts and fnmatch.fnmatchcase(parts[0], patterns[0]) and match(parts[1:], patterns[1:]))
    return match(path.split("/"), pattern.split("/"))


def select(paths, include: list[str] | None) -> list[str]:
    """The paths a source's configuration includes; no include means all of them."""
    return sorted(p for p in paths if include is None or any(matches_path(p, pattern) for pattern in include))


def title(text: str, fallback: str) -> str:
    """A document's first level-one heading, else the fallback."""
    return next((line[2:].strip() for line in text.splitlines() if line.startswith("# ")), fallback)


def _owner(path: str, folders: set[str]) -> str | None:
    """The nearest folder in folders that contains path."""
    return next((str(parent) for parent in PurePosixPath(path).parents if str(parent) in folders), None)


def _date_key(value: str | None) -> float:
    return datetime.fromisoformat(value).timestamp() if value else float("-inf")


def packages(pin: Pin, readme: str = "README.md", skip: tuple[str, ...] = (),
             include: list[str] | None = None) -> list[dict]:
    """Folders with a README, newest change first: the units a person reads, discovered, never listed.

    Each file belongs to its nearest folder with a README, so a nested package
    stands on its own. The root is not a package, nor anything under ``skip``.
    A package is dated by the newest change to any of its files.
    """
    paths = select(tree(pin), include)
    paths = [p for p in paths if not any(p == s or p.startswith(s.rstrip("/") + "/") for s in skip)]
    folders = {str(PurePosixPath(p).parent) for p in paths if PurePosixPath(p).name == readme} - {"."}
    groups: dict[str, list[str]] = {f: [] for f in folders}
    for path in paths:
        if (folder := _owner(path, folders)) is not None:
            groups[folder].append(path)
    dated = changes(pin)
    readmes = read_many(pin, [f"{f}/{readme}" for f in groups])
    found = [dict(folder=folder, title=title(readmes.get(f"{folder}/{readme}", ""), folder),
                  readme=f"{folder}/{readme}", files=len(files),
                  updated_at=max((dated.get(f, {}).get("at") or "" for f in files), key=_date_key, default="") or None,
                  documents=sorted((f for f in files if f.endswith(".md")), key=lambda f: (f != f"{folder}/{readme}", f)))
             for folder, files in sorted(groups.items())]
    return sorted(found, key=lambda p: _date_key(p["updated_at"]), reverse=True)


# ── Links ───────────────────────────────────────────────────────────────────

def resolver(paths):
    """Find the file a written path means, as its author meant it, inside one world.

    Tries the path beside the source, then from the root, then the old
    ``artifacts/`` name for ``evidence/``, then a unique file whose path ends
    with it, so ``voice-staging.md`` finds ``evidence/sleep/voice-staging.md``.
    Only paths of this world are ever returned; ambiguity returns None.
    """
    paths = {p for p in paths if _visible(p)}
    by_name: dict[str, list[str]] = {}
    for path in paths:
        by_name.setdefault(PurePosixPath(path).name, []).append(path)

    def resolve(ref: str, source: str) -> str | None:
        ref = unquote(ref.split("#", 1)[0].split("?", 1)[0]).strip()
        parsed = urlsplit(ref)
        if not ref or ref.endswith("/") or parsed.scheme or parsed.netloc or ref.startswith("/"):
            return None
        ref = ref.removeprefix("gurugee_data/")
        relative = posixpath.normpath(posixpath.join(str(PurePosixPath(source).parent), ref))
        if relative == ".." or relative.startswith("../"):
            return None
        candidates = [posixpath.normpath(posixpath.join(str(PurePosixPath(source).parent), ref)),
                      posixpath.normpath(ref.lstrip("/"))]
        candidates += ["evidence/" + c.removeprefix("artifacts/") for c in candidates if c.startswith("artifacts/")]
        for candidate in candidates:
            if candidate in paths:
                return candidate
        tail = ref.lstrip("./").removeprefix("artifacts/")
        matches = [p for p in by_name.get(PurePosixPath(ref).name, []) if p == tail or p.endswith("/" + tail)]
        return matches[0] if len(matches) == 1 else None

    return resolve


def references(text: str) -> list[str]:
    """What a Markdown document points at: its relative links and the code spans that are paths."""
    refs = [m.group(1) for m in _MD_LINK.finditer(text) if not urlsplit(m.group(1)).scheme]
    return refs + [m.group(1) for m in _CODE_SPAN.finditer(text) if _PATH_LIKE.match(m.group(1))]


def link_edges(paths, texts: dict[str, str]) -> set[tuple[str, str]]:
    """(source, target) pairs between documents of one world; links never leave it."""
    resolve = resolver(paths)
    edges = set()
    for source, text in texts.items():
        if source not in paths:
            continue
        for ref in references(text):
            target = resolve(ref, source)
            if target and target != source:
                edges.add((source, target))
    return edges


# ── Owners ──────────────────────────────────────────────────────────────────

def owner_url(remote: str) -> str | None:
    """A credential-free https://github.com/owner/repo for a GitHub remote; never a local path."""
    remote = re.sub(r"^git@github\.com:", "https://github.com/", remote or "")
    remote = re.sub(r"^ssh://git@github\.com/", "https://github.com/", remote)
    try:
        parts = urlsplit(remote)
    except ValueError:
        return None
    if parts.scheme != "https" or parts.hostname != "github.com" or parts.username or parts.password:
        return None
    path = parts.path.removesuffix("/").removesuffix(".git").strip("/")
    return "https://github.com/" + path if re.fullmatch(r"[\w.-]+/[\w.-]+", path) else None


def blob_url(owner: str | None, pin: Pin, path: str) -> str | None:
    """The document at its owner, at exactly the pinned commit."""
    safe_owner = owner_url(owner) if owner else None
    if not _visible(path):
        raise ValueError("Owner path must be root-relative")
    return f"{safe_owner}/blob/{pin.sha}/{quote(path, safe='/')}" if safe_owner else None

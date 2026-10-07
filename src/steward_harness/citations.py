"""Human-readable links to existing Git evidence. No writes, network or index."""

from __future__ import annotations

import html
import os
import re
import subprocess
import textwrap
from dataclasses import dataclass, replace
from datetime import datetime
from functools import cached_property
from pathlib import Path
from urllib.parse import urlsplit

import yaml

from steward_harness.git import hardened_git_argv
from steward_harness.turn_id import validate_turn_id


def readable_subject(text: str, fallback: str = "Recorded conversation") -> str:
    """An excerpt, not an invented summary; the full text remains the evidence."""
    for line in text.splitlines():
        line = re.sub(r"^\s*(?:#{1,6}\s+|[-*+]\s+|>\s*)", "", line).strip()
        line = " ".join(line.split()).strip("*_` ")
        if line:
            shortened = textwrap.shorten(line, width=96, placeholder="…")
            return line[:95] + "…" if shortened == "…" else shortened
    return fallback


@dataclass(frozen=True)
class Citation:
    reference: str
    commit: str
    authored_at: str
    committed_at: str
    subject: str
    title: str
    url: str

    def markdown(self, label: str | None = None) -> str:
        if label is None:
            date = datetime.fromisoformat(self.authored_at)
            label = f"{self.title} · {date:%Y-%m-%d %H:%M %z}"
        label = html.escape(" ".join(label.split()), quote=False)
        label = re.sub(r"([\\`*_{\[\]}])", r"\\\1", label)
        return f"[{label}]({self.url})"


class GitCitations:
    """Resolve within one explicitly chosen repository and history boundary."""

    def __init__(self, repo: str | Path = ".", *, web_url: str | None = None,
                 revision: str = "HEAD") -> None:
        self.repo = Path(repo)
        self.revision = revision
        remote = web_url or self._git("remote", "get-url", "origin").strip()
        if remote.startswith("git@github.com:"):
            remote = "https://github.com/" + remote.removeprefix("git@github.com:")
        if remote.startswith("ssh://git@github.com/"):
            remote = "https://github.com/" + remote.removeprefix("ssh://git@github.com/")
        remote = remote.rstrip("/").removesuffix(".git")
        parsed = urlsplit(remote)
        if (parsed.scheme != "https" or parsed.hostname != "github.com"
                or parsed.username or parsed.password or parsed.port
                or parsed.query or parsed.fragment
                or not re.fullmatch(r"/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", parsed.path)):
            raise ValueError("citations need a GitHub repository URL; pass --web-url https://github.com/owner/repo")
        self.web_url = remote

    def _git(self, *args: str) -> str:
        env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        env["GIT_NO_LAZY_FETCH"] = "1"
        result = subprocess.run(hardened_git_argv("-C", str(self.repo), *args),
                                capture_output=True, text=True, timeout=60, env=env)
        if result.returncode:
            raise ValueError(f"cannot resolve Git evidence ({args[0]}): {result.stderr.strip()}")
        return result.stdout

    def _log(self, *revisions: str, limit: int | None = None) -> list[Citation]:
        fields = ("%H", "%aI", "%cI", "%s", "%(trailers:key=Steward-Turn,valueonly)", "%B")
        output = self._git("log", "--no-notes", "--format=" + "%x00".join(fields) + "%x00",
                           *([f"--max-count={limit}"] if limit is not None else []), *revisions, "--")
        values = output.split("\0")[:-1]
        result = []
        for offset in range(0, len(values), len(fields)):
            commit, authored, committed, subject, turn, body = values[offset:offset + len(fields)]
            commit, turn = commit.strip(), turn.strip()
            legacy = re.fullmatch(r"steward: (?:checkpoint|turn) (turn_[A-Za-z0-9._-]+)", subject)
            if not turn and legacy:
                turn = legacy[1]
            title = subject
            if legacy:
                # A legacy label is a view of the retained reply, never a rewrite.
                reply = body.partition("\n\nReply:\n")[2].rpartition("\n\nSteward-Turn:")[0]
                title = readable_subject(reply, "Recorded turn")
            result.append(Citation(turn or commit, commit, authored, committed, subject,
                                   title, f"{self.web_url}/commit/{commit}"))
        return result

    @cached_property
    def _revision(self) -> str:
        return self._git("rev-parse", "--verify", "--end-of-options", self.revision + "^{commit}").strip()

    @cached_property
    def _turns(self) -> dict[str, list[Citation]]:
        turns: dict[str, list[Citation]] = {}
        for item in self._log(self._revision):
            if item.reference != item.commit:
                turns.setdefault(item.reference, []).append(item)
        return turns

    def resolve(self, reference: str) -> Citation:
        if re.fullmatch(r"[0-9a-f]{7,40}", reference):
            objects = self._git("rev-parse", "--disambiguate=" + reference).splitlines()
            if len(objects) != 1:
                raise ValueError(f"missing or ambiguous commit hash: {reference}")
            commit = objects[0]
            if self._git("cat-file", "-t", commit).strip() != "commit":
                raise ValueError(f"not a commit: {reference}")
            return replace(self._log(commit, limit=1)[0], reference=reference)
        if reference.startswith("task-"):
            validate_turn_id(reference)
            commit = self._git("rev-parse", "--verify", "--end-of-options",
                               f"refs/heads/tasks/{reference}^{{commit}}").strip()
            document = self._git("show", f"{commit}:task.md")
            if not document.startswith("---\n") or "\n---\n" not in document[4:]:
                raise ValueError(f"task {reference} has no task.md frontmatter")
            header = yaml.safe_load(document[4:].split("\n---\n", 1)[0])
            if not isinstance(header, dict) or not isinstance(header.get("title"), str) or not header["title"].strip():
                raise ValueError(f"task {reference} has no title")
            return replace(self._log(commit, limit=1)[0], reference=reference,
                           title=header["title"].strip(), url=f"{self.web_url}/blob/{commit}/task.md")
        validate_turn_id(reference)
        matches = self._turns.get(reference, [])
        if not matches:
            raise ValueError(f"no Steward-Turn trailer for {reference} in this history")
        if len(matches) != 1:
            raise ValueError(f"ambiguous turn {reference}: {len(matches)} commits; cite an exact commit")
        return matches[0]

    def history(self, limit: int = 20) -> list[Citation]:
        if limit < 1:
            raise ValueError("history count must be positive")
        return self._log(self._revision, limit=limit)


def cite(reference: str, *, repo: str | Path = ".", label: str | None = None,
         web_url: str | None = None, revision: str = "HEAD") -> str:
    return GitCitations(repo, web_url=web_url, revision=revision).resolve(reference).markdown(label)

#!/usr/bin/env python3
"""A scripted model CLI for the follow-through environment.

Speaks the harness's native Claude-CLI stream-json contract over stdio:

- the harness offers work as JSON `user` objects, one per line, each with a
  `uuid` it generated (the offered command identity);
- the first event back must be `system/init` carrying a `session_id` uuid
  and the effective `model`; on `--resume <uuid>` the same session identity
  must come back;
- each offered command reports `command_lifecycle` queued → started →
  completed, and the terminal `result` names the command via
  `user_message_uuid` — result before the root command completes;
- the reply text is the `result` field of that terminal event.

Everything else in the real CLI's output stream is noise the harness does not
require, so the stub emits the minimum the parser validates.

The stub runs as the dropped identity with the turn's worktree as CWD, so a
plan's `files` land in the candidate exactly as a real model's edits would.

Plans are JSON: {"steps": [{"match": "substring of the prompt",
"reply": "...", "files": {"path": "content"}, "sleep": seconds,
"fail": "message"}]}. First matching step wins; a step with no "match"
is the default. Looked up from $STUB_PLAN, re-read per turn so the
driver can swap scenarios without restarting the daemon.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import sys
import time
import uuid


def parse_argv(argv: list[str]) -> tuple[str | None, str]:
    """Extract --resume and --model; ignore the rest of the CLI surface.

    The harness resumes by handing the CLI the session record's path —
    `--resume <worktree>/artefacts/claude/projects/*/<uuid>.jsonl` — not a
    bare identity, so the session uuid is the basename. A first turn passes
    no --resume at all and the stub invents a uuid the harness accepts.
    """
    resume: str | None = None
    model = "stub-model"
    arguments = iter(argv)
    for argument in arguments:
        if argument == "--resume":
            value = next(arguments, None)
            if value:
                stem = pathlib.Path(value).name
                if stem.endswith(".jsonl"):
                    stem = stem[: -len(".jsonl")]
                resume = stem
        elif argument == "--model":
            model = next(arguments, None) or model
    return resume, model


def content_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            block.get("text", "") for block in content
            if isinstance(block, dict)
        )
    return ""


def find_step(prompt: str) -> dict:
    # Task prompts name their retained document instead of copying the brief.
    # Follow that explicit reference, as the native agent is instructed to do.
    record = re.search(r"## This task's record\n(\S+) holds the brief", prompt)
    if record:
        task_file = pathlib.Path(record.group(1))
        if task_file.is_file():
            prompt += "\n" + task_file.read_text(encoding="utf-8")
    path = os.environ.get("STUB_PLAN")
    if not path or not os.path.isfile(path):
        return {}
    try:
        plan = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    for step in plan.get("steps", []):
        if not isinstance(step, dict):
            continue
        match = step.get("match")
        if match is None or str(match) in prompt:
            return step
    return {}


def emit(event: dict, transcript: pathlib.Path | None = None) -> None:
    line = json.dumps(event, ensure_ascii=False) + "\n"
    sys.stdout.write(line)
    sys.stdout.flush()
    if transcript is not None:
        # Persist the session the way the real CLI does, inside the worktree:
        # the harness resumes by globbing artefacts/claude/projects/*/<session>.jsonl
        # in the candidate, and accepts exactly one match. A fixed project
        # component keeps one session to one file across worktrees.
        transcript.parent.mkdir(parents=True, exist_ok=True)
        with transcript.open("a", encoding="utf-8") as handle:
            handle.write(line)


def main() -> int:
    resume, model = parse_argv(sys.argv[1:])
    session = resume or str(uuid.uuid4())
    initialized = False

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict) or event.get("type") != "user":
            continue

        command = str(event.get("uuid") or uuid.uuid4())
        prompt = content_text((event.get("message") or {}).get("content"))
        step = find_step(prompt)
        reply = str(step.get("reply", "Acknowledged with no scripted work."))
        transcript = (
            pathlib.Path.cwd()
            / "artefacts" / "claude" / "projects" / "stub" / f"{session}.jsonl"
        )

        if not initialized:
            emit({"type": "system", "subtype": "init",
                  "session_id": session, "model": model}, transcript)
            initialized = True

        emit({"type": "assistant", "session_id": session,
              "parent_tool_use_id": None,
              "message": {"role": "assistant",
                          "content": [{"type": "text", "text": reply}]}}, transcript)
        emit({"type": "command_lifecycle", "session_id": session,
              "command_uuid": command, "state": "queued"}, transcript)
        emit({"type": "command_lifecycle", "session_id": session,
              "command_uuid": command, "state": "started"}, transcript)

        delay = float(step.get("sleep", 0) or 0)
        if delay:
            time.sleep(delay)

        if step.get("fail"):
            # A provider-side failure: the stream completes but the turn
            # reports an error result, which the harness treats as a failed
            # execution (retry / fallback policy), not a crash.
            emit({"type": "result", "subtype": "success", "is_error": True,
                  "terminal_reason": "completed",
                  "errors": [str(step["fail"])],
                  "user_message_uuid": command, "session_id": session}, transcript)
            continue

        for relative, content in (step.get("files") or {}).items():
            target = pathlib.Path(str(relative))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(str(content), encoding="utf-8")

        emit({"type": "result", "subtype": "success", "is_error": False,
              "terminal_reason": "completed", "result": reply,
              "user_message_uuid": command, "session_id": session}, transcript)
        emit({"type": "command_lifecycle", "session_id": session,
              "command_uuid": command, "state": "completed"}, transcript)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

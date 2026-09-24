#!/usr/bin/env python3
"""Local architecture inspector. Render prompts with the runtime's own utilities.

uv run python scripts/serve-stewardship-flow.py --port 8766

Reads code and policy files only. No controller state, model, transport or
deployment calls. Values in angle brackets are visible example substitutions.
"""
from __future__ import annotations

import argparse
import ast
from datetime import UTC, datetime
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib
import inspect
import json
import os
from pathlib import Path
import subprocess
from urllib.parse import urlsplit

from steward_harness import prompts
from steward_harness.world import orientation

ROOT = Path(__file__).resolve().parents[1]
FRAGMENT = ROOT / "output/stewardship-flow/stewardship-flow.html"
STAGES = {
    "conversation": "Conversation",
    "conversation-telegram": "Conversation · Telegram actions and photo roots",
    "conversation-desk": "Conversation · Desk",
    "task": "Task cognition",
    "reflection": "Scheduled reflection",
    "review": "Required review",
    "repair": "Task repair",
    "ownership-answer": "Task · resumed with ownership answer",
    "assessment": "Task result assessment",
    "reflection-assessment": "Scheduled review assessment",
    "target-assessment": "Target result assessment",
    "world-repair": "World conflict reconciliation",
    "world-repair-configured": "World conflict · configured procedure",
    "old-conversation": "Conversation · historical composer",
    "old-task": "Task · historical composer",
    "old-reflection": "Rhythm · historical composer",
    "old-assessment": "Assessment · historical composer",
}


def modified_sources(paths: set[Path]) -> list[str]:
    changes = subprocess.check_output(
        ["git", "status", "--porcelain", "-z", "--no-renames", "--untracked-files=all", "--",
         *sorted(str(p.relative_to(ROOT)) for p in paths)],
        cwd=ROOT, text=True,
    )
    return [entry[3:] for entry in changes.split("\0") if entry]


def render_prompt(stage: str) -> dict:
    """Use current source on every request, including edits made after startup."""
    if stage not in STAGES:
        raise KeyError(stage)
    importlib.reload(prompts)
    importlib.reload(orientation)
    read_text = "Read AGENTS.md, README.md, the world's charter, docs/README.md, episodes.md and Git history in the actual workspace. These are separate files, not an injected context block."
    source = []
    output = "World edits and an optional TASK_PROPOSAL or TASK_ACTION cross controller acceptance."
    mode = "Runtime utility output with placeholder inputs"
    source_files = {Path(__file__).resolve()}
    if stage.startswith("old-"):
        path, function = {
            "old-conversation": ("prompts.py", "build_turn_prompt"),
            "old-task": ("prompts.py", "build_task_prompt"),
            "old-reflection": ("rhythms.py", "_prompt"),
            "old-assessment": ("conversations.py", "_assess_task_result"),
        }[stage]
        text = subprocess.check_output(["git", "show", f"e2a7777:src/steward_harness/{path}"], cwd=ROOT, text=True)
        fn = next(n for n in ast.walk(ast.parse(text)) if isinstance(n, ast.FunctionDef) and n.name == function)
        prompt = ast.get_source_segment(text, fn)
        source = [f"e2a7777:src/steward_harness/{path}:{fn.lineno}"]
        mode = "Historical composer source read directly from Git; not a rendered prompt"
        read_text = "Dynamic historical state and provider context are not reconstructed."
        output = "Historical source only."
    elif stage in {"world-repair", "world-repair-configured"}:
        prompt = prompts.build_conflict_prompt(("<conflicted file>",), "<base ref>", "<world branch>", 1, 20)
        source = [prompts.build_conflict_prompt]
        read_text = "Read the native git-reconciler skill and conflicted files. A configured world.reconcile procedure appends its instructions at runtime; none is configured for this rendering."
        if stage == "world-repair-configured":
            prompt += "\n\n<configured world.reconcile procedure instructions>"
            source.append("src/steward_harness/daemon.py · resolve: appends procedure instructions")
            source_files.add(ROOT / "src/steward_harness/daemon.py")
            read_text = "Read the native git-reconciler skill and conflicted files. The final placeholder represents the contents of the configured procedure.instructions file, appended to the prompt by the runtime. No instance configuration is loaded."
        output = "Resolve the world conflict. World acceptance verifies the resulting tree. This is distinct from repairing a product task."
    elif stage in {"task", "reflection", "review", "repair", "ownership-answer"}:
        procedure = stage in {"reflection", "review"}
        scope = prompts.build_procedure_scope(candidate="<candidate SHA>", base="<base SHA>", read_only=procedure, full_tree=stage=="review", verdict=stage=="review")
        operator_context = {
            "repair": ("repair: <retained repair input: exact work, base and candidate revisions; gate or conflict diagnostics>",),
            "ownership-answer": ("answer: <dated TASK_QUERY answer: matching accepted task metadata, live locks and completeness limits>",),
        }.get(stage, ())
        prompt = prompts.build_task_prompt("<accepted task title>", "<canonical accepted task account>\n\n" + scope, "<repository>", "<task execution id>", operator_context=operator_context)
        source = [prompts.build_task_prompt]
        if procedure:
            source.insert(0, prompts.build_procedure_scope)
        read_text = "The prompt includes the canonical accepted brief, previous findings and current plan from task Git; this viewer does not read private controller state."
        if stage == "repair":
            read_text += "\n\nThe Incoming task context block above carries an example retained repair input, supplied by TaskRunner on the resumed slice. Exact diagnostics remain in the accepted task input history."
        if stage == "ownership-answer":
            read_text += "\n\nThe task previously closed with QUESTION: TASK_QUERY. The controller retained an answer on that same task; TaskRunner supplies it as Incoming task context on continuation. The answer is dated evidence, not authority to steer other work."
        if procedure:
            policy = "config/procedures/security-review.md"
            if stage == "reflection":
                policy = os.environ.get("STEWARD_REFLECTION_POLICY", "")
            if policy and (ROOT / policy).is_file():
                read_text += f"\n\nCurrent policy file: {policy}\n(This is the source file now; an admitted run uses its frozen accepted copy.)\n\n" + (ROOT / policy).read_text()
                source_files.add(ROOT / policy)
            else:
                read_text += "\n\nNo reflection policy ships with the harness: it belongs to the instance that configures the procedure. Set STEWARD_REFLECTION_POLICY to a repository-relative path to preview one."
        output = "Findings, COMMIT, DISPOSITION and QUESTION; read-only procedures also return VERDICT. See the exact closure instructions above."
    else:
        request = "<operator message>"
        if not stage.startswith("conversation"):
            request = prompts.build_result_assessment_request("<accepted task brief>", "<dated target observation>" if stage=="target-assessment" else "<accepted task findings>", quiet=stage in {"reflection-assessment", "target-assessment"})
            source.append(prompts.build_result_assessment_request)
        enabled_telegram = stage == "conversation-telegram"
        prompt = prompts.build_turn_prompt(
            request, orientation.world_orientation(), "<world turn id>",
            transport="desk" if stage == "conversation-desk" else "telegram",
            telegram_actions=("pin_reply", "pin_message") if enabled_telegram else (),
            delivery_roots=("<configured absolute photo delivery root>",) if enabled_telegram else (),
        )
        source.extend([prompts.build_turn_prompt, orientation.world_orientation])
    locations = [s if isinstance(s, str) else f"{Path(inspect.getsourcefile(s)).relative_to(ROOT)}:{inspect.getsourcelines(s)[1]} · {s.__name__}" for s in source]
    source_files.update(Path(inspect.getsourcefile(s)) for s in source if not isinstance(s, str))
    revision = subprocess.check_output(["git", "rev-parse", "e2a7777" if stage.startswith("old-") else "HEAD"], cwd=ROOT, text=True).strip()
    return dict(stage=stage, label=STAGES[stage], prompt=prompt, reads=read_text, output=output, source=locations,
                mode=mode, revision=revision, modified_sources=[] if stage.startswith("old-") else modified_sources(source_files),
                rendered_at=datetime.now(UTC).isoformat(), characters=len(prompt),
                scope=("Historical composer source; dynamic inputs are not reconstructed." if stage.startswith("old-") else
                       "Current working-tree functions. Placeholder values are not live inputs. Provider system instructions, native history, skill catalogue and tool results are not included."))


def document() -> str:
    # No prompt text is persisted in the fragment or shell. The browser requests
    # a fresh rendering when a cognition stage is selected.
    css = (ROOT / "scripts/stewardship-flow.css").read_text()
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Stewardship — signals and cognition</title><style>{css}</style></head>
<body><main>{FRAGMENT.read_text()}</main></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = urlsplit(self.path).path
        try:
            if path == "/api/stages":
                content, kind = json.dumps(STAGES), "application/json"
            elif path.startswith("/api/prompt/"):
                content, kind = json.dumps(render_prompt(path.removeprefix("/api/prompt/"))), "application/json"
            elif path == "/":
                content, kind = document(), "text/html"
            else:
                self.send_error(404)
                return
        except KeyError:
            self.send_error(404)
            return
        except Exception as error:
            self.send_error(500, escape(str(error)))
            return
        data = content.encode()
        self.send_response(200)
        self.send_header("Content-Type", kind + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *_args):
        pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args()
    with ThreadingHTTPServer(("127.0.0.1", args.port), Handler) as server:
        print(f"http://127.0.0.1:{server.server_port}/", flush=True)
        server.serve_forever()

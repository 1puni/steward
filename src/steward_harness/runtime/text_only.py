"""Audited native options for tool-free application inference.

New native releases must pass the local wire acceptance before extending these
version pins. Unknown options or a tool event fail the call, never relax it.
"""
from pathlib import Path
from dataclasses import replace
import math
import time

from steward_harness.runtime.contracts import RuntimeExecutionError

NATIVE_VERSIONS = {"codex": "codex-cli 0.160.1", "claude": "2.1.281 (Claude Code)"}

# Audited against the installed Codex: the Responses request has no tools.
CODEX_DISABLED_FEATURES = (
    "shell_tool", "unified_exec", "apps", "multi_agent", "multi_agent_v2",
    "hooks", "plugins", "remote_plugin", "skill_search",
    "skill_mcp_dependency_install", "browser_use", "browser_use_external",
    "computer_use", "image_generation", "view_image", "code_mode",
    "code_mode_host", "goals", "sleep_tool", "default_mode_request_user_input",
    "request_permissions_tool", "tool_suggest", "memories", "shell_snapshot",
)
CODEX_CONFIG = (
    'web_search = "disabled"\nproject_doc_max_bytes = 0\n'
    '[history]\npersistence = "none"\n'
    '[tools.update_plan]\nenabled = false\n'
    '[skills]\ninclude_instructions = false\n'
    '[skills.bundled]\nenabled = false\n'
    '[features]\nskip_host_skill_discovery = true\n'
    + ''.join(f'{name} = false\n' for name in CODEX_DISABLED_FEATURES)
)


def verify_version(broker, executable: Path, family: str, *, timeout: int = 10) -> None:
    result = broker.run([str(executable), "--version"], cwd="/", timeout=timeout)
    expected = NATIVE_VERSIONS["codex" if family == "codex" else "claude"]
    if result.returncode or result.stdout.strip() != expected:
        raise RuntimeExecutionError("Unsupported native version for text-only inference")


def verify_text_request(broker, executable, family, request):
    """Version inspection consumes the same finite native call deadline."""
    budget = request.timeout_seconds or 180
    deadline = time.monotonic() + budget
    verify_version(broker, executable, family, timeout=min(10, budget))
    remaining = math.ceil(deadline - time.monotonic())
    if remaining <= 0:
        raise RuntimeExecutionError("Text-only version check exceeded deadline")
    return replace(request, timeout_seconds=remaining)


def reject_claude_tools(event: dict) -> None:
    """Reject complete and partial tool blocks before ordinary stream handling."""
    if event.get("type") == "control_request" or event.get("parent_tool_use_id"):
        raise RuntimeExecutionError("Tool activity in text-only inference")
    if event.get("type") == "system" and event.get("subtype") == "init":
        if event.get("tools") != []:
            raise RuntimeExecutionError("Native text-only initialization exposed tools")
    if event.get("type") == "system" and event.get("subtype") in {
        "task_started", "task_notification", "hook_started", "hook_response",
    }:
        raise RuntimeExecutionError("Native activity in text-only inference")
    blocks = event.get("message", {}).get("content", [])
    partial = event.get("event", {}).get("content_block")
    if isinstance(partial, dict):
        blocks = [*blocks, partial]
    if isinstance(blocks, list) and any(
        isinstance(block, dict) and block.get("type") in {
            "tool_use", "server_tool_use", "tool_result",
        } for block in blocks
    ):
        raise RuntimeExecutionError("Tool activity in text-only inference")

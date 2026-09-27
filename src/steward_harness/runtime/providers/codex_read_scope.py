"""A private Codex home with an explicit public read grant and no integrations."""
from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path

from steward_harness.runtime.contracts import RuntimeExecutionError, RuntimeRequest
from steward_harness.runtime.execution import UntrustedExecutionBroker

PROFILE = "steward-public"
# Public cognition has only sandboxed local commands over the explicit read grant.
DISABLED_FEATURES = (
    "apps", "connectors", "plugins", "recommended_plugins", "remote_plugin",
    "hooks", "codex_hooks", "plugin_hooks", "multi_agent", "multi_agent_v2",
    "memories", "memory_tool", "external_agent_memory_import", "js_repl",
    "browser_use", "computer_use", "image_generation", "imagegenext",
    "view_image", "apply_patch_freeform",
    "skill_mcp_dependency_install", "skill_search", "request_permissions_tool",
    "request_permissions", "remote_control", "shell_snapshot", "shell_snapshot_v2",
)


def scope_config(roots: tuple[Path, ...], workspace: Path) -> dict:
    return {
        "default_permissions": PROFILE,
        "approval_policy": "never",
        "approvals_reviewer": "user",
        "web_search": "disabled",
        "project_doc_max_bytes": 0,
        "allow_login_shell": False,
        "shell_environment_policy": {"inherit": "none", "set": {"PATH": "/usr/bin:/bin"}},
        # Luna dispatches shell tools through exec. This host only dispatches the
        # already-confined tools; disabling it also disables permitted reads.
        "features": {**dict.fromkeys(DISABLED_FEATURES, False), "skip_host_skill_discovery": True,
                     "code_mode": True, "code_mode_host": True},
        "agents": {"enabled": False},
        "mcp_servers": {},
        "permissions": {PROFILE: {
            "filesystem": {":minimal": "read", str(workspace): "read",
                           **{str(root): "read" for root in roots}},
            "network": {"enabled": False},
        }},
    }


def config_toml(config: dict) -> str:
    """Use JSON-compatible TOML scalar values, with quoted dotted key segments."""
    result = []

    def flatten(value, parts):
        if isinstance(value, dict) and value:
            for key, child in value.items():
                flatten(child, (*parts, key))
        else:
            result.append(".".join(json.dumps(key) for key in parts)
                          + "=" + ("{}" if value == {} else json.dumps(value)))

    flatten(config, ())
    return "\n".join(result) + "\n"


def verify_scope_config(config: dict, expected: dict) -> None:
    """Refuse inherited integrations or a policy changed by native configuration."""
    def matches(actual, desired):
        if isinstance(desired, dict):
            return isinstance(actual, dict) and (actual == {} if not desired else
                all(key in actual and matches(actual[key], value) for key, value in desired.items()))
        return actual == desired

    def without_unset(value):
        return {key: without_unset(child) for key, child in value.items() if child is not None} \
            if isinstance(value, dict) else value

    config = without_unset(config)
    if any(config.get(key) for key in ("developer_instructions", "model_instructions_file",
                                       "experimental_instructions_file", "notify", "hooks")):
        raise RuntimeExecutionError("Public Codex cannot inherit instructions or hooks")
    if (not matches(config, expected)
            or config.get("permissions", {}).get(PROFILE) != expected["permissions"][PROFILE]
            or config.get("shell_environment_policy") != expected["shell_environment_policy"]):
        raise RuntimeExecutionError("Codex did not enforce the public read configuration")


_PREPARE = r'''
import json, os, pathlib, sys, tempfile
source, scope, roots, config = json.load(sys.stdin)
source = pathlib.Path(source)
if source != source.resolve():
    raise ValueError("public native home must use its canonical path")
# No selected public root may contain the provider's private records/auth.
for raw in roots:
    root = pathlib.Path(raw).resolve(strict=True)
    if (not (root.is_dir() or root.is_file())
            or root == source or root in source.parents or source in root.parents):
        raise ValueError('public read roots must exclude the native home')
parent = source / '.steward-read-scopes'
home = parent / scope
workspace = home / 'workspace'
for directory in (parent, home, workspace):
    if directory.is_symlink():
        raise ValueError('public native namespace must not be a symlink')
    directory.mkdir(mode=0o700, exist_ok=True)
# Only authentication is shared. No config, skills, plugins, memory or history
# from the operator home is exposed to this native session.
auth = home / 'auth.json'
if not auth.exists() and not auth.is_symlink() and (source / 'auth.json').is_file():
    auth.symlink_to(source / 'auth.json')
with tempfile.NamedTemporaryFile(mode='w', dir=home, delete=False) as stream:
    stream.write(config)
    stream.flush()
    os.fsync(stream.fileno())
os.replace(stream.name, home / 'config.toml')
print(json.dumps({'home': str(home), 'workspace': str(workspace)}))
'''


def prepare_scope(broker: UntrustedExecutionBroker, request: RuntimeRequest,
                  native_home: Path) -> tuple[RuntimeRequest, Path]:
    scope = request.read_scope
    assert scope is not None
    identity = hashlib.sha256(scope.identity.encode()).hexdigest()
    workspace = native_home / ".steward-read-scopes" / identity / "workspace"
    result = broker.run(
        [broker.python_executable, "-I", "-c", _PREPARE], cwd="/", timeout=30,
        input_text=json.dumps([str(native_home), identity, list(map(str, scope.roots)),
                               config_toml(scope_config(scope.roots, workspace))]),
    )
    if result.returncode:
        raise RuntimeExecutionError("Cannot prepare private public-desk native home")
    paths = json.loads(result.stdout)
    return replace(request, cwd=Path(paths["workspace"])), Path(paths["home"])

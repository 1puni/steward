"""Declarative Pydantic V2 schemas for steward and repository configurations."""

from __future__ import annotations

import os
import re
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

from steward_harness.git import validate_git_branch, validate_git_remote_url
from steward_harness.provider_types import (
    ModelChoice,
    ProviderFamily,
    ProviderProfile,
)

TelegramAgentAction = Literal["pin_reply", "pin_message"]

_DEFAULT_UNTRUSTED_ENVIRONMENT = (
    "COLORTERM",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "LANG",
    "LANGUAGE",
    "LC_ALL",
    "NO_PROXY",
    "PATH",
    "SSL_CERT_DIR",
    "SSL_CERT_FILE",
    "TERM",
    "TZ",
)
_PRIVILEGED_ENVIRONMENT_NAMES = frozenset(
    {
        "DOCKER_AUTH_CONFIG",
        "DOCKER_CONFIG",
        "DOCKER_CONTEXT",
        "DOCKER_HOST",
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "KUBECONFIG",
        "SSH_AGENT_PID",
        "SSH_AUTH_SOCK",
        "TELEGRAM_BOT_TOKEN",
    }
)
_PRIVILEGED_ENVIRONMENT_PREFIXES = ("AWS_", "AZURE_", "ARM_", "KUBE_")


def _absolute_path(path: str) -> Path:
    """Normalize configured topology without following a live terminal symlink."""
    return Path(os.path.abspath(path))


def _require_bounded_absolute(label: str, value: str) -> None:
    path = Path(value)
    if not path.is_absolute() or path == Path(path.anchor):
        raise ValueError(f"{label} must be a bounded absolute path")


def _require_https_base_url(label: str, value: str) -> None:
    # Provider credentials travel to this endpoint, so plain HTTP never applies.
    parts = urlsplit(value)
    if parts.scheme != "https" or not parts.netloc or len(value) > 512:
        raise ValueError(f"{label} must be a bounded https:// base URL")


class RepairMode(StrEnum):
    """The repair authority granted to a pipeline."""

    MONITOR_ONLY = "monitor_only"
    AUTONOMOUS = "autonomous"


class IdentityConfig(BaseModel):
    """Operational identity of the steward instance."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    slug: str = Field(min_length=1, pattern=r"^[a-z0-9_-]+$")


class UntrustedExecutionConfig(BaseModel):
    """OS identity and inherited environment for model-controlled commands."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    user: str | None = None
    group: str | None = None
    home: str | None = None
    tmpdir: str = "/tmp"
    inherited_environment: tuple[str, ...] = _DEFAULT_UNTRUSTED_ENVIRONMENT

    @model_validator(mode="after")
    def validates_identity_boundary(self) -> "UntrustedExecutionConfig":
        if self.user is None and (self.group is not None or self.home is not None):
            raise ValueError("execution group/home requires an execution user")
        if self.user in {"root", "0"}:
            raise ValueError("untrusted execution user must not be root")
        for field_name, value in (("home", self.home), ("tmpdir", self.tmpdir)):
            if value is None:
                continue
            _require_bounded_absolute(f"execution {field_name}", value)
        if len(set(self.inherited_environment)) != len(self.inherited_environment):
            raise ValueError("execution inherited_environment must be unique")
        for name in self.inherited_environment:
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) is None:
                raise ValueError(f"invalid inherited environment name: {name!r}")
            if name in _PRIVILEGED_ENVIRONMENT_NAMES or name.startswith(
                _PRIVILEGED_ENVIRONMENT_PREFIXES
            ):
                raise ValueError(
                    f"privileged environment variable cannot be inherited: {name}"
                )
        return self


class CommandSpec(BaseModel):
    """One allowlisted command expressed without an arbitrary shell."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    argv: tuple[str, ...] = Field(min_length=1)
    timeout_seconds: int = Field(default=600, ge=1, le=7200)
    cwd: str = "."

    @model_validator(mode="after")
    def rejects_shell_launchers(self) -> "CommandSpec":
        if len(self.argv) >= 2:
            launcher = (
                self.argv[0]
                .replace("\\", "/")
                .rsplit("/", maxsplit=1)[-1]
                .lower()
                .removesuffix(".exe")
            )
            option = self.argv[1].lower()
            shell_launchers = {
                "sh",
                "bash",
                "zsh",
                "fish",
                "dash",
                "ksh",
                "cmd",
                "powershell",
            }
            shell_options = {"-c", "-lc", "/c", "-command"}
            if launcher in shell_launchers and option in shell_options:
                raise ValueError("shell launchers are not allowed in command argv")
        return self


class CommandProbeSpec(BaseModel):
    """A pipeline observed by running an allowlisted argument array."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    type: Literal["command"]
    command: CommandSpec


class FilesystemProbeSpec(BaseModel):
    """A freshness check over matching produced files."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    type: Literal["filesystem"]
    path: str
    pattern: str = "*"
    recursive: bool = False
    minimum_files: int = Field(default=1, ge=1)
    minimum_bytes: int = Field(default=1, ge=1)
    max_age_seconds: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def validates_bounded_location(self) -> "FilesystemProbeSpec":
        location = Path(self.path)
        if not location.is_absolute():
            raise ValueError("filesystem probe path must be absolute")
        if not self.pattern or "/" in self.pattern or "\\" in self.pattern or ".." in self.pattern:
            raise ValueError("filesystem probe pattern must be one filename pattern")
        return self


ProbeSpec = Annotated[
    CommandProbeSpec | FilesystemProbeSpec,
    Field(discriminator="type"),
]


class PipelineConfig(BaseModel):
    """Health and repair policy for one data pipeline."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repository: str
    observation_interval_seconds: int = Field(default=300, ge=1)
    repair_mode: RepairMode = RepairMode.AUTONOMOUS
    probe: ProbeSpec


class RepositoryConfig(BaseModel):
    """Policy for one managed repository."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str
    remote_url: str
    default_branch: str = "main"
    gates: tuple[CommandSpec, ...] = ()
    requires: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validates_path(self) -> "RepositoryConfig":
        _require_bounded_absolute("repository path", self.path)
        validate_git_remote_url(self.remote_url, allow_local=True)
        validate_git_branch(self.default_branch)
        return self


class TelegramAdapterCommandConfig(BaseModel):
    """One product-specific Telegram command executed without a shell.

    The harness owns parsing, authorization, topic scoping, and reporting. The
    adapter owns only the allowlisted executable (for example an app's APK
    builder). Any parsed argument is appended as one argv element.

    ``authority`` says whose identity runs it. ``agent`` goes through the
    execution broker like any model tool. ``controller`` is for acts only the
    operator may cause, such as minting a pairing link: a model turn can reach
    anything the agent identity can run, so these run as the controller
    instead, from an executable the agent cannot replace.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    description: str = Field(min_length=1, max_length=256)
    argument_mode: Literal[
        "none",
        "optional_token",
        "optional_remainder",
        "required_token",
        "required_remainder",
    ] = "none"
    command: CommandSpec
    allowed_topics: tuple[str, ...] = ()
    authority: Literal["agent", "controller"] = "agent"

    @model_validator(mode="after")
    def validates_controller_executable(self) -> "TelegramAdapterCommandConfig":
        if self.authority == "controller":
            _require_bounded_absolute("controller adapter executable", self.command.argv[0])
        return self


class TelegramConfig(BaseModel):
    """Configured fixed-chat Telegram bot interface."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    token_path: str = "/etc/steward/telegram-token"
    chat_id: int
    allowed_users: tuple[int, ...] = Field(min_length=1)
    # Admit whoever currently administers the chat, on top of `allowed_users`.
    # Group membership is the operator's existing, visible record of who speaks
    # for the project, so this stops admission drifting from it — the failure it
    # exists for is a new administrator being silently ignored, which reads as
    # the steward being broken rather than as a permission. `allowed_users`
    # stays required and stays break-glass: it is the list that still admits
    # when Telegram is unreachable or someone loses their admin rights.
    allow_group_administrators: bool = False
    topics: dict[str, int] = Field(default_factory=dict)
    # Opt-in deny-list over `topics`: a named topic is a one-way feed the
    # steward consumes but never acts on. Absence still means admitted, so an
    # undeclared topic stays a live session surface — the whole point of
    # e8a6d36, which removed the topic allowlist that silently swallowed
    # operator messages. Only a deliberately named topic goes inert.
    passive_topics: tuple[str, ...] = ()
    media_max_mb: int = Field(default=50, ge=1, le=2000)
    inbound_media_dir: str | None = None
    botapi_base: str | None = None
    delivery_outbox_dir: str | None = None
    delivery_quarantine_dir: str | None = None
    delivery_roots: tuple[str, ...] = ()
    agent_actions: tuple[TelegramAgentAction, ...] = ()
    adapter_commands: dict[str, TelegramAdapterCommandConfig] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validates_adapter_commands(self) -> "TelegramConfig":
        _require_bounded_absolute("Telegram token_path", self.token_path)
        if len(set(self.topics.values())) != len(self.topics):
            raise ValueError("Telegram topic IDs must be unique")
        unknown_passive = set(self.passive_topics) - set(self.topics)
        if unknown_passive:
            names = ", ".join(sorted(unknown_passive))
            raise ValueError(f"Telegram passive_topics has unknown topics: {names}")
        if self.inbound_media_dir is not None:
            _require_bounded_absolute(
                "Telegram inbound_media_dir", self.inbound_media_dir
            )
        if len(set(self.agent_actions)) != len(self.agent_actions):
            raise ValueError("Telegram agent_actions must be unique")
        builtins = {
            "cancel", "clear", "git", "model", "model_family", "pause",
            "resume", "rhythm", "status", "task", "tasks",
        }
        for name, adapter in self.adapter_commands.items():
            if re.fullmatch(r"[a-z][a-z0-9_]{0,31}", name) is None:
                raise ValueError(f"invalid Telegram adapter command name: {name!r}")
            if name in builtins:
                raise ValueError(f"Telegram adapter command shadows harness command: {name}")
            unknown_topics = set(adapter.allowed_topics) - set(self.topics)
            if unknown_topics:
                names = ", ".join(sorted(unknown_topics))
                raise ValueError(f"Telegram adapter command {name!r} has unknown topics: {names}")
        for field_name, configured_path in (
            ("delivery_outbox_dir", self.delivery_outbox_dir),
            ("delivery_quarantine_dir", self.delivery_quarantine_dir),
        ):
            if configured_path is not None:
                _require_bounded_absolute(f"Telegram {field_name}", configured_path)
        if self.delivery_outbox_dir is not None and self.delivery_quarantine_dir is None:
            raise ValueError(
                "Telegram delivery_outbox_dir requires delivery_quarantine_dir"
            )
        if self.delivery_outbox_dir is not None and not self.delivery_roots:
            raise ValueError("Telegram delivery_outbox_dir requires delivery_roots")
        if (
            self.delivery_outbox_dir is not None
            and self.delivery_outbox_dir == self.delivery_quarantine_dir
        ):
            raise ValueError("Telegram delivery outbox and quarantine must be distinct")
        for root in self.delivery_roots:
            path = Path(root)
            if not path.is_absolute() or path == Path(path.anchor):
                raise ValueError("Telegram delivery_roots must be bounded absolute paths")
        return self


class ProviderConfig(BaseModel):
    """Provider execution limits and model selection."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    default_family: ProviderFamily = "codex"
    fallback_families: tuple[ProviderFamily, ...] = ("claude", "glm")
    default_profile: ProviderProfile = "balanced"
    workdir: str = "/var/lib/steward/work"
    state_db: str = "/var/lib/steward/state.db"
    timeout_seconds: int = Field(default=1200, ge=10, le=7200)

    claude_executable: str = "/usr/local/bin/claude"
    codex_executable: str = "/usr/bin/codex"
    glm_credential_path: str = "/etc/steward/zai-token"
    # Anthropic-compatible endpoint the Claude CLI families target. Routing
    # through a gateway is one base URL plus a credential file; omit the pair
    # to keep native-home login.
    glm_anthropic_base_url: str = "https://api.z.ai/api/anthropic"
    claude_anthropic_base_url: str | None = None
    claude_credential_path: str | None = None

    # A profile names a model, or a model and the effort to run it at. The
    # bare string keeps meaning exactly what it always did; see `ModelChoice`.
    models: dict[str, dict[str, ModelChoice | str]] = Field(default_factory=dict)
    # Explicit stewardship-owned native configuration, separate from personal
    # provider homes. Adapters interpret their own path; lifecycle stays neutral.
    native_homes: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validates_provider_order(self) -> "ProviderConfig":
        order = (self.default_family, *self.fallback_families)
        if len(set(order)) != len(order):
            raise ValueError("provider order must contain unique families")
        for provider_id in (*order, *self.models, *self.native_homes):
            if re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", provider_id) is None:
                raise ValueError(f"invalid provider ID: {provider_id!r}")
        for provider_id, path in self.native_homes.items():
            _require_bounded_absolute(f"provider native home {provider_id!r}", path)
        paths = [Path(path).resolve() for path in self.native_homes.values()]
        for i, path in enumerate(paths):
            if any(path == other or path in other.parents or other in path.parents for other in paths[:i]):
                raise ValueError("provider native homes must not overlap")
        for label, value in (
            ("provider workdir", self.workdir),
            ("provider state_db", self.state_db),
            ("provider glm_credential_path", self.glm_credential_path),
        ):
            _require_bounded_absolute(label, value)
        _require_https_base_url("provider glm_anthropic_base_url", self.glm_anthropic_base_url)
        if (self.claude_anthropic_base_url is None) != (self.claude_credential_path is None):
            raise ValueError(
                "provider claude_anthropic_base_url and claude_credential_path "
                "must be configured together"
            )
        if self.claude_anthropic_base_url is not None:
            _require_https_base_url(
                "provider claude_anthropic_base_url", self.claude_anthropic_base_url,
            )
        if self.claude_credential_path is not None:
            _require_bounded_absolute(
                "provider claude_credential_path", self.claude_credential_path,
            )
        profiles = {"fast", "balanced", "deep"}
        for family, configured in self.models.items():
            unknown_profiles = set(configured) - profiles
            if unknown_profiles:
                raise ValueError(
                    f"unsupported {family} model profiles: "
                    + ", ".join(sorted(unknown_profiles))
                )
        return self

    @property
    def family_order(self) -> tuple[ProviderFamily, ...]:
        """Ordered automatic provider policy, primary first."""
        return (self.default_family, *self.fallback_families)


class WorldConfig(BaseModel):
    """Configured Git-world cognitive storage."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    root: str = "/var/lib/steward/world"
    reconcile: str | None = None
    # Optional shared controller-owned lock directory. Set this only when an
    # external process already serializes writes to the same Git world.
    lock_dir: str | None = None

    @model_validator(mode="after")
    def validates_paths(self) -> "WorldConfig":
        _require_bounded_absolute("world root", self.root)
        if self.lock_dir is not None:
            _require_bounded_absolute("world lock_dir", self.lock_dir)
        return self

    def lock_path(self, state_db: str | Path) -> Path:
        """The world lease file: the process takes it, the boundary protects it."""
        if self.lock_dir is not None:
            return Path(self.lock_dir) / "world.lock"
        state = Path(state_db)
        return state.parent / f".{state.name}.world.lock"


class IncidentPolicy(BaseModel):
    """Automated incident diagnosis and rate limits."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    confirm_after_failures: int = Field(default=2, ge=1)
    transient_retries: int = Field(default=1, ge=0)
    repair_attempts_per_24h: int = Field(default=3, ge=0)
    landed_changes_per_24h: int = Field(default=2, ge=0)
    recovery_cooldown_seconds: int = Field(default=1800, ge=0)
    # Pacing between failed repair attempts, so a broken pipeline cannot burn
    # its whole 24h budget in back-to-back probe ticks.
    repair_failure_cooldown_seconds: int = Field(default=900, ge=0)
    # Consecutive failed repairs (since the last landed one) before the harness
    # gives up, escalates to an operator task, and suspends auto-repair.
    escalate_after_failed_repairs: int = Field(default=2, ge=1)


class ControllerConfig(BaseModel):
    """Scheduler limits."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    poll_seconds: int = Field(default=5, ge=1, le=60)
    # One budget for everything the steward schedules for itself. It used to
    # be two pools of this size, one for tasks and one for repositories, so 8
    # keeps the capacity that split gave and drops the reservation.
    workers: int = Field(default=8, ge=1, le=32)
    health_bind: str | None = None  # host:port loopback healthz for self-deploy
    world_session_idle_seconds: int = Field(default=604800, ge=1)


class DeskConfig(BaseModel):
    """Configured web desk bridge (filesystem inbox shared with the sidecar)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    inbox_dir: str = "/var/lib/steward/desk-inbox"
    events_file: str = "/var/lib/steward/desk/events.jsonl"

    @model_validator(mode="after")
    def validates_paths(self) -> "DeskConfig":
        _require_bounded_absolute("desk inbox_dir", self.inbox_dir)
        _require_bounded_absolute("desk events_file", self.events_file)
        return self


class TaskIntakeConfig(BaseModel):
    """A private Git remote whose tasks/* refs are accepted controller decisions."""
    model_config = ConfigDict(frozen=True, extra="forbid")
    remote_url: str | None = None

    @model_validator(mode="after")
    def validates_remote(self):
        if self.remote_url:
            validate_git_remote_url(self.remote_url, allow_local=True)
        return self


class ProcedureConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    instructions: str
    provider: str
    model: ModelChoice
    access: Literal["read-only", "workspace-write"] = "read-only"


class QuietSchedule(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    quiet: int = Field(gt=0, description="Seconds without newly observed Git activity")


class ProcedureRhythmConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    schedule: Annotated[int, Field(gt=0)] | QuietSchedule
    procedure: str
    input: str
    owner: str | None  # Explicit null retains findings without assessment/delivery.
    workdir: str | None = None

    @model_validator(mode="after")
    def validates_workdir(self):
        if self.workdir is not None:
            _require_bounded_absolute("rhythm workdir", self.workdir)
        return self


class TargetConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    ref: str
    driver: str
    requires: tuple[str, ...] = ()
    timeout_seconds: int = Field(default=300, ge=1, le=3600)


class StewardConfig(BaseModel):
    """Root configuration document for a steward instance."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    procedures: dict[str, ProcedureConfig] = Field(default_factory=dict)
    rhythms: dict[str, ProcedureRhythmConfig] = Field(default_factory=dict)
    targets: dict[str, TargetConfig] = Field(default_factory=dict)
    tasks: TaskIntakeConfig = Field(default_factory=TaskIntakeConfig)
    identity: IdentityConfig
    execution: UntrustedExecutionConfig = UntrustedExecutionConfig()
    controller: ControllerConfig = ControllerConfig()
    incident_policy: IncidentPolicy = IncidentPolicy()
    provider: ProviderConfig = ProviderConfig()
    telegram: TelegramConfig | None = None
    world: WorldConfig | None = None
    desk: DeskConfig | None = None
    repositories: dict[str, RepositoryConfig] = Field(default_factory=dict)
    pipelines: dict[str, PipelineConfig] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validates_cross_references(self) -> "StewardConfig":
        for name in (*self.procedures, *self.rhythms, *self.targets):
            if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}", name):
                raise ValueError(f"invalid procedure, rhythm or target name: {name!r}")
        for name, procedure in self.procedures.items():
            _require_bounded_absolute(f"procedure {name} instructions", procedure.instructions)
            if not procedure.provider.strip():
                raise ValueError("procedure provider must be nonblank")
        for binding in (*self.rhythms.values(), *self.targets.values()):
            reference = binding.input if isinstance(binding, ProcedureRhythmConfig) else binding.ref
            parts = reference.split("/", 2)
            if len(parts) != 3 or parts[0] != "repositories" or parts[1] not in self.repositories:
                raise ValueError(f"unknown configured input {reference!r}")
            validate_git_branch(parts[2])
        for rhythm in self.rhythms.values():
            if rhythm.procedure not in self.procedures:
                raise ValueError("rhythm names an unknown procedure")
            if rhythm.workdir is not None and self.procedures[rhythm.procedure].access != "read-only":
                raise ValueError("organisation rhythm workdir requires a read-only procedure")
            if rhythm.owner is not None:
                kind, _, reference = rhythm.owner.partition(":")
                if kind == "telegram":
                    if not self.telegram or reference not in {str(t) for t in self.telegram.topics.values()}:
                        raise ValueError("rhythm owner must name a configured Telegram topic")
                elif kind != "desk" or not self.desk or not reference.strip():
                    raise ValueError("rhythm owner must name an enabled desk or Telegram conversation")
        if self.world and self.world.reconcile:
            procedure = self.procedures.get(self.world.reconcile)
            if procedure is None or procedure.access != "workspace-write":
                raise ValueError("world reconciliation must name a workspace-write procedure")
        for repository in self.repositories.values():
            for requirement in repository.requires:
                if requirement not in self.procedures or self.procedures[requirement].access != "read-only":
                    raise ValueError("publication requirements must name read-only procedures")
        for name, target in self.targets.items():
            _require_bounded_absolute(f"target {name} driver", target.driver)
            if len(set(target.requires)) != len(target.requires):
                raise ValueError("target requirements must be unique")
            for requirement in target.requires:
                if requirement not in self.procedures or self.procedures[requirement].access != "read-only":
                    raise ValueError("target requirements must name read-only procedures")
        git_roots = [Path(repo.path).resolve() for repo in self.repositories.values()]
        if self.world is not None:
            git_roots.append(Path(self.world.root).resolve())
        for path in (*[Path(p.instructions).resolve() for p in self.procedures.values()],
                     *[Path(t.driver).resolve() for t in self.targets.values()]):
            if any(path == root or root in path.parents for root in git_roots):
                raise ValueError("procedure and target authority must be outside model-writable roots")
        for path in self.provider.native_homes.values():
            native_home = Path(path).resolve()
            if any(root == native_home or root in native_home.parents for root in git_roots):
                raise ValueError("native provider homes contain private runtime data and must be outside Git worlds and repositories")
        self._validate_controller_paths()
        repository_paths: dict[str, str] = {}
        publication_targets: dict[tuple[str, str], str] = {}
        for repository_name, repository in self.repositories.items():
            if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", repository_name) is None:
                raise ValueError(
                    f"repository name {repository_name!r} must be a simple slug"
                )
            prior = repository_paths.setdefault(str(Path(repository.path).resolve()), repository_name)
            if prior != repository_name:
                raise ValueError(f"repositories {prior!r} and {repository_name!r} share a working repository")
            target = (repository.remote_url, repository.default_branch)
            prior = publication_targets.setdefault(target, repository_name)
            if prior != repository_name:
                raise ValueError(f"repositories {prior!r} and {repository_name!r} share a publication target")
            if self.execution.user is not None and Path(
                repository.remote_url
            ).is_absolute():
                raise ValueError(
                    f"repository {repository_name!r} local remote_url is not allowed "
                    "across a split-identity boundary"
                )
        for pipeline_name, pipeline in self.pipelines.items():
            if (
                re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", pipeline_name)
                is None
            ):
                raise ValueError(
                    f"pipeline name {pipeline_name!r} must be a simple slug"
                )
            if pipeline.repository not in self.repositories:
                raise ValueError(
                    f"pipeline {pipeline_name!r} references unknown repository {pipeline.repository!r}"
                )
        return self

    def _validate_controller_paths(self) -> None:
        """Keep controller authority outside model-writable filesystem roots."""
        if self.execution.user is None:
            return
        telegram = self.telegram
        if telegram is not None and telegram.inbound_media_dir is None:
            raise ValueError(
                "split-identity Telegram requires an explicit inbound_media_dir"
            )

        writable_roots = {
            _absolute_path(self.execution.tmpdir),
            _absolute_path(self.provider.workdir),
            *(_absolute_path(path) for path in self.provider.native_homes.values()),
            *(
                _absolute_path(repository.path)
                for repository in self.repositories.values()
            ),
        }
        if self.execution.home is not None:
            writable_roots.add(_absolute_path(self.execution.home))
        world = self.world
        if world is not None:
            writable_roots.add(_absolute_path(world.root))

        private_files = {"provider.state_db": _absolute_path(self.provider.state_db)}
        if telegram is not None:
            private_files["telegram.token_path"] = _absolute_path(
                telegram.token_path
            )
            if telegram.inbound_media_dir is not None:
                private_files["telegram.inbound_media_dir"] = _absolute_path(
                    telegram.inbound_media_dir
                )
        if world is not None and world.lock_dir is not None:
            private_files["world.lock_dir"] = _absolute_path(world.lock_dir)

        for label, path in private_files.items():
            for root in writable_roots:
                if path == root or root in path.parents:
                    raise ValueError(
                        f"controller-owned {label} must be outside model-writable root {root}"
                    )

        for label, path in (
            *((f"procedure {name}", Path(p.instructions).resolve()) for name, p in self.procedures.items()),
            *((f"target {name}", Path(t.driver).resolve()) for name, t in self.targets.items()),
        ):
            if any(path == root.resolve() or root.resolve() in path.parents for root in writable_roots):
                raise ValueError(f"{label} authority must be outside model-writable roots")

    @property
    def requires_execution_boundary(self) -> bool:
        """Whether the configured instance must prove a separate execution identity."""
        return (
            self.execution.user is not None
            or self.telegram is not None
            or bool(self.repositories)
        )

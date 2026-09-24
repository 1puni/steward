"""Config schema and loader coverage."""

from __future__ import annotations

from steward_harness.deploy.config import SystemdReleaseConfig

from pathlib import Path

import pytest
from pydantic import ValidationError

from steward_harness.config.loader import load_config
from steward_harness.config.schema import (
    CommandSpec,
    FilesystemProbeSpec,
    PipelineConfig,
    ProviderConfig,
    RepairMode,
    RepositoryConfig,
    StewardConfig,
)
from steward_harness.runtime.contracts import ResolvedModel, resolve_model

_PROBE = {"type": "command", "command": {"argv": ["true"]}}
_REMOTE = "https://example.invalid/repository.git"


def test_anthropic_router_endpoints_are_validated() -> None:
    assert ProviderConfig().glm_anthropic_base_url == "https://api.z.ai/api/anthropic"

    routed = ProviderConfig(
        glm_anthropic_base_url="https://api.cheaperinference.com",
        claude_anthropic_base_url="https://api.cheaperinference.com",
        claude_credential_path="/etc/steward/cheaperinference-token",
    )
    assert routed.glm_anthropic_base_url == "https://api.cheaperinference.com"

    with pytest.raises(ValidationError, match="https"):
        ProviderConfig(glm_anthropic_base_url="http://api.cheaperinference.com")
    with pytest.raises(ValidationError, match="https"):
        ProviderConfig(
            claude_anthropic_base_url="http://api.cheaperinference.com",
            claude_credential_path="/etc/steward/cheaperinference-token",
        )
    with pytest.raises(ValidationError, match="together"):
        ProviderConfig(claude_anthropic_base_url="https://api.cheaperinference.com")
    with pytest.raises(ValidationError, match="bounded absolute"):
        ProviderConfig(
            claude_anthropic_base_url="https://api.cheaperinference.com",
            claude_credential_path="relative-token",
        )


def test_command_spec_rejects_shell_launchers() -> None:
    with pytest.raises(ValidationError):
        CommandSpec(argv=("bash", "-c", "echo hi"))


def test_filesystem_probe_requires_absolute_path() -> None:
    with pytest.raises(ValidationError):
        FilesystemProbeSpec(type="filesystem", path="relative/dir")


def test_filesystem_probe_rejects_path_traversal_pattern() -> None:
    with pytest.raises(ValidationError):
        FilesystemProbeSpec(type="filesystem", path="/tmp", pattern="../secret")


def test_pipeline_must_reference_known_repository() -> None:
    with pytest.raises(ValidationError):
        StewardConfig.model_validate(
            {
                "identity": {"name": "t", "slug": "t"},
                "pipelines": {
                    "p": {"repository": "ghost", "probe": _PROBE}
                },
            }
        )


def test_pipeline_name_is_its_bounded_durable_identity() -> None:
    with pytest.raises(ValidationError, match="pipeline name.*simple slug"):
        StewardConfig.model_validate(
            {
                "identity": {"name": "t", "slug": "t"},
                "repositories": {
                    "r": {"path": "/tmp/r", "remote_url": _REMOTE}
                },
                "pipelines": {"../legacy-key": {"repository": "r", "probe": _PROBE}},
            }
        )


def test_a_model_override_may_carry_its_effort_and_a_bare_name_still_may_not(
) -> None:
    """The bare string is the whole of backward compatibility.

    Every configuration written before effort was declarable names a model and
    nothing else, and it has to keep meaning exactly that — the model is
    replaced, the profile's built-in effort stands.
    """
    config = ProviderConfig.model_validate(
        {
            "models": {
                "claude": {"deep": "claude-opus-4-6"},
                "glm": {
                    "deep": {"model": "glm-5.3", "effort": "high"},
                    "balanced": {"model": "glm-5.3"},
                },
            }
        }
    )
    assert config.models["claude"]["deep"] == "claude-opus-4-6"
    assert resolve_model("claude", "deep", config.models) == ResolvedModel(
        "claude", "deep", "claude-opus-4-6", "max"
    )
    assert resolve_model("glm", "deep", config.models) == ResolvedModel(
        "glm", "deep", "glm-5.3", "high"
    )
    # Declared with no effort: the provider decides, which is the one thing a
    # gateway-backed family could not previously be told.
    assert resolve_model("glm", "balanced", config.models) == ResolvedModel(
        "glm", "balanced", "glm-5.3", None
    )
    with pytest.raises(ValidationError):
        ProviderConfig.model_validate(
            {"models": {"glm": {"deep": {"model": "glm-5.3", "effort": "colossal"}}}}
        )


def test_old_native_policy_rhythm_schema_is_rejected() -> None:
    """The native policies are seeded into the world, not resolved from config.

    Configuration that declared rhythms was the second source of truth the
    `enabled_override` / `schedule_overridden` columns existed to arbitrate.
    Both are gone, so the key is refused rather than quietly ignored.
    """
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        StewardConfig.model_validate(
            {
                "identity": {"name": "t", "slug": "t"},
                "world": {"root": "/tmp/world"},
                "rhythms": {
                    "light": {
                        "policy": "light",
                        "schedule": {"type": "settled", "delay_seconds": 60},
                    }
                },
            }
        )


@pytest.mark.parametrize("field", ["execution", "freshness_seconds"])
def test_pipeline_rejects_fields_without_runtime_behavior(field: str) -> None:
    document: dict[str, object] = {
        "repository": "r",
        "probe": _PROBE,
    }
    document[field] = "systemd" if field == "execution" else 60
    with pytest.raises(ValidationError, match=field):
        PipelineConfig.model_validate(document)


def test_pipeline_repair_modes_match_runtime_authority() -> None:
    assert PipelineConfig.model_validate(
        {"repository": "r", "probe": _PROBE}
    ).repair_mode is RepairMode.AUTONOMOUS
    assert {mode.value for mode in RepairMode} == {"monitor_only", "autonomous"}
    with pytest.raises(ValidationError, match="validated"):
        PipelineConfig.model_validate(
            {
                "repository": "r",
                "repair_mode": "validated",
                "probe": _PROBE,
            }
        )


def test_pipeline_requires_an_observable_probe() -> None:
    with pytest.raises(ValidationError, match="probe"):
        PipelineConfig(repository="r")  # type: ignore[call-arg]






def test_desk_presence_is_the_only_bridge_state() -> None:
    disabled = StewardConfig.model_validate(
        {"identity": {"name": "t", "slug": "t"}}
    )
    configured = StewardConfig.model_validate(
        {"identity": {"name": "t", "slug": "t"}, "desk": {}}
    )

    assert disabled.desk is None
    assert configured.desk is not None
    with pytest.raises(ValidationError, match="enabled"):
        StewardConfig.model_validate(
            {
                "identity": {"name": "t", "slug": "t"},
                "desk": {"enabled": False},
            }
        )


def test_world_presence_is_the_only_configuration_state() -> None:
    disabled = StewardConfig.model_validate(
        {"identity": {"name": "t", "slug": "t"}}
    )
    configured = StewardConfig.model_validate(
        {"identity": {"name": "t", "slug": "t"}, "world": {}}
    )

    assert disabled.world is None
    assert configured.world is not None
    assert configured.world.root == "/var/lib/steward/world"
    with pytest.raises(ValidationError, match="enabled"):
        StewardConfig.model_validate(
            {
                "identity": {"name": "t", "slug": "t"},
                "world": {"enabled": False},
            }
        )

    with pytest.raises(ValidationError, match="reconcile"):
        RepositoryConfig.model_validate(
            {"path": "/tmp/r", "remote_url": _REMOTE, "reconcile": False}
        )


def test_load_config_reads_example_yaml(repo_root: Path) -> None:
    config = load_config(repo_root / "config" / "steward.example.yaml")
    assert config.identity.slug == "example"
    assert "app" in config.repositories
    assert config.pipelines["app-build"].probe.type == "command"


@pytest.fixture()
def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def test_health_bind_and_ack_defaults() -> None:
    config = StewardConfig.model_validate({"identity": {"name": "t", "slug": "t"}})
    assert config.controller.health_bind is None
    assert config.provider.family_order == ("codex", "claude", "glm")
    assert config.world is None
    assert config.execution.user is None
    assert config.telegram is None


def test_telegram_presence_is_the_only_transport_state() -> None:
    with pytest.raises(ValidationError, match="chat_id"):
        StewardConfig.model_validate(
            {
                "identity": {"name": "t", "slug": "t"},
                "telegram": {
                    "allowed_users": [1],
                    "agent_actions": ["pin_reply"],
                },
            }
        )

    config = StewardConfig.model_validate(
        {
            "identity": {"name": "t", "slug": "t"},
            "telegram": {
                "chat_id": -100123,
                "allowed_users": [1],
                "agent_actions": ["pin_reply", "pin_message"],
            },
        }
    )
    assert config.telegram is not None
    assert config.telegram.agent_actions == ("pin_reply", "pin_message")
    with pytest.raises(ValidationError, match="enabled"):
        StewardConfig.model_validate(
            {
                "identity": {"name": "t", "slug": "t"},
                "telegram": {
                    "enabled": False,
                    "chat_id": 1,
                    "allowed_users": [1],
                },
            }
        )


@pytest.mark.parametrize(
    "telegram",
    [
        {"chat_id": 1},
        {"chat_id": 1, "allowed_users": []},
    ],
)
def test_telegram_requires_an_explicit_nonempty_user_allowlist(
    telegram: dict,
) -> None:
    with pytest.raises(ValidationError, match="allowed_users"):
        StewardConfig.model_validate(
            {
                "identity": {"name": "t", "slug": "t"},
                "telegram": telegram,
            }
        )


def test_telegram_topic_ids_have_one_configured_identity() -> None:
    with pytest.raises(ValidationError, match="topic IDs must be unique"):
        StewardConfig.model_validate(
            {
                "identity": {"name": "t", "slug": "t"},
                "telegram": {
                    "chat_id": 1,
                    "allowed_users": [1],
                    "topics": {"builds": 8, "incidents": 8},
                },
            }
        )


def test_untrusted_execution_rejects_root_and_unbounded_paths() -> None:
    with pytest.raises(ValidationError, match="must not be root"):
        StewardConfig.model_validate(
            {
                "identity": {"name": "t", "slug": "t"},
                "execution": {"user": "root"},
            }
        )

    with pytest.raises(ValidationError, match="cannot be inherited"):
        StewardConfig.model_validate(
            {
                "identity": {"name": "t", "slug": "t"},
                "execution": {
                    "user": "steward",
                    "inherited_environment": ["PATH", "GITHUB_TOKEN"],
                },
            }
        )
    with pytest.raises(ValidationError, match="bounded absolute path"):
        StewardConfig.model_validate(
            {
                "identity": {"name": "t", "slug": "t"},
                "execution": {"user": "steward", "home": "/"},
            }
        )


@pytest.mark.parametrize(
    "document",
    [
        {"provider": {"state_db": "state.db"}},
        {"provider": {"workdir": "work"}},
        {
            "repositories": {
                "app": {"path": "repository", "remote_url": _REMOTE}
            }
        },
        {"world": {"root": "world"}},
        {"world": {"lock_dir": "locks"}},
        {
            "telegram": {
                "chat_id": 1,
                "allowed_users": [1],
                "token_path": "token",
            }
        },
        {"desk": {"inbox_dir": "inbox"}},
    ],
)
def test_authority_bearing_paths_must_be_absolute(document: dict) -> None:
    with pytest.raises(ValidationError, match="bounded absolute path"):
        StewardConfig.model_validate(
            {"identity": {"name": "t", "slug": "t"}, **document}
        )


@pytest.mark.parametrize(
    ("configured", "match"),
    [
        ({"provider": {"state_db": "/home/steward/state.db"}}, "provider.state_db"),
        (
            {
                "telegram": {
                    "chat_id": 1,
                    "allowed_users": [1],
                    "token_path": "/srv/app/telegram-token",
                    "inbound_media_dir": "/var/lib/steward/inbound-media",
                }
            },
            "telegram.token_path",
        ),
        (
            {
                "telegram": {
                    "chat_id": 1,
                    "allowed_users": [1],
                    "inbound_media_dir": "/home/steward/media",
                }
            },
            "telegram.inbound_media_dir",
        ),
    ],
)
def test_controller_files_must_be_outside_model_writable_roots(
    configured: dict, match: str
) -> None:
    document = {
        "identity": {"name": "t", "slug": "t"},
        "execution": {"user": "steward", "home": "/home/steward"},
        "repositories": {
            "app": {"path": "/srv/app", "remote_url": _REMOTE}
        },
        **configured,
    }

    with pytest.raises(ValidationError, match=match):
        StewardConfig.model_validate(document)




def test_split_identity_telegram_requires_explicit_media_spool() -> None:
    with pytest.raises(ValidationError, match="explicit inbound_media_dir"):
        StewardConfig.model_validate(
            {
                "identity": {"name": "t", "slug": "t"},
                "execution": {"user": "steward", "home": "/home/steward"},
                "telegram": {"chat_id": 1, "allowed_users": [1]},
            }
        )




def test_repository_policy_requires_execution_boundary() -> None:
    config = StewardConfig.model_validate(
        {
            "identity": {"name": "t", "slug": "t"},
            "repositories": {
                "app": {
                    "path": "/srv/app",
                    "remote_url": _REMOTE,
                }
            },
        }
    )
    assert config.requires_execution_boundary is True


def test_repository_remote_is_explicit_controller_policy() -> None:
    with pytest.raises(ValidationError, match="remote_url"):
        RepositoryConfig(path="/srv/app")

    with pytest.raises(ValidationError, match="must not embed credentials"):
        RepositoryConfig(
            path="/srv/app",
            remote_url="https://token@example.invalid/repository.git",
        )

    with pytest.raises(ValidationError, match="bounded absolute path"):
        RepositoryConfig(path="/srv/app", remote_url="/")


def test_split_identity_rejects_local_git_remote() -> None:
    with pytest.raises(ValidationError, match="local remote_url is not allowed"):
        StewardConfig.model_validate(
            {
                "identity": {"name": "t", "slug": "t"},
                "execution": {"user": "steward", "home": "/home/steward"},
                "repositories": {
                    "app": {"path": "/srv/app", "remote_url": "/srv/origin.git"}
                },
            }
        )


def test_explicit_execution_identity_requires_its_boundary() -> None:
    config = StewardConfig.model_validate(
        {
            "identity": {"name": "t", "slug": "t"},
            "execution": {"user": "steward", "home": "/home/steward"},
        }
    )

    assert config.requires_execution_boundary is True


def test_repository_and_telegram_authority_require_execution_boundary() -> None:
    push = StewardConfig.model_validate(
        {
            "identity": {"name": "t", "slug": "t"},
            "repositories": {
                "app": {"path": "/srv/app", "remote_url": _REMOTE}
            },
        }
    )
    telegram = StewardConfig.model_validate(
        {
            "identity": {"name": "t", "slug": "t"},
            "telegram": {"chat_id": 1, "allowed_users": [1]},
        }
    )
    assert push.requires_execution_boundary is True
    assert telegram.requires_execution_boundary is True


def test_provider_fallback_order_is_explicit_and_unique() -> None:
    config = StewardConfig.model_validate(
        {
            "identity": {"name": "t", "slug": "t"},
            "provider": {
                "default_family": "codex",
                "fallback_families": ["claude", "glm"],
            },
        }
    )
    assert config.provider.family_order == ("codex", "claude", "glm")

    with pytest.raises(ValidationError, match="unique"):
        StewardConfig.model_validate(
            {
                "identity": {"name": "t", "slug": "t"},
                "provider": {
                    "default_family": "codex",
                    "fallback_families": ["claude", "codex"],
                },
            }
        )


def test_provider_ids_are_open_but_profiles_and_id_shape_are_validated() -> None:
    configured = StewardConfig.model_validate(
        {
            "identity": {"name": "t", "slug": "t"},
            "provider": {
                "default_family": "future-provider",
                "models": {"future-provider": {"balanced": "model"}},
            },
        }
    )
    assert configured.provider.default_family == "future-provider"

    with pytest.raises(ValidationError, match="invalid provider ID"):
        StewardConfig.model_validate(
            {
                "identity": {"name": "t", "slug": "t"},
                "provider": {"default_family": "Future Provider"},
            }
        )
    with pytest.raises(ValidationError, match="unsupported codex model profiles"):
        StewardConfig.model_validate(
            {
                "identity": {"name": "t", "slug": "t"},
                "provider": {
                    "models": {"codex": {"imaginary": "model"}},
                },
            }
        )






@pytest.mark.parametrize("shared", ["path", "target"])
def test_concurrent_repository_aliases_require_distinct_ownership(shared):
    repositories = {
        "a": {"path": "/srv/a", "remote_url": "git@example.com:org/a.git"},
        "b": {"path": "/srv/b", "remote_url": "git@example.com:org/b.git"},
    }
    field = "path" if shared == "path" else "remote_url"
    repositories["b"][field] = repositories["a"][field]
    with pytest.raises(ValidationError, match="share a"):
        StewardConfig.model_validate({"identity": {"name": "test", "slug": "test"}, "repositories": repositories})


def test_target_and_procedure_authority_cannot_be_authored_in_product_tree():
    document = dict(identity=dict(name="test", slug="test"),
                    repositories={"app": dict(path="/srv/app", remote_url=_REMOTE)})
    for extra in (
        {"targets": {"production": dict(ref="repositories/app/main", driver="/srv/app/driver")}},
        {"procedures": {"review": dict(instructions="/srv/app/review.md", provider="codex", model=dict(model="review-model"))}},
    ):
        with pytest.raises(ValidationError, match="authority must be outside"):
            StewardConfig.model_validate(document | extra)
    with pytest.raises(ValidationError, match="unknown procedure"):
        StewardConfig.model_validate(document | {"rhythms": {"weekly": dict(owner=None, schedule=604800, procedure="missing", input="repositories/app/main")}})
    with pytest.raises(ValidationError, match="read-only"):
        StewardConfig.model_validate(document | {"targets": {"production": dict(ref="repositories/app/main", driver="/opt/driver", requires=["missing"])}})
    with pytest.raises(ValidationError, match="Extra inputs"):
        RepositoryConfig(path="/srv/app", remote_url=_REMOTE, deploy={"service": "app"})


@pytest.mark.parametrize("authority", ["instructions", "driver"])
def test_authority_symlink_into_product_tree_is_rejected(tmp_path, authority):
    root = tmp_path / "product"
    root.mkdir()
    target = root / "agent-controlled"
    target.write_text("agent controlled")
    link = tmp_path / "installed-authority"
    link.symlink_to(target)
    extra = ({"procedures": {"review": dict(instructions=str(link), provider="codex", model=dict(model="review"))}}
             if authority == "instructions" else
             {"targets": {"production": dict(ref="repositories/app/main", driver=str(link))}})
    with pytest.raises(ValidationError, match="authority must be outside"):
        StewardConfig.model_validate(dict(identity=dict(name="test", slug="test"),
                                         repositories={"app": dict(path=str(root), remote_url=_REMOTE)}) | extra)


def test_rhythm_output_owner_is_explicit_and_uses_configured_transport():
    base=dict(identity=dict(name="test", slug="test"),
              repositories={"app": dict(path="/srv/app", remote_url=_REMOTE)},
              procedures={"review": dict(instructions="/etc/review.md", provider="codex", model=dict(model="review"))})
    rhythm=dict(schedule=100, procedure="review", input="repositories/app/main")
    configured = base | {"rhythms": {"review": rhythm | {"owner": None, "workdir": "/srv/org"}}}
    assert StewardConfig.model_validate(configured).rhythms["review"].workdir == "/srv/org"
    with pytest.raises(ValidationError, match="absolute"):
        StewardConfig.model_validate(base | {"rhythms": {"review": rhythm | {"owner": None, "workdir": "relative"}}})
    with pytest.raises(ValidationError, match="read-only"):
        StewardConfig.model_validate(configured | {"procedures": {"review": base["procedures"]["review"] | {"access": "workspace-write"}}})
    with pytest.raises(ValidationError, match="owner"):
        StewardConfig.model_validate(base | {"rhythms": {"review": rhythm}})
    assert StewardConfig.model_validate(base | {"rhythms": {"review": rhythm | {"owner": None}}})
    for owner in ("telegram:999", "desk:steward", "task:arbitrary"):
        with pytest.raises(ValidationError, match="rhythm owner"):
            StewardConfig.model_validate(base | {"rhythms": {"review": rhythm | {"owner": owner}}})
    configured=base | {"telegram":dict(chat_id=123, allowed_users=[7], topics={"steward":0}),
                      "rhythms": {"review": rhythm | {"owner":"telegram:0"}}}
    assert StewardConfig.model_validate(configured)

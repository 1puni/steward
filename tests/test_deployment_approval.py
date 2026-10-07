"""Contributors can work; only authenticated operators release an exact revision."""
import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from steward_harness.config.schema import StewardConfig
from steward_harness.deployment_approval import DeploymentApprovals
from steward_harness.slack.commands import slack_command
from steward_harness.slack.service import SlackService
from steward_harness.state import StateDatabase, TaskSpec
from steward_harness.targets import Targets, Observation
from test_daemon import _status_commands
from test_git_tasks import harness
from test_slack import config, event, answer_one, transport
from test_slack_daemon import config_for, ROUTE
from test_task_no_changes import InvestigationAdapter
from test_task_runner_kernel import _git, _repository
from test_world_durability import runtime
from test_world_turn_checkpoint import _episodes

ACTOR = "slack:T123:U123"
SHA = "a" * 40


def contributor_config(tmp_path):
    data = config_for(tmp_path).model_dump(mode="json")
    data["slack"]["users"]["U456"] = "contributor"
    data["deployment_operators"] = [ACTOR, "telegram:7"]
    data["targets"] = {"production": dict(ref="repositories/app/main", driver="/opt/driver")}
    data["repositories"] = {"app": dict(path=str(tmp_path / "repo"),
        remote_url="https://example.invalid/app.git", publish_requires_approval=False)}
    return StewardConfig.model_validate(data)


def test_contributor_converses_with_attribution_and_can_request_work(transport):
    cfg = config(users={"U123": "operator", "U456": "contributor"}, user_names={"U456": "Alice"})
    service = transport.make(cfg)
    service.ingest(event(user="U456", text="Create the architecture document"))
    answer_one(service)
    text = transport.turn.call_args.args[3]
    assert json.loads(text.splitlines()[0].removeprefix("Slack sender: ")) == dict(
        transport="slack", team="T123", user="U456", role="contributor", name="Alice")
    assert text.endswith("\n\nCreate the architecture document")
    service.ingest(event(2, user="U456", text="!task note task1 add examples"))
    answer_one(service)
    transport.command.assert_called_once_with("task", "note task1 add examples",
                                              "T123:C123:1234567890.000002", "U456")


def test_queued_contributor_demotion_is_enforced(transport):
    service = transport.make(config(users={"U123": "operator", "U456": "contributor"}))
    service.ingest(event(user="U456", text="do work"))
    service.config.users["U456"] = "observer"
    answer_one(service)
    transport.turn.assert_not_called()


def test_contributor_document_and_task_cross_real_world_acceptance_once(tmp_path):
    state, checkpoint, conversations, cognition = runtime(tmp_path)
    cfg = config(users={"U123": "operator", "U456": "contributor"}, user_names={"U456": "Alice"})
    sent = []
    def turn(event_id, route, user, text):
        return conversations.run_turn(transport="slack", transport_key=route,
            source_event_key=event_id, operator_id=user, text=text).transport_reply
    def send(method, **kwargs):
        sent.append(kwargs)
        return dict(channel=kwargs["channel"], ts="1234567891.000001")
    service = SlackService(cfg, state=state, turn_handler=turn, command_handler=None,
                           api=SimpleNamespace(call=send))
    incoming = event(user="U456", text="Write the handoff document and request implementation")
    service.ingest(incoming)
    answer_one(service)
    assert (checkpoint.world.root / "decision.md").read_text().startswith("The private consumer")
    [task] = state.tasks.all()
    assert task.owner == "slack:T123:C123:1234567890.000001"
    assert task.status.value == "queued"
    [episode] = _episodes(checkpoint.world.root)
    assert '"user": "U456"' in episode["user"] and '"name": "Alice"' in episode["user"]
    assert len(sent) == 1 and sent[0]["thread_ts"] == "1234567890.000001"
    service.ingest(incoming)
    assert not service.inbox.pending()
    assert cognition.calls == 1 and len(state.tasks.all()) == 1


@pytest.mark.parametrize("change", ["no_operator", "contributor_approver", "implicit_publication"])
def test_contributor_configuration_requires_explicit_deployment_boundary(tmp_path, change):
    data = contributor_config(tmp_path).model_dump(mode="json")
    if change == "no_operator":
        data["deployment_operators"] = []
    elif change == "contributor_approver":
        data["deployment_operators"] = ["slack:T123:U456"]
    else:
        del data["repositories"]["app"]["publish_requires_approval"]
    with pytest.raises(ValidationError):
        StewardConfig.model_validate(data)


def test_approval_survives_restart_but_never_changes_scope_or_revision(tmp_path):
    state = StateDatabase(tmp_path / "state.db")
    approvals = DeploymentApprovals(state, (ACTOR,))
    policy = {"ref": "repositories/app/main", "driver": "/opt/driver"}
    assert not approvals.require("target", "production", SHA, policy)
    assert not approvals.require("target", "production", SHA, policy)
    assert len(state.pending_result_receipts()) == 1
    with pytest.raises(ValueError, match="only a configured"):
        approvals.approve("target", "production", SHA, policy, "slack:T123:U456")
    approvals.approve("target", "production", SHA, policy, ACTOR)
    recovered = DeploymentApprovals(StateDatabase(state.path), (ACTOR,))
    assert recovered.require("target", "production", SHA, policy)
    assert not recovered.require("target", "production", "b" * 40, policy)
    assert not recovered.require("target", "other", SHA, policy)
    assert not recovered.require("target", "production", SHA, policy | {"driver": "/other"})
    assert not DeploymentApprovals(state, ("telegram:7",)).require("target", "production", SHA, policy)
    with pytest.raises(ValueError, match="no pending"):
        recovered.approve("publication", "app", SHA, policy, ACTOR)
    with pytest.raises(ValueError, match="full 40"):
        recovered.approve("target", "production", "main", policy, ACTOR)


def test_automatic_target_apply_and_force_both_require_exact_approval(tmp_path, monkeypatch):
    cfg = contributor_config(tmp_path)
    state = StateDatabase(cfg.provider.state_db)
    desired = [SHA]
    monkeypatch.setattr("steward_harness.targets.resolve_input", lambda *_: ("app", desired[0], desired[0]))
    targets = Targets(cfg, state, {}, SimpleNamespace(require=lambda *_: ""))
    calls = []
    def driver(name, operation, repository, revision):
        calls.append(operation)
        return Observation(revision=None, ready=False) if operation == "observe" else None
    monkeypatch.setattr(targets, "call", driver)
    assert "awaiting operator approval" in targets.advance("production")
    assert "awaiting operator approval" in targets.advance("production", force=True)
    assert "apply" not in calls
    approvals = DeploymentApprovals(state, cfg.deployment_operators)
    approvals.approve("target", "production", SHA, cfg.targets["production"].model_dump(mode="json"), ACTOR)
    targets.advance("production")
    assert calls.count("apply") == 1
    desired[0] = "b" * 40
    assert "awaiting operator approval" in targets.advance("production")
    assert calls.count("apply") == 1


def test_slack_and_telegram_approval_use_authenticated_operator_not_message_text(tmp_path):
    commands = _status_commands(tmp_path)
    cfg = contributor_config(tmp_path)
    commands.config = cfg
    approvals = DeploymentApprovals(commands.state, cfg.deployment_operators)
    policy = cfg.targets["production"].model_dump(mode="json")
    approvals.require("target", "production", SHA, policy)
    arg = "approve target production " + SHA
    assert "refused" in slack_command(commands, "git", arg, ROUTE, "U456")
    assert "refused" in slack_command(commands, "git", arg, ROUTE.replace("T123", "T999"), "U123")
    assert "refused" in commands._git(arg)
    assert "refused" in commands("git", arg, 999, 42, 7)
    assert "refused" in commands("git", arg, 1, 42, 8)
    assert "Approved" in slack_command(commands, "git", arg, ROUTE, "U123")
    other = "b" * 40
    approvals.require("target", "production", other, policy)
    assert "Approved" in commands("git", "approve target production " + other, 1, 42, 7)
    assert approvals.require("target", "production", other, policy)


@pytest.mark.parametrize("move_base", [False, True])
def test_push_triggered_deployment_waits_for_tested_commit_approval(tmp_path, move_base):
    bare, clone = _repository(tmp_path)
    state, runner, _, reconciler = harness(tmp_path / "state", bare, clone,
                                            InvestigationAdapter(edit_first=True))
    repository = reconciler.repositories["app"].model_copy(update={"publish_requires_approval": True})
    reconciler.repositories["app"] = repository
    reconciler.deployment_approvals = DeploymentApprovals(state, (ACTOR,))
    task, _ = state.tasks.create(TaskSpec("app", "Write document", "Create a document"))
    runner.prepare(task)
    before = _git("rev-parse", "main", cwd=bare)
    assert reconciler.publish_repository("app") is None
    assert _git("rev-parse", "main", cwd=bare) == before
    [pending] = reconciler.deployment_approvals.pending()
    revision = pending["revision"]
    assert revision != before
    assert revision in reconciler.transports["app"]._run("for-each-ref", "--format=%(objectname)", "refs/steward/candidates/")
    # Rebuilding the candidate must preserve its reviewed commit identity.
    assert reconciler.publish_repository("app") is None
    assert len(reconciler.deployment_approvals.pending()) == 1
    reconciler.deployment_approvals.approve("publication", "app", revision,
        repository.model_dump(mode="json"), ACTOR)
    if move_base:
        (clone / "other.txt").write_text("Concurrent accepted work\n")
        _git("add", "other.txt", cwd=clone)
        _git("commit", "-qm", "Move the deployment base", cwd=clone)
        _git("push", "-q", "origin", "main", cwd=clone)
        moved = _git("rev-parse", "main", cwd=bare)
        assert reconciler.publish_repository("app") is None
        assert _git("rev-parse", "main", cwd=bare) == moved
        [pending] = reconciler.deployment_approvals.pending()
        assert pending["revision"] != revision
        revision = pending["revision"]
        reconciler.deployment_approvals.approve("publication", "app", revision,
            repository.model_dump(mode="json"), ACTOR)
    assert reconciler.publish_repository("app") == task
    assert _git("rev-parse", "main", cwd=bare) == revision
    assert _git("show", "main:result.txt", cwd=bare) == "completed"

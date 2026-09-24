"""The admitted charter is read from the accepted task, without a history exporter."""
from pathlib import Path
from test_git_tasks import harness
from test_task_runner_kernel import _repository
from test_task_no_changes import InvestigationAdapter
from test_rewrite_convergence import setup_procedures


def test_task_keeps_the_admitted_charter_when_policy_changes(tmp_path):
    bare, clone = _repository(tmp_path)
    adapter = InvestigationAdapter("continue")
    state, runner, _, _ = harness(tmp_path / "state", bare, clone, adapter)
    config, procedures = setup_procedures(tmp_path, state, runner)
    candidate = runner.transports["app"].fetch()
    instructions = Path(config.procedures["security-one"].instructions)
    accepted = instructions.read_text()
    task = procedures.request("security-one", "app", candidate, candidate, event="test:charter")
    instructions.write_text("Different policy after admission.")
    runner.prepare(task)
    runner.prepare(task)
    assert len(adapter.requests) == 2
    request = adapter.requests[-1]
    assert request.prompt.count(accepted) == 1
    assert "Different policy after admission" not in request.prompt
    assert "git log" in request.prompt and "git show" in request.prompt

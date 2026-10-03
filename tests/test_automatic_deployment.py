"""The daemon advances configured targets independently of accepted publication."""
from pathlib import Path
import json
import sys
import time

import pytest

from steward_harness.config.schema import StewardConfig, UntrustedExecutionConfig
from steward_harness.daemon import StewardDaemon
from steward_harness.runtime.execution import UntrustedExecutionBroker
from steward_harness.state import StateDatabase, TaskSpec
from test_task_no_changes import InvestigationAdapter
from task_tool_fixtures import TaskAdapter
from test_task_runner_kernel import _git, _repository


@pytest.mark.parametrize("external_ready,wrong_revision", [(True, False), (False, False), (True, True)])
def test_daemon_publishes_task_and_independently_observes_target(tmp_path, external_ready, wrong_revision):
    remote, clone = _repository(tmp_path)
    source = _git("rev-parse", "main", cwd=remote)
    applied = tmp_path / "applied"
    destination = tmp_path / "external.json"
    driver = tmp_path / "installed-driver"
    driver.write_text(f"#!{sys.executable}\n" + f'''import json,sys
from pathlib import Path
request=json.load(sys.stdin)
path=Path({str(destination)!r})
if sys.argv[1]=='observe':
    print(path.read_text() if path.exists() else json.dumps(dict(revision=None,ready=False)))
else:
    path.write_text(json.dumps(dict(revision={source!r} if {wrong_revision!r} else request['revision'],ready={external_ready!r})))
    Path({str(applied)!r}).write_text(request['revision'])
''')
    driver.chmod(0o755)
    config = StewardConfig.model_validate(dict(
        identity=dict(name="test", slug="test"),
        provider=dict(state_db=str(tmp_path / "state.db"), workdir=str(tmp_path / "work"),
                      default_family="claude", fallback_families=[]),
        repositories={"app": dict(path=str(clone), remote_url=str(remote))},
        targets={"production": dict(ref="repositories/app/main", driver=str(driver))},
    ))
    Path(config.provider.workdir).mkdir()
    state = StateDatabase(config.provider.state_db)
    task, _ = state.tasks.create(TaskSpec("app", "Investigate", "Find the consumer"))
    daemon = StewardDaemon(config, tmp_path / "config.yaml", adapters={"claude": TaskAdapter(InvestigationAdapter())},
                           broker=UntrustedExecutionBroker(UntrustedExecutionConfig()))
    with daemon._daemon_lease():
        step = daemon._start_owned()
        deadline = time.monotonic() + 20
        try:
            while True:
                step()
                current = StateDatabase(config.provider.state_db)
                current.tasks.transports = daemon._targets.transports
                landed = current.tasks.get(task).landed
                if landed and applied.exists() and applied.read_text() == landed:
                    break
                assert time.monotonic() < deadline, "daemon did not advance publication and target"
                time.sleep(.02)
            assert _git("rev-parse", "main", cwd=remote) == landed
            outcome = daemon._targets.advance("production")
            assert ("satisfied" in outcome) is (external_ready and not wrong_revision)
            assert current.tasks.get(task).status.value == "done"
            assert json.loads(destination.read_text())["revision"] == (source if wrong_revision else landed)
        finally:
            daemon.stop()

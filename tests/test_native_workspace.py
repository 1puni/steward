"""Native files belong to candidate Git worlds; launch credentials do not."""

from dataclasses import replace
from pathlib import Path

import pytest

from steward_harness.config.schema import UntrustedExecutionConfig
from steward_harness.runtime.contracts import RuntimeExecutionError, RuntimeRequest, resolve_model
from steward_harness.runtime.execution import UntrustedExecutionBroker
from steward_harness.runtime.native_workspace import native_workspace

SESSION = '11111111-1111-4111-8111-111111111111'


@pytest.fixture
def setup(tmp_path):
    home = tmp_path / 'private'
    home.mkdir()
    (home / 'auth.json').write_text('provider credential')
    (home / 'config.toml').write_text('approved config')
    (home / 'skills').mkdir()
    (home / 'skills/workflow.md').write_text('approved native workflow')
    (home / 'state_5.sqlite').write_text('private runtime state')
    world = tmp_path / 'world'
    world.mkdir()
    request = RuntimeRequest(
        execution_id='native', resolved=resolve_model('codex', 'fast'),
        provider_session_id=None, prompt='work', cwd=world,
        timeout_seconds=10, sandbox_mode='workspace-write',
    )
    return UntrustedExecutionBroker(UntrustedExecutionConfig()), request, home


def mapped(setup, **overrides):
    broker, request, home = setup
    return native_workspace(
        broker, replace(request, **overrides), home,
        mappings={'sessions': 'artefacts/codex/sessions', 'memories': 'memories/codex'},
        resume_pattern='artefacts/codex/sessions/**/rollout-*{session}.jsonl',
    )


def test_original_records_write_directly_and_survive_private_cleanup(setup):
    _, request, home = setup
    with mapped(setup) as native:
        assert native.home != home
        assert (native.home / 'auth.json').read_text() == 'provider credential'
        assert (native.home / 'skills/workflow.md').read_text() == 'approved native workflow'
        assert not (native.home / 'state_5.sqlite').exists()
        record = native.home / 'sessions' / f'rollout-{SESSION}.jsonl'
        record.write_text('native original\n')
        assert (request.cwd / 'artefacts/codex/sessions' / record.name).read_text() == 'native original\n'
        (native.home / 'memories/MEMORY.md').write_text('native interpretation')
    assert not native.home.exists()
    assert (home / 'auth.json').read_text() == 'provider credential'
    assert not (home / 'sessions').exists()
    assert not list(request.cwd.rglob('auth.json'))
    assert (request.cwd / 'memories/codex/MEMORY.md').read_text() == 'native interpretation'
    with mapped(setup, provider_session_id=SESSION) as resumed:
        assert resumed.resume == str(request.cwd / 'artefacts/codex/sessions' / record.name)
        assert resumed.home != native.home


def test_two_active_workspaces_never_retarget_each_others_native_writes(setup, tmp_path):
    other = tmp_path / 'other'
    other.mkdir()
    with mapped(setup) as first, mapped(setup, cwd=other) as second:
        (first.home / 'sessions/first.jsonl').write_text('first')
        (second.home / 'sessions/second.jsonl').write_text('second')
        assert not (first.home / 'sessions/second.jsonl').exists()
        assert not (second.home / 'sessions/first.jsonl').exists()
    assert (setup[1].cwd / 'artefacts/codex/sessions/first.jsonl').read_text() == 'first'
    assert (other / 'artefacts/codex/sessions/second.jsonl').read_text() == 'second'


@pytest.mark.parametrize('component', ['artefacts', 'artefacts/codex', 'artefacts/codex/sessions', 'memories'])
def test_native_mapping_rejects_symlinked_candidate_directories(setup, tmp_path, component):
    _, request, home = setup
    outside = tmp_path / 'outside'
    outside.mkdir()
    link = request.cwd / component
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(RuntimeExecutionError, match='could not be prepared') as failure:
        with mapped(setup):
            pytest.fail('must not launch')
    assert '[Errno' in str(failure.value)
    assert not list(outside.iterdir())
    assert not list(home.glob('.steward-launch-*'))


def test_missing_old_private_session_is_not_silently_restarted(setup):
    with pytest.raises(RuntimeExecutionError, match='native session absent or ambiguous') as failure:
        with mapped(setup, provider_session_id=SESSION):
            pytest.fail('must not launch')
    assert failure.value.session_id == SESSION


def test_readonly_native_calls_keep_records_outside_the_candidate(setup):
    with mapped(setup, sandbox_mode='read-only', provider_session_id=SESSION) as native:
        assert native.home == setup[2]
        assert native.resume == SESSION
    assert list(setup[1].cwd.iterdir()) == []


def test_private_mapping_cleanup_does_not_erase_partial_work_on_failure(setup):
    with pytest.raises(RuntimeError, match='provider failure'):
        with mapped(setup) as native:
            (native.home / 'sessions/partial.jsonl').write_text('partial native record')
            raise RuntimeError('provider failure')
    assert not native.home.exists()
    assert (setup[1].cwd / 'artefacts/codex/sessions/partial.jsonl').exists()


@pytest.mark.parametrize("name", ["commit", "git-reconciler"])
def test_bundled_skill_is_available_without_mutating_native_configuration(setup, name):
    _, request, home = setup
    bundled = Path(__file__).resolve().parents[1] / 'src/steward_harness/skills' / name
    with mapped(setup) as native:
        assert (native.home / 'skills' / name / 'SKILL.md').read_text() == (bundled / 'SKILL.md').read_text()
        assert (native.home / 'skills/workflow.md').read_text() == 'approved native workflow'
        assert (native.home / 'skills' / name / 'SKILL.md').is_file()
        assert not (home / 'skills' / name).exists()
        assert not (request.cwd / 'skills').exists()
    assert not native.home.exists()
    assert bundled.is_dir()


@pytest.mark.parametrize("name", ["commit", "git-reconciler"])
def test_instance_skill_takes_precedence_over_bundled_default(setup, name):
    _, _, home = setup
    skill = home / 'skills' / name
    skill.mkdir()
    (skill / 'SKILL.md').write_text('instance-owned commit workflow')
    with mapped(setup) as first, mapped(setup) as second:
        assert (first.home / 'skills' / name).resolve() == skill
        assert (second.home / 'skills' / name).resolve() == skill
    assert (skill / 'SKILL.md').read_text() == 'instance-owned commit workflow'

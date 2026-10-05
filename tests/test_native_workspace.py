"""Native files belong to candidate Git worlds; launch credentials do not."""

from dataclasses import replace
from pathlib import Path
import subprocess

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
    subprocess.run(['git', 'init', '-q', str(world)], check=True)
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
        (native.home / 'memories/MEMORY.md').write_text('native interpretation')
    # An anonymous launch home keeps its record and commits it like an owner's.
    assert native.home.exists() and record.read_text() == 'native original\n'
    assert _git(request.cwd, 'show', f':artefacts/codex/sessions/{record.name}') == 'native original\n'
    assert (home / 'auth.json').read_text() == 'provider credential'
    assert not (home / 'sessions').exists()
    assert not list(request.cwd.rglob('auth.json'))
    assert (request.cwd / 'memories/codex/MEMORY.md').read_text() == 'native interpretation'
    with mapped(setup, provider_session_id=SESSION) as resumed:
        assert resumed.home != native.home
        assert Path(resumed.resume).read_text() == 'native original\n'


def test_two_active_workspaces_never_retarget_each_others_native_writes(setup, tmp_path):
    other = tmp_path / 'other'
    other.mkdir()
    _git(other, 'init', '-q')
    with mapped(setup) as first, mapped(setup, cwd=other) as second:
        (first.home / 'sessions/first.jsonl').write_text('first')
        (second.home / 'sessions/second.jsonl').write_text('second')
        assert not (first.home / 'sessions/second.jsonl').exists()
        assert not (second.home / 'sessions/first.jsonl').exists()
    assert _git(setup[1].cwd, 'ls-files') == 'artefacts/codex/sessions/first.jsonl\n'
    assert _git(other, 'ls-files') == 'artefacts/codex/sessions/second.jsonl\n'


@pytest.mark.parametrize('component', ['artefacts', 'artefacts/codex', 'artefacts/codex/sessions'])
def test_a_symlinked_record_path_in_the_checkout_receives_nothing(setup, tmp_path, component):
    _, request, _ = setup
    outside = tmp_path / 'outside'
    outside.mkdir()
    link = request.cwd / component
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(outside, target_is_directory=True)
    with mapped(setup) as native:
        (native.home / 'sessions/record.jsonl').write_text('record')
    assert not list(outside.iterdir())
    assert _git(request.cwd, 'show', ':artefacts/codex/sessions/record.jsonl') == 'record'


@pytest.mark.parametrize('component', ['memories', 'memories/codex'])
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
    assert list(home.glob('.steward-launch-*'))


def test_missing_old_private_session_is_not_silently_restarted(setup):
    with pytest.raises(RuntimeExecutionError, match='native session absent or ambiguous') as failure:
        with mapped(setup, provider_session_id=SESSION):
            pytest.fail('must not launch')
    assert failure.value.session_id == SESSION


def test_readonly_native_calls_keep_records_outside_the_candidate(setup):
    with mapped(setup, sandbox_mode='read-only', provider_session_id=SESSION) as native:
        assert native.home == setup[2]
        assert native.resume == SESSION
    assert [p for p in setup[1].cwd.iterdir() if p.name != '.git'] == []


def test_private_mapping_cleanup_does_not_erase_partial_work_on_failure(setup):
    with pytest.raises(RuntimeError, match='provider failure'):
        with mapped(setup) as native:
            (native.home / 'sessions/partial.jsonl').write_text('partial native record')
            raise RuntimeError('provider failure')
    assert native.home.exists()
    assert (native.home / 'sessions/partial.jsonl').exists()
    assert _git(setup[1].cwd, 'show', ':artefacts/codex/sessions/partial.jsonl') == 'partial native record'


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
    assert native.home.exists()
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


def test_owner_home_preserves_native_databases_across_turns_and_controller_restart(setup):
    import sqlite3

    broker, request, source = setup
    with mapped(setup, native_owner='conversation:one', native_generation=3) as first:
        (first.home / 'sessions' / f'rollout-{SESSION}.jsonl').write_text('original')
        with sqlite3.connect(first.home / 'queue.sqlite') as database:
            database.execute('CREATE TABLE queued (value TEXT)')
            database.execute("INSERT INTO queued VALUES ('retained native job')")
    replacement_broker = UntrustedExecutionBroker(broker.config)
    with mapped((replacement_broker, request, source), native_owner='conversation:one',
                native_generation=3, provider_session_id=SESSION) as second:
        assert second.home == first.home
        assert second.resume.endswith(f'rollout-{SESSION}.jsonl')
        with sqlite3.connect(second.home / 'queue.sqlite') as database:
            assert database.execute('SELECT value FROM queued').fetchone() == ('retained native job',)
    assert second.home.exists()
    assert (source / 'state_5.sqlite').read_text() == 'private runtime state'
    assert not list(source.glob('.steward-launch-*'))


def test_concurrent_owner_homes_and_generations_do_not_share_runtime_state(setup, tmp_path):
    other = tmp_path / 'other-world'
    other.mkdir()
    _git(other, 'init', '-q')
    with mapped(setup, native_owner='one') as first, mapped(setup, native_owner='two', cwd=other) as second:
        (first.home / 'goals.sqlite').write_text('owner one')
        assert not (second.home / 'goals.sqlite').exists()
        (first.home / 'sessions/one.jsonl').write_text('one')
        assert not (second.home / 'sessions/one.jsonl').exists()
    with mapped(setup, native_owner='one', native_generation=2) as cleared:
        assert cleared.home not in {first.home, second.home}
        assert not (cleared.home / 'goals.sqlite').exists()
    with mapped(setup, native_owner='one', resolved=resolve_model('claude', 'fast')) as switched:
        assert switched.home not in {first.home, second.home, cleared.home}
    # Superseding a lineage must preserve unique database/queued state even
    # when no Git checkpoint has captured it.
    assert (first.home / 'goals.sqlite').read_text() == 'owner one'
    assert cleared.home.exists()
    assert switched.home.exists()
    assert second.home.exists()



def _git(cwd, *args):
    return subprocess.run(
        ['git', '-c', 'user.name=Fixture', '-c', 'user.email=fixture@localhost',
         '-c', 'commit.gpgsign=false', *args],
        cwd=cwd, check=True, capture_output=True, text=True).stdout


def test_owner_records_reach_the_index_not_the_checkout(setup):
    from steward_harness.runtime.native_evidence import EVIDENCE_PENDING

    _, request, _ = setup
    record = f'sessions/2026/10/04/rollout-{SESSION}.jsonl'
    with mapped(setup, native_owner='one') as native:
        assert not (native.home / 'sessions').is_symlink()
        (native.home / record).parent.mkdir(parents=True)
        (native.home / record).write_text('original\n')
        (native.home / 'memories/MEMORY.md').write_text('native interpretation')
    world_path = 'artefacts/codex/' + record
    assert not (request.cwd / 'artefacts').exists()
    assert not (request.cwd / EVIDENCE_PENDING).exists()
    # Skip-worktree: the checkout's lack of the file is not a deletion.
    assert _git(request.cwd, 'ls-files', '-v', world_path) == f'S {world_path}\n'
    assert _git(request.cwd, 'show', ':' + world_path) == 'original\n'
    assert (request.cwd / 'memories/codex/MEMORY.md').read_text() == 'native interpretation'
    _git(request.cwd, 'add', '--all')
    _git(request.cwd, 'commit', '-qm', 'turn')
    assert _git(request.cwd, 'show', 'HEAD:' + world_path) == 'original\n'
    assert _git(request.cwd, 'status', '--porcelain') == ''
    with mapped(setup, native_owner='one', provider_session_id=SESSION) as resumed:
        assert resumed.home == native.home
        assert resumed.resume == str(native.home / record)
        (native.home / record).write_text('original\nresumed\n')
    assert _git(request.cwd, 'diff', '--cached', '--name-only') == world_path + '\n'
    assert _git(request.cwd, 'show', ':' + world_path) == 'original\nresumed\n'


@pytest.mark.parametrize('checkout', ['released', 'uncaptured'])
def test_home_that_wrote_into_a_checkout_resumes_from_the_world(setup, checkout):
    _, request, _ = setup
    record = f'artefacts/codex/sessions/2026/rollout-{SESSION}.jsonl'
    other = 'artefacts/codex/sessions/2026/rollout-22222222-2222-4222-8222-222222222222.jsonl'
    for path, text in [(record, 'committed\n'), (other, 'another owner\n')]:
        (request.cwd / path).parent.mkdir(parents=True, exist_ok=True)
        (request.cwd / path).write_text(text)
    _git(request.cwd, 'add', '--all')
    _git(request.cwd, 'commit', '-qm', 'records written through the old link')
    with mapped(setup, native_owner='one') as native:
        pass
    sessions = native.home / 'sessions'
    sessions.rmdir()
    sessions.symlink_to(request.cwd / 'artefacts/codex/sessions', target_is_directory=True)
    if checkout == 'released':
        from steward_harness.runtime.native_evidence import leave_to_git
        leave_to_git(request.cwd)
        assert not (request.cwd / record).exists()
    else:
        (request.cwd / record).write_text('committed\nnot yet captured\n')
    with mapped(setup, native_owner='one', provider_session_id=SESSION) as resumed:
        assert not sessions.is_symlink()
        assert Path(resumed.resume) == sessions / '2026' / Path(record).name
        assert Path(resumed.resume).read_text() == (
            'committed\n' if checkout == 'released' else 'committed\nnot yet captured\n')
        assert not (sessions / '2026' / Path(other).name).exists()


def test_owner_home_refuses_retargeting_and_preserves_state_after_failure(setup, tmp_path):
    with pytest.raises(RuntimeError, match='provider failed'):
        with mapped(setup, native_owner='one') as first:
            (first.home / 'jobs.sqlite').write_text('must survive')
            raise RuntimeError('provider failed')
    other = tmp_path / 'other-world'
    other.mkdir()
    with pytest.raises(RuntimeExecutionError, match='native home link changed'):
        with mapped(setup, native_owner='one', cwd=other):
            pytest.fail('must not retarget an existing owner home')
    assert (first.home / 'jobs.sqlite').read_text() == 'must survive'
    assert (first.home / 'sessions').is_dir() and not (first.home / 'sessions').is_symlink()


def test_owner_home_does_not_link_instance_runtime_state_or_other_owners(setup):
    source = setup[2]
    (source / 'cache').mkdir()
    (source / 'history.jsonl').write_text('another owner')
    (source / '.steward-owner-uninspected').mkdir()
    with mapped(setup, native_owner='one') as native:
        assert not (native.home / 'cache').exists()
        assert not (native.home / 'history.jsonl').exists()
        assert not (native.home / '.steward-owner-uninspected').exists()
        assert (native.home / 'auth.json').read_text() == 'provider credential'
    assert (source / '.steward-owner-uninspected').is_dir()


def test_readonly_owner_imports_only_its_requested_legacy_original(setup):
    source = setup[2]
    records = source / 'sessions'
    records.mkdir()
    original = records / f'rollout-{SESSION}.jsonl'
    original.write_text('legacy original')
    (records / 'rollout-unrelated.jsonl').write_text('different owner')
    with mapped(setup, native_owner='reader', sandbox_mode='read-only', provider_session_id=SESSION) as native:
        assert Path(native.resume).read_text() == 'legacy original'
        assert Path(native.resume).is_relative_to(native.home)
        assert not (native.home / 'sessions/rollout-unrelated.jsonl').exists()
        assert not (native.home / 'state_5.sqlite').exists()
        (native.home / 'state_5.sqlite').write_text('owner state')
    original.write_text('legacy source changed')
    with mapped(setup, native_owner='reader', sandbox_mode='read-only', provider_session_id=SESSION) as again:
        assert again.home == native.home
        assert Path(again.resume).read_text() == 'legacy original'
        assert (again.home / 'state_5.sqlite').read_text() == 'owner state'
    assert [p for p in setup[1].cwd.iterdir() if p.name != '.git'] == []


def test_readonly_import_refuses_symlinked_legacy_records(setup, tmp_path):
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / f'rollout-{SESSION}.jsonl').write_text('not in seed custody')
    (setup[2] / 'sessions').symlink_to(outside, target_is_directory=True)
    with pytest.raises(RuntimeExecutionError, match='could not be prepared'):
        with mapped(setup, native_owner='reader', sandbox_mode='read-only', provider_session_id=SESSION):
            pytest.fail('must not follow a legacy record directory symlink')


def test_owned_authentication_replacement_requires_inspection(setup):
    with mapped(setup, native_owner='one') as native:
        (native.home / 'auth.json').unlink()
        (native.home / 'auth.json').write_text('provider refreshed credential')
    with pytest.raises(RuntimeExecutionError, match='native home link changed'):
        with mapped(setup, native_owner='one'):
            pytest.fail('must not silently split the configured credential authority')
    assert (native.home / 'auth.json').read_text() == 'provider refreshed credential'
    assert (setup[2] / 'auth.json').read_text() == 'provider credential'


def test_workspace_modules_cannot_shadow_preparation_stdlib(setup):
    (setup[1].cwd / 'json.py').write_text("raise RuntimeError('repository module executed')\n")
    with mapped(setup, native_owner='one') as native:
        assert native.home.is_dir()


def test_owner_policy_replacement_is_refused_without_erasing_it(setup):
    (setup[2] / 'config.toml').write_text('approved policy')
    with mapped(setup, native_owner='one') as native:
        policy = native.home / 'config.toml'
        policy.unlink()
        policy.write_text('local replacement')
    with pytest.raises(RuntimeExecutionError, match='native home link changed'):
        with mapped(setup, native_owner='one'):
            pytest.fail('must not accept a policy replacement')
    assert policy.read_text() == 'local replacement'


def test_owner_home_permissions_are_checked_on_reentry(setup):
    with mapped(setup, native_owner='one') as native:
        native.home.chmod(0o755)
    with pytest.raises(RuntimeExecutionError, match='mode 0700'):
        with mapped(setup, native_owner='one'):
            pytest.fail('must not use an exposed home')
    native.home.chmod(0o700)


@pytest.mark.parametrize('session', ['*', '../escape', 'not-a-uuid'])
def test_owner_session_glob_injection_is_refused(setup, session):
    with pytest.raises(RuntimeExecutionError, match='could not be prepared'):
        with mapped(setup, native_owner='one', provider_session_id=session):
            pytest.fail('must validate session before globbing')


def test_persistent_bundled_skills_recover_and_follow_instance_overrides(setup):
    with mapped(setup, native_owner='one') as native:
        skill = native.home / 'skills/commit'
        (skill / 'SKILL.md').unlink()  # interrupted old installation
    with mapped(setup, native_owner='one'):
        bundled = (skill / 'SKILL.md').read_text()
        assert bundled
    override = setup[2] / 'skills/commit'
    override.mkdir()
    (override / 'SKILL.md').write_text('instance override')
    with mapped(setup, native_owner='one'):
        assert skill.is_symlink()
        assert (skill / 'SKILL.md').read_text() == 'instance override'
        preserved = list(native.home.glob('.steward-preserved-skill-*'))
        assert len(preserved) == 1
        assert (preserved[0] / 'SKILL.md').read_text() == bundled
    (override / 'SKILL.md').unlink()
    override.rmdir()
    with mapped(setup, native_owner='one'):
        assert not skill.is_symlink()
        assert (skill / 'SKILL.md').read_text() == bundled


@pytest.mark.parametrize('initial,changed', [('read-only', 'workspace-write'), ('workspace-write', 'read-only')])
def test_owner_layout_change_fails_closed_preserving_generation(setup, initial, changed):
    with mapped(setup, native_owner='one', sandbox_mode=initial) as native:
        (native.home / 'jobs.sqlite').write_text('retained')
    with pytest.raises(RuntimeExecutionError) as error:
        with mapped(setup, native_owner='one', sandbox_mode=changed, provider_session_id=SESSION):
            pytest.fail('layout transition requires an explicit migration')
    assert error.value.session_id == SESSION
    assert (native.home / 'jobs.sqlite').read_text() == 'retained'
    with mapped(setup, native_owner='one', sandbox_mode=initial) as again:
        assert again.home == native.home


@pytest.mark.parametrize('provider', ['claude', 'glm'])
def test_claude_shaped_owner_homes_keep_user_instructions_and_styles(setup, provider):
    source = setup[2]
    (source / 'CLAUDE.md').write_text('user instructions')
    for name in ('rules', 'output-styles'):
        (source / name).mkdir()
        (source / name / 'chosen.md').write_text(name)
    with mapped(setup, native_owner='one', resolved=resolve_model(provider, 'fast')) as native:
        assert (native.home / 'CLAUDE.md').read_text() == 'user instructions'
        for name in ('rules', 'output-styles'):
            assert (native.home / name / 'chosen.md').read_text() == name


def test_legacy_import_refuses_an_existing_writer_and_preserves_source(setup):
    records = setup[2] / 'sessions'
    records.mkdir()
    original = records / f'rollout-{SESSION}.jsonl'
    original.write_text('retained legacy session')
    with original.open('a'):
        with pytest.raises(RuntimeExecutionError, match='requires an available Linux read lease'):
            with mapped(setup, native_owner='reader', sandbox_mode='read-only', provider_session_id=SESSION):
                pytest.fail('must not capture a writable source')
    assert original.read_text() == 'retained legacy session'
    assert not list(setup[2].glob('.steward-owner-*/sessions/*.jsonl'))
    assert not list(setup[2].glob('.steward-owner-*/sessions/.steward-import-*'))
    with mapped(setup, native_owner='reader', sandbox_mode='read-only', provider_session_id=SESSION) as native:
        assert Path(native.resume).read_text() == 'retained legacy session'


@pytest.mark.parametrize("signal_mode", ["handled", "ignored", "blocked"])
def test_legacy_import_refuses_writer_arriving_during_copy(setup, monkeypatch, tmp_path, signal_mode):
    import concurrent.futures
    import importlib
    import os
    import time

    module = importlib.import_module('steward_harness.runtime.native_workspace')
    ready = tmp_path / 'leased-copy-ready'
    # Pause after bytes have been copied but before the lease is checked and
    # the destination can be accepted. Only the preparation subprocess waits.
    injected = ('shutil.copyfileobj(original, imported)\n'
                f'                            pathlib.Path({str(ready)!r}).touch()\n'
                '                            import time; time.sleep(5)')
    script = module._PREPARE.replace('shutil.copyfileobj(original, imported)', injected)
    if signal_mode == "ignored":
        script = script.replace('signal.signal(signal.SIGIO, source_changed)',
                                'signal.signal(signal.SIGIO, signal.SIG_IGN)')
    if signal_mode == 'blocked':
        script = script.replace('signal.signal(signal.SIGIO, source_changed)',
                                'signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGIO})')
    assert str(ready) in script
    monkeypatch.setattr(module, '_PREPARE', script)
    records = setup[2] / 'sessions'
    records.mkdir()
    original = records / f'rollout-{SESSION}.jsonl'
    original.write_text('stable original')

    def prepare():
        with mapped(setup, native_owner='reader', sandbox_mode='read-only', provider_session_id=SESSION):
            pytest.fail('writer contention must refuse capture')

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        result = pool.submit(prepare)
        deadline = time.monotonic() + 8
        while not ready.exists() and not result.done() and time.monotonic() < deadline:
            time.sleep(.01)
        assert ready.exists(), 'preparation never acquired the lease'
        with pytest.raises(BlockingIOError):
            descriptor = os.open(original, os.O_WRONLY | os.O_NONBLOCK)
            os.close(descriptor)
        reason = 'writer requested access' if signal_mode == 'handled' else 'read lease was broken'
        with pytest.raises(RuntimeExecutionError, match=reason):
            result.result(timeout=8)
    assert original.read_text() == 'stable original'
    assert not list(setup[2].glob('.steward-owner-*/sessions/*.jsonl'))
    assert not list(setup[2].glob('.steward-owner-*/sessions/.steward-import-*'))


def test_legacy_import_refuses_writable_mapping_after_descriptor_closes(setup):
    import mmap

    records = setup[2] / 'sessions'
    records.mkdir()
    original = records / f'rollout-{SESSION}.jsonl'
    original.write_text('legacy original with writable mapping')
    with original.open('r+b') as descriptor:
        writer = mmap.mmap(descriptor.fileno(), 0, access=mmap.ACCESS_WRITE)
    try:
        with pytest.raises(RuntimeExecutionError, match='requires an available Linux read lease'):
            with mapped(setup, native_owner='reader', sandbox_mode='read-only', provider_session_id=SESSION):
                pytest.fail('a writable mmap remains a writer after the fd closes')
    finally:
        writer.close()
    assert not list(setup[2].glob('.steward-owner-*/sessions/*.jsonl'))
    assert not list(setup[2].glob('.steward-owner-*/sessions/.steward-import-*'))


@pytest.mark.parametrize('provider', ['claude', 'glm'])
def test_claude_credentials_are_not_cloned_into_owner_homes(setup, provider):
    (setup[2] / '.credentials.json').write_text('shared native credential authority')
    with mapped(setup, native_owner='one', resolved=resolve_model(provider, 'fast')) as native:
        assert not (native.home / '.credentials.json').exists()
        assert not (native.home / '.credentials.json').is_symlink()
    assert (setup[2] / '.credentials.json').read_text() == 'shared native credential authority'


@pytest.mark.parametrize('linked', [False, True])
def test_legacy_owner_credentials_remain_for_inspected_retirement(setup, linked):
    source = setup[2] / '.credentials.json'
    source.write_text('seed credential')
    options = dict(native_owner='one', resolved=resolve_model('claude', 'fast'))
    with mapped(setup, **options) as native:
        legacy = native.home / '.credentials.json'
        if linked:
            legacy.symlink_to(source)
        else:
            legacy.write_text('legacy provider refresh')
    with mapped(setup, **options):
        assert legacy.is_symlink() == linked
        assert legacy.read_text() == ('seed credential' if linked else 'legacy provider refresh')
    assert source.read_text() == 'seed credential'


def test_codex_shared_auth_write_is_visible_to_other_owner(setup):
    with mapped(setup, native_owner='one') as first:
        with mapped(setup, native_owner='two') as second:
            (first.home / 'auth.json').write_text('refreshed shared credential')
            assert (first.home / 'auth.json').is_symlink()
            assert (second.home / 'auth.json').read_text() == 'refreshed shared credential'
    with mapped(setup, native_owner='one') as again:
        assert (again.home / 'auth.json').read_text() == 'refreshed shared credential'


def test_codex_without_seed_auth_preserves_native_local_login(setup):
    (setup[2] / 'auth.json').unlink()
    with mapped(setup, native_owner='one') as first:
        (first.home / 'auth.json').write_text('independent native login')
    with mapped(setup, native_owner='one') as again:
        assert (again.home / 'auth.json').read_text() == 'independent native login'
    assert not (setup[2] / 'auth.json').exists()


def test_codex_seed_login_after_local_login_requires_inspection(setup):
    seed_auth = setup[2] / 'auth.json'
    seed_auth.unlink()
    with mapped(setup, native_owner='one') as first:
        local_auth = first.home / 'auth.json'
        local_auth.write_text('independent native login')
    seed_auth.write_text('new shared login')
    with pytest.raises(RuntimeExecutionError, match='native home link changed') as error:
        with mapped(setup, native_owner='one'):
            pytest.fail('must not choose between independently created credentials')
    assert str(local_auth) in str(error.value)
    assert local_auth.read_text() == 'independent native login'
    assert seed_auth.read_text() == 'new shared login'


@pytest.mark.parametrize('failure', ['timeout', 'oserror'])
@pytest.mark.parametrize('stage', ['validation', 'preparation'])
def test_import_setup_failure_preserves_session_identity(setup, monkeypatch, failure, stage):
    import subprocess
    broker = setup[0]
    original_run = broker.run

    def fail_preparation(command, **kwargs):
        from steward_harness.runtime.native_workspace import _PREPARE
        if (_PREPARE in command) == (stage == 'preparation'):
            if failure == 'timeout':
                raise subprocess.TimeoutExpired(command, 1)
            raise OSError('fixture preparation launch failure')
        return original_run(command, **kwargs)

    monkeypatch.setattr(broker, 'run', fail_preparation)
    with pytest.raises(RuntimeExecutionError, match='Native workspace .* failed') as error:
        with mapped(setup, native_owner='one', sandbox_mode='read-only', provider_session_id=SESSION):
            pytest.fail('must not launch after failed preparation')
    assert error.value.session_id == SESSION
    assert error.value.__cause__ is not None


def test_large_import_sigkill_never_resumes_partial_record(setup, monkeypatch):
    import importlib

    module = importlib.import_module('steward_harness.runtime.native_workspace')
    records = setup[2] / 'sessions'
    records.mkdir()
    original = records / f'rollout-{SESSION}.jsonl'
    content = b'{"fixture":"retained history"}\n' * 300000
    original.write_bytes(content)
    normal = module._PREPARE
    # Kill only this disposable preparation subprocess after persisting a prefix.
    # SIGKILL bypasses finally blocks, as controller/host crashes can do.
    injected = ('imported.write(original.read(65536)); imported.flush(); '
                'os.fsync(imported.fileno()); os.kill(os.getpid(), 9)')
    monkeypatch.setattr(module, '_PREPARE', normal.replace('shutil.copyfileobj(original, imported)', injected))
    with pytest.raises(RuntimeExecutionError) as error:
        with mapped(setup, native_owner='reader', sandbox_mode='read-only', provider_session_id=SESSION):
            pytest.fail('partial import must not launch a provider')
    assert error.value.session_id == SESSION
    assert not list(setup[2].glob('.steward-owner-*/sessions/*.jsonl'))
    leftovers = list(setup[2].glob('.steward-owner-*/sessions/.steward-import-*'))
    assert len(leftovers) == 1
    assert leftovers[0].read_bytes() == content[:65536]
    assert original.read_bytes() == content

    monkeypatch.setattr(module, '_PREPARE', normal)
    with mapped(setup, native_owner='reader', sandbox_mode='read-only', provider_session_id=SESSION) as resumed:
        assert Path(resumed.resume).read_bytes() == content
    # Retry must neither adopt nor sweep an uninspected interrupted artifact.
    assert leftovers[0].read_bytes() == content[:65536]
    assert original.read_bytes() == content


def test_import_real_timeout_reaps_preparer(setup, monkeypatch, tmp_path):
    import importlib
    import os

    module = importlib.import_module('steward_harness.runtime.native_workspace')
    pidfile = tmp_path / 'preparer-pid'
    records = setup[2] / 'sessions'
    records.mkdir()
    (records / f'rollout-{SESSION}.jsonl').write_text('original')
    injected = (f'pathlib.Path({str(pidfile)!r}).write_text(str(os.getpid())); '
                'import time; time.sleep(10); shutil.copyfileobj(original, imported)')
    monkeypatch.setattr(module, '_PREPARE', module._PREPARE.replace('shutil.copyfileobj(original, imported)', injected))
    with pytest.raises(RuntimeExecutionError) as error:
        with mapped(setup, native_owner='reader', sandbox_mode='read-only',
                    provider_session_id=SESSION, timeout_seconds=2):
            pytest.fail('timeout must prevent provider launch')
    assert error.value.session_id == SESSION
    assert pidfile.exists(), 'fixture did not reach the leased copy'
    with pytest.raises(ProcessLookupError):
        os.kill(int(pidfile.read_text()), 0)
    assert not list(setup[2].glob('.steward-owner-*/sessions/*.jsonl'))


def test_path_validation_cannot_reset_import_deadline(setup, monkeypatch):
    import importlib
    import types

    module = importlib.import_module('steward_harness.runtime.native_workspace')
    times = iter([100.0, 103.0])
    monkeypatch.setattr(module, 'time', types.SimpleNamespace(monotonic=lambda: next(times)))
    with pytest.raises(RuntimeExecutionError, match='exceeded execution deadline') as error:
        with mapped(setup, native_owner='reader', provider_session_id=SESSION, timeout_seconds=2):
            pytest.fail('spent validation budget must prevent preparation')
    assert error.value.session_id == SESSION
    assert not list(setup[2].glob('.steward-owner-*'))


@pytest.mark.parametrize('companion_kind', ['directory', 'file', 'dangling-symlink', 'late-directory'])
def test_claude_legacy_primary_refuses_visible_companion_state(setup, monkeypatch, companion_kind):
    import importlib
    module = importlib.import_module('steward_harness.runtime.native_workspace')
    broker, request, source = setup
    project = source / 'projects/legacy-project'
    project.mkdir(parents=True)
    original = project / f'{SESSION}.jsonl'
    original.write_text('retained parent transcript')
    companion = project / SESSION
    if companion_kind == 'directory':
        companion.mkdir()
        (companion / 'child.jsonl').write_text('retained child transcript')
    elif companion_kind == 'file':
        companion.write_text('unknown companion state')
    elif companion_kind == 'dangling-symlink':
        companion.symlink_to(project / 'absent')
    else:
        monkeypatch.setattr(module, '_PREPARE', module._PREPARE.replace(
            'shutil.copyfileobj(original, imported)',
            "shutil.copyfileobj(original, imported); (source / relative.parent / session).mkdir()"))
    requested = replace(request, resolved=resolve_model('claude', 'fast'),
        native_owner='reader', sandbox_mode='read-only', provider_session_id=SESSION)
    with pytest.raises(RuntimeExecutionError, match='companion state requiring inspected migration') as caught:
        with native_workspace(broker, requested, source,
                mappings={'projects': 'artefacts/claude/projects'},
                resume_pattern='artefacts/claude/projects/*/{session}.jsonl'):
            pytest.fail('incomplete parent-only migration admitted')
    assert caught.value.session_id == SESSION
    assert original.read_text() == 'retained parent transcript'
    assert companion.exists() or companion.is_symlink()
    assert not list(source.glob(f'.steward-owner-*/projects/*/{SESSION}.jsonl'))


@pytest.mark.parametrize('failure', ['capture', 'commit'])
@pytest.mark.parametrize('mode', ['workspace-write', 'read-only'])
def test_complete_owner_evidence_survives_failed_git_capture_and_generation_change(setup, failure, mode):
    import contextlib
    import hashlib
    import os

    broker, request, source = setup
    # These synthetic files model storage classes, not a provider schema. No
    # assumption is made that an index or a queue can be rebuilt from JSONL.
    fixtures = {
        f'sessions/rollout-{SESSION}.jsonl': b'old rollout with tool event\n',
        'sessions/child/tool-results/result.txt': b'complete tool result',
        'memories/MEMORY.md': b'old native memory',
        'state.sqlite': b'unique database fixture',
        'thread_history/thread.json': b'unique thread history',
        'queue/pending.json': b'unfinished native research',
    }
    if failure == 'commit':
        hook = request.cwd / '.git/hooks/pre-commit'
        hook.write_text('#!/bin/sh\nexit 1\n')
        hook.chmod(0o700)
    refused = failure == 'capture' and mode == 'workspace-write'
    with (pytest.raises(RuntimeExecutionError, match='originals retained') if refused
          else contextlib.nullcontext()), mapped(setup, native_owner='one', sandbox_mode=mode) as first:
        for relative, data in fixtures.items():
            p = first.home / relative
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)
            os.utime(p, (946684800, 946684800))  # 2000-01-01
        if failure == 'capture':
            (request.cwd / '.git/index.lock').write_text('simulated interrupted capture')
            failed = subprocess.run(['git', 'add', '.'], cwd=request.cwd, capture_output=True)
        else:
            (request.cwd / 'work.txt').write_text('synthetic work')
            subprocess.run(['git', 'add', '.'], cwd=request.cwd, check=True)
            failed = subprocess.run(
                ['git', '-c', 'user.name=Fixture', '-c', 'user.email=fixture@localhost',
                 '-c', 'commit.gpgsign=false', 'commit', '-qm', 'must fail'],
                cwd=request.cwd, capture_output=True)
        assert failed.returncode != 0
    (request.cwd / '.git/index.lock').unlink(missing_ok=True)
    with mapped(setup, native_owner='one', native_generation=2, sandbox_mode=mode):
        pass
    for relative, data in fixtures.items():
        assert hashlib.sha256((first.home / relative).read_bytes()).digest() == hashlib.sha256(data).digest()


def test_failed_capture_retains_home_and_pending_checkout(setup, monkeypatch):
    import json
    import subprocess
    from steward_harness.runtime.native_evidence import EVIDENCE_PENDING

    broker, request, _ = setup
    original = broker.run

    def fail_final(*args, **kwargs):
        data = kwargs.get('input_text')
        if data and json.loads(data)[0] == 'capture':
            return subprocess.CompletedProcess(args, 1, '', 'simulated full disk')
        return original(*args, **kwargs)

    monkeypatch.setattr(broker, 'run', fail_final)
    with pytest.raises(RuntimeExecutionError, match='originals retained'):
        with mapped(setup) as native:
            (native.home / 'unique.sqlite').write_bytes(b'uncaptured native queue')
            (native.home / 'sessions/latest.jsonl').write_text('last tool result')
    assert (native.home / 'unique.sqlite').read_bytes() == b'uncaptured native queue'
    assert (native.home / 'sessions/latest.jsonl').read_text() == 'last tool result'
    assert (request.cwd / EVIDENCE_PENDING).exists()


@pytest.mark.parametrize('provider', ['claude', 'glm'])
@pytest.mark.parametrize('owner,access', [('one', 'workspace-write'), (None, 'workspace-write'), (None, 'read-only')])
def test_native_temporary_output_is_private_and_survives_failure(setup, provider, owner, access):
    _, _, seed = setup
    seeded = seed / '.steward-tmp'
    seeded.mkdir(mode=0o700)
    (seeded / 'old-output').write_bytes(b'old seed evidence')
    with pytest.raises(RuntimeError, match='failed Git capture'):
        with mapped(setup, native_owner=owner, sandbox_mode=access,
                    resolved=resolve_model(provider, 'fast')) as native:
            temporary = native.home / '.steward-tmp'
            assert not temporary.is_symlink()
            assert temporary.stat().st_mode & 0o777 == 0o700
            if native.home != seed:
                assert not (temporary / 'old-output').exists()
            (temporary / 'task-output').write_bytes(b'unique temporary tool result')
            raise RuntimeError('failed Git capture')
    assert (temporary / 'task-output').read_bytes() == b'unique temporary tool result'
    assert (seeded / 'old-output').read_bytes() == b'old seed evidence'


def test_direct_seed_temporary_directory_refuses_external_symlink(setup, tmp_path):
    _, _, seed = setup
    external = tmp_path / 'other-owner'
    external.mkdir(mode=0o700)
    (external / 'evidence').write_bytes(b'other owner')
    (seed / '.steward-tmp').symlink_to(external, target_is_directory=True)
    with pytest.raises(RuntimeExecutionError, match='temporary directory preparation failed'):
        with mapped(setup, sandbox_mode='read-only', resolved=resolve_model('claude', 'fast')):
            pytest.fail('must refuse a redirected private temp root')
    assert (external / 'evidence').read_bytes() == b'other owner'


def test_a_read_only_run_commits_its_records_to_the_checkout_it_is_given(setup, tmp_path):
    """Read-only limits what the agent writes, never whether its record is kept."""
    _, request, _ = setup
    organisation = tmp_path / 'organisation'
    organisation.mkdir()
    with mapped(setup, native_owner='task:review', sandbox_mode='read-only',
                cwd=organisation, record_checkout=request.cwd) as native:
        (native.home / f'sessions/rollout-{SESSION}.jsonl').write_text('review transcript\n')
        (native.home / 'memories/MEMORY.md').write_text('review memory')
    assert not list(organisation.iterdir())
    assert _git(request.cwd, 'show', f':artefacts/codex/sessions/rollout-{SESSION}.jsonl') == 'review transcript\n'
    assert _git(request.cwd, 'show', ':memories/codex/MEMORY.md') == 'review memory'
    assert not (request.cwd / 'artefacts').exists() and not (request.cwd / 'memories').exists()
    # Without a record checkout a read-only run has nowhere to commit; its home keeps it.
    with mapped(setup, native_owner='task:other', sandbox_mode='read-only') as unrecorded:
        (unrecorded.home / 'sessions/kept.jsonl').write_text('kept')
    assert (unrecorded.home / 'sessions/kept.jsonl').read_text() == 'kept'


def test_a_run_working_elsewhere_resumes_from_its_record_checkout(setup, tmp_path):
    """The voice agent works in a code repository but commits records to the world."""
    _, world, _ = setup[0], setup[1].cwd, None
    code = tmp_path / 'code'
    code.mkdir()
    _git(code, 'init', '-q')
    with mapped(setup, native_owner='voice:guru', cwd=code, record_checkout=world) as native:
        (native.home / f'sessions/rollout-{SESSION}.jsonl').write_text('spoken turn\n')
    assert _git(world, 'show', f':artefacts/codex/sessions/rollout-{SESSION}.jsonl') == 'spoken turn\n'
    assert _git(code, 'ls-files') == ''
    # A new generation home has no copy; it resumes from the world's index.
    with mapped(setup, native_owner='voice:guru', native_generation=2, cwd=code,
                record_checkout=world, provider_session_id=SESSION) as resumed:
        assert resumed.home != native.home
        assert Path(resumed.resume).read_text() == 'spoken turn\n'
    assert (world / 'memories/codex').is_dir() and not (code / 'memories').exists()


def test_codex_thread_record_follows_the_rollout_into_its_home(tmp_path):
    import sqlite3
    import sys
    from steward_harness.runtime.providers.codex_app_server import _RELOCATE_THREAD

    home = tmp_path / 'home'
    home.mkdir()
    linked = '/worktrees/session-x/artefacts/codex/sessions/2026/09/30/rollout-a.jsonl'
    with sqlite3.connect(home / 'state_5.sqlite') as database:
        database.execute('CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT NOT NULL)')
        database.executemany('INSERT INTO threads VALUES (?, ?)', [('a', linked), ('b', '/elsewhere/b.jsonl')])
    rollout = str(home / 'sessions/2026/09/30/rollout-a.jsonl')
    subprocess.run([sys.executable, '-I', '-c', _RELOCATE_THREAD, str(home), 'a', rollout], check=True)
    with sqlite3.connect(home / 'state_5.sqlite') as database:
        assert dict(database.execute('SELECT id, rollout_path FROM threads')) == {
            'a': rollout, 'b': '/elsewhere/b.jsonl'}

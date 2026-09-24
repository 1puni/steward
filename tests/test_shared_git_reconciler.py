"""Shared resolver policy and acceptance without holding the world lease during cognition."""

from state_fixtures import prepare_turn
import subprocess
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from steward_harness.config.schema import StewardConfig
from steward_harness.daemon import StewardDaemon
from steward_harness.runtime.contracts import RuntimeUnavailable
from steward_harness.world.turn_checkpoint import WorldContentConflict
from world_fixtures import ReadingAdapter
from test_world_turn_checkpoint import _git_world, _checkpoint, _commit_all, _finish, _head


def prepare(checkpoint, turn, key):
    state = checkpoint.state
    conversation = state.get_or_create_conversation(
        'telegram', key, provider='codex', profile='balanced',
    )
    source, _ = state.start_turn(conversation.conversation_id, key, 'operator', 'save')
    event = str(source.turn_id)
    base, sha = turn.base_sha, checkpoint.retain(turn, event, 'save', 'saved', key)
    prepare_turn(state, event, world_root=str(checkpoint.world.root),
        base_sha=base, candidate_sha=sha, output='saved', provider='codex', model='model',
        provider_session_id=None, profile='balanced',
    )
    return event, lambda: state.accept_turn(event, visible_reply='saved', spec=None, rejection=None)


def conflicting_world(tmp_path):
    world = _git_world(tmp_path / 'world')
    (world.root / 'shared.md').write_text('base\n')
    _commit_all(world.root, 'base')
    checkpoint = _checkpoint(world, tmp_path)
    turn = checkpoint.checkout(workspace_id='conflicted')
    (turn.path / 'shared.md').write_text('session\n')
    (world.root / 'shared.md').write_text('accepted\n')
    _commit_all(world.root, 'concurrent world edit')
    event, finalize = prepare(checkpoint, turn, 'first')
    return checkpoint, turn, event, finalize


def test_slow_resolver_allows_peer_acceptance_then_revalidates_current_world(tmp_path):
    checkpoint, turn, event, finalize = conflicting_world(tmp_path)
    entered, release = Event(), Event()
    calls = []

    def resolve(request):
        calls.append(request.base_ref)
        if len(calls) == 1:
            entered.set()
            assert release.wait(10)
        (request.worktree / 'shared.md').write_text('accepted and session\n')
        # Native runtime evidence must become part of the reconciled candidate.
        records = request.worktree / 'artefacts'
        records.mkdir(exist_ok=True)
        (records / 'resolution.txt').write_text('native resolver record')

    checkpoint.resolve_turn = resolve
    with ThreadPoolExecutor(max_workers=2) as executor:
        pending = executor.submit(checkpoint.apply, event, finalize)
        try:
            assert entered.wait(10)
            peer = checkpoint.checkout(workspace_id='peer')
            (peer.path / 'peer.md').write_text('peer progress')
            accepted = executor.submit(_finish, checkpoint, peer, 'peer', 'save peer', 'saved')
            accepted.result(timeout=10)  # Would time out if cognition held the lease.
            peer_sha = _head(checkpoint.world.root)
            assert checkpoint.state.prepared_turn(event)['state'] == 'running'
        finally:
            release.set()
        pending.result(timeout=15)
    assert calls == [calls[0], peer_sha]
    assert (checkpoint.world.root / 'shared.md').read_text() == 'accepted and session\n'
    assert (checkpoint.world.root / 'peer.md').read_text() == 'peer progress'
    assert (checkpoint.world.root / 'artefacts/resolution.txt').read_text() == 'native resolver record'
    assert checkpoint.state.prepared_turn(event)['state'] == 'completed'
    assert checkpoint.state.pending_turns() == []
    assert turn.path.is_dir()
    checkpoint.apply(event, finalize)
    assert len(calls) == 2  # Receipt replay cannot rerun the resolver.


def test_world_resolver_failure_retains_original_candidate_and_diagnostics(tmp_path):
    checkpoint, turn, event, finalize = conflicting_world(tmp_path)
    base = _head(checkpoint.world.root)
    candidate = checkpoint.state.prepared_turn(event)['candidate_sha']
    def unavailable(request):
        raise RuntimeUnavailable('configured resolver unavailable')
    checkpoint.resolve_turn = unavailable
    with pytest.raises(WorldContentConflict, match='RuntimeUnavailable: configured resolver unavailable'):
        checkpoint.apply(event, finalize)
    row = checkpoint.state.prepared_turn(event)
    assert row['state'] == 'running'
    assert row['candidate_sha'] == candidate
    assert _head(checkpoint.world.root) == base
    assert subprocess.run(['git', 'show', f'{candidate}:shared.md'], cwd=checkpoint.world.root,
                          check=True, capture_output=True, text=True).stdout.strip() == 'session'
    assert turn.path.is_dir()


def test_world_reconciliation_uses_a_named_procedure(tmp_path, monkeypatch):
    world = _git_world(tmp_path / 'world')
    work = tmp_path / 'work'
    work.mkdir()
    instructions = tmp_path / 'reconcile.md'
    instructions.write_text('Preserve both accepted intents.')
    config = StewardConfig.model_validate({
        'identity': {'name': 'Steward', 'slug': 'test'},
        'provider': {'state_db': str(tmp_path / 'state.db'), 'workdir': str(work),
                     'default_family': 'codex', 'fallback_families': [],
                     'default_profile': 'fast', 'models': {'codex': {'deep': 'selected-reconciler-model'}}},
        'world': {'root': str(world.root), 'reconcile': 'merge-intents'},
        'procedures': {'merge-intents': {'instructions': str(instructions), 'provider': 'codex',
            'model': {'model': 'selected-reconciler-model'}, 'access': 'workspace-write'}},
    })
    checkpoints = []
    import steward_harness.daemon as daemon_module
    original = daemon_module.WorldTurnCheckpoint
    def capture(*args, **kwargs):
        checkpoint = original(*args, **kwargs)
        checkpoints.append(checkpoint)
        return checkpoint
    monkeypatch.setattr(daemon_module, 'WorldTurnCheckpoint', capture)
    adapter = ReadingAdapter('resolver-session', lambda request: 'resolved')
    daemon = StewardDaemon(config, tmp_path / 'steward.yaml', adapters={'codex': adapter})
    with daemon._daemon_lease():
        try:
            daemon._start_owned()
            kernel = daemon._kernel
            assert not hasattr(kernel.reconciler, 'resolve_turn')
            assert not hasattr(kernel.reconciler, 'repair_turn')
            callback = checkpoints[0].resolve_turn
            from steward_harness.git_reconcile import ResolverTurn
            callback(ResolverTurn(world.root, ('shared.md',), _head(world.root), 'candidate', 1))
            assert 'Preserve both accepted intents.' in adapter.requests[0].prompt
            assert adapter.requests[0].resolved.model == 'selected-reconciler-model'
        finally:
            daemon.stop()

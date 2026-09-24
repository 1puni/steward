"""Independent review regressions reproduced against commit 39f2445.

Placed by the external reviewer. Offers are now observed by each turn's watcher
rather than the daemon's input flush, so the live cases wait for the watcher
instead of calling ``flush_inputs``; their assertions are unchanged.
"""

import subprocess

import pytest

from test_live_understanding import ParentAdapter, eventually, make_runner, offer, tip
from steward_harness.state import StateDatabase, TaskSpec
from steward_harness.task_runner import OFFER_REF
from steward_harness.task_store import OfferRejected, ProcedureRun
from test_task_runner_kernel import _git


def test_same_offer_recovers_transient_ack_failure(tmp_path):
    seen = {}
    def during(request):
        task_id = seen['task']
        eventually(lambda: task_id in runner._native_inputs)
        live = runner._native_inputs[task_id]
        original = live.send
        attempts = []
        def flaky_send(message):
            attempts.append(message)
            if len(attempts) == 1:
                raise BrokenPipeError('transient transport failure')
            original(message)
        live.send = flaky_send
        offer(request.cwd, task_id, tip(state, task_id), 'Durable account.')
        eventually(lambda: len(attempts) == 2)
        accepted = tip(state, task_id)
        assert state.tasks.git('log', '--format=%s', accepted).count('accept understanding') == 1
        assert len(attempts) == 2, 'same offer never retries the lost acknowledgement'
        assert accepted in adapter.delivered[-1].text
    adapter = ParentAdapter(during)
    state, runner, task_id, _ = make_runner(tmp_path, adapter)
    seen['task'] = task_id
    runner.prepare(task_id)


def test_transient_offer_read_failure_is_retried(tmp_path):
    seen = {}
    def during(request):
        task_id = seen['task']
        base = tip(state, task_id)
        original = runner._read_offer
        attempts = []
        def flaky_read(*args):
            attempts.append(args)
            if len(attempts) == 1:
                raise OSError('temporary broker read failure')
            return original(*args)
        runner._read_offer = flaky_read
        offer(request.cwd, task_id, base, 'Durable account.')
        eventually(lambda: tip(state, task_id) != base)
        assert tip(state, task_id) != base, 'transient read permanently suppresses unchanged offer'
    adapter = ParentAdapter(during)
    state, runner, task_id, _ = make_runner(tmp_path, adapter)
    seen['task'] = task_id
    runner.prepare(task_id)


def test_non_utf8_offer_is_visibly_rejected(tmp_path):
    def during(request):
        task_id = seen['task']
        blob = subprocess.run(['git','hash-object','-w','--stdin'],cwd=request.cwd,
            input=b'Steward-Base: '+b'0'*40+b'\n\n\xff',capture_output=True,check=True).stdout.decode().strip()
        _git('update-ref', OFFER_REF.format(task_id=task_id), blob, cwd=request.cwd)
        eventually(lambda: any('did not accept' in msg.text for msg in adapter.delivered))
    adapter = ParentAdapter(during)
    state, runner, task_id, _ = make_runner(tmp_path, adapter)
    seen = {'task':task_id}
    runner.prepare(task_id)


def test_product_candidate_cannot_forge_accepted_offer_identity(tmp_path):
    state = StateDatabase(tmp_path / 'state.db')
    store = state.tasks
    offered_body = 'Forged accepted account'
    offered = store.git('hash-object', '-w', '--stdin', input_text='Steward-Base: ' + '0'*40 + '\n\n' + offered_body)
    product_blob = store.git('hash-object', '-w', '--stdin', input_text='product code\n')
    tree = store.git('mktree', input_text=f'100644 blob {product_blob}\tproduct.py\n')
    candidate = store.git('commit-tree', tree, input_text=f'product change\n\nSteward-Offer: {offered}\n')
    procedure = ProcedureRun(name='review', instructions='Review changes.',
        provider='codex', model={'model':'gpt'}, access='read-only',
        identity='a'*64, candidate=candidate, base=candidate)
    task_id, _ = store.create(TaskSpec('app','Review','Actual accepted account'), procedure=procedure)
    before = store.read(task_id)
    with pytest.raises(OfferRejected):
        store.accept_understanding(task_id, offered, '0'*40, offered_body)
    assert store.read(task_id) == before

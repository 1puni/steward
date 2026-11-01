"""Independent review invariants across the synchronous task-call boundary."""
import subprocess

import pytest

from test_live_understanding import ParentAdapter, make_runner, tip
from steward_harness.state import StateDatabase, TaskSpec
from steward_harness.task_store import OfferRejected, ProcedureRun


def test_non_utf8_task_call_is_visibly_rejected(tmp_path):
    def during(request):
        before = tip(state, task_id)
        result = subprocess.run(['curl', '--silent', '--show-error', '--noproxy', '*',
            '--unix-socket', request.task_call_socket, '-H', 'Content-Type: application/json',
            '--data-binary', '@-', 'http://localhost/task'], input=b'{"operation":"understanding","key":"bad","body":"\xff"}',
            capture_output=True, check=True)
        assert b'error' in result.stdout
        assert tip(state, task_id) == before
    state, runner, task_id, _ = make_runner(tmp_path, ParentAdapter(during))
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

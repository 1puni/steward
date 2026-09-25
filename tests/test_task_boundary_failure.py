"""Unverified provider teardown must not enter task worktree capture."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from steward_harness.runtime.contracts import RuntimeExecutionError
from steward_harness.runtime.execution import ExecutionBoundaryUnavailable
from steward_harness.state import CheckpointDisposition
from steward_harness.task_runner import TaskRunner, _boundary_failed


@pytest.mark.parametrize('wrapped', [False, True, 'oserror'])
@pytest.mark.parametrize('interruption', ['failure', 'cancel', 'shutdown'])
def test_boundary_failure_blocks_without_worktree_capture(wrapped, interruption):
    boundary = ExecutionBoundaryUnavailable('owned unit remains populated')
    error = (OSError('wrapped filesystem failure') if wrapped == 'oserror' else
             RuntimeExecutionError('native failure', session_id='retained') if wrapped else boundary)
    if wrapped:
        error.__cause__ = boundary
    task = SimpleNamespace(repository='repo', procedure=None)
    tasks = SimpleNamespace(get=Mock(return_value=task), cancelled=Mock(return_value=False),
                            finish_slice=Mock())
    runner = object.__new__(TaskRunner)
    runner.state = SimpleNamespace(tasks=tasks)
    runner.repositories = {'repo': object()}
    runner._stopping = Mock()
    runner._stopping.is_set.return_value = interruption == 'shutdown'
    runner._open_worktree = Mock(return_value=(None, '/fixture', None))

    def fail(*args):
        tasks.cancelled.return_value = interruption == 'cancel'
        raise error

    runner._work = Mock(side_effect=fail)
    runner._autosave = Mock(side_effect=AssertionError('unsafe autosave'))
    runner._retain_work = Mock(side_effect=AssertionError('unsafe capture'))
    runner._autosave_and_interrupt = Mock(side_effect=AssertionError('unsafe interruption'))
    assert runner._run_owned('task', 'accepted-revision') == 'task'
    tasks.finish_slice.assert_called_once()
    kwargs = tasks.finish_slice.call_args.kwargs
    assert kwargs['disposition'] == CheckpointDisposition.BLOCKED
    assert kwargs['consumed_input_ids'] == frozenset()
    assert 'work_sha' not in kwargs
    assert 'capture refused' in kwargs['detail']
    runner._autosave.assert_not_called()
    runner._retain_work.assert_not_called()
    runner._autosave_and_interrupt.assert_not_called()


def test_boundary_detection_handles_context_and_cycles():
    outer = RuntimeExecutionError('outer')
    inner = RuntimeError('inner')
    outer.__cause__ = inner
    inner.__cause__ = outer
    assert not _boundary_failed(outer)
    inner.__cause__ = None
    inner.__context__ = ExecutionBoundaryUnavailable('failed containment')
    assert _boundary_failed(outer)
    inner.__cause__ = RuntimeError('unrelated explicit cause')
    assert _boundary_failed(outer)


@pytest.mark.parametrize('phase', ['open', 'stage', 'commit', 'retain'])
@pytest.mark.parametrize('shutdown', [False, True])
def test_containment_failure_during_error_recovery_stops_capture(phase, shutdown):
    task = SimpleNamespace(repository='repo', procedure=None, branch='branch',
                           session_id='owner', task_id='task', work_sha='prior')
    tasks = SimpleNamespace(get=Mock(return_value=task), cancelled=Mock(return_value=False),
                            finish_slice=Mock(), change=Mock())
    runner = object.__new__(TaskRunner)
    runner.state = SimpleNamespace(tasks=tasks, get_conversation=Mock(
        return_value=SimpleNamespace(provider_session_id='native')))
    runner.repositories = {'repo': object()}
    runner._stopping = Mock()
    runner._stopping.is_set.return_value = shutdown
    checkpoint = SimpleNamespace(stage=Mock(), commit=Mock())
    runner._open_worktree = Mock(return_value=(None, '/fixture', checkpoint))
    runner._work = Mock(side_effect=RuntimeExecutionError('ordinary provider failure'))
    runner._retain_work = Mock(return_value='new-head')
    boundary = ExecutionBoundaryUnavailable('autosave unit teardown unverified')
    if phase == 'open':
        runner._open_worktree.side_effect = [(None, '/fixture', checkpoint), boundary]
    elif phase == 'retain':
        runner._retain_work.side_effect = boundary
    else:
        getattr(checkpoint, phase).side_effect = boundary

    assert runner._run_owned('task', 'revision') == 'task'
    tasks.finish_slice.assert_called_once()
    result = tasks.finish_slice.call_args.kwargs
    assert result['disposition'] == CheckpointDisposition.BLOCKED
    assert 'work_sha' not in result
    assert result['consumed_input_ids'] == frozenset()
    tasks.change.assert_not_called()
    if phase != 'retain':
        runner._retain_work.assert_not_called()
    if phase in ('open', 'stage'):
        checkpoint.commit.assert_not_called()


def test_boundary_detection_traverses_exception_groups():
    group = ExceptionGroup('cleanup failures', [ValueError('ordinary'),
                           ExecutionBoundaryUnavailable('unit not empty')])
    assert _boundary_failed(group)
    assert not _boundary_failed(ExceptionGroup('ordinary failures', [ValueError('ordinary')]))


def test_failed_hold_write_propagates_without_claiming_a_block(caplog):
    runner = object.__new__(TaskRunner)
    runner._run_owned_turn = Mock(side_effect=ExecutionBoundaryUnavailable('unverified'))
    runner.state = SimpleNamespace(tasks=SimpleNamespace(
        finish_slice=Mock(side_effect=OSError('accepted metadata unavailable'))))
    with pytest.raises(OSError, match='accepted metadata unavailable'):
        runner._run_owned('task', 'revision')
    assert 'blocked hold could not be recorded' in caplog.text

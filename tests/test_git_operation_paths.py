"""Interruption guards resolve linked-worktree paths with one owned Git launch."""

from pathlib import Path
import subprocess

import pytest

from steward_harness.git import IN_PROGRESS_GIT_MARKERS, git_operation_paths


@pytest.mark.parametrize('linked', [False, True])
def test_operation_paths_follow_git_layout_in_one_invocation(tmp_path, linked):
    root = tmp_path / 'repository with spaces'
    subprocess.run(['git', 'init', str(root)], check=True, capture_output=True)
    subprocess.run(['git', '-C', str(root), '-c', 'user.name=Test', '-c',
                    'user.email=test@example.invalid', 'commit', '--allow-empty', '-m', 'init'],
                   check=True, capture_output=True)
    path = root
    if linked:
        path = tmp_path / 'linked worktree'
        subprocess.run(['git', '-C', str(root), 'worktree', 'add', '--detach', str(path)],
                       check=True, capture_output=True)
    calls = []

    def git(*args):
        calls.append(args)
        return subprocess.check_output(['git', '-C', str(path), *args], text=True)

    paths = git_operation_paths(git)
    assert len(calls) == 1
    assert list(paths) == list(IN_PROGRESS_GIT_MARKERS)
    for marker, marker_path in paths.items():
        expected = subprocess.check_output(
            ['git', '-C', str(path), 'rev-parse', '--path-format=absolute', '--git-path', marker],
            text=True).strip()
        assert marker_path == Path(expected)
        if linked:
            assert marker_path.parent != root / '.git'


@pytest.mark.parametrize('output', ['', '/tmp/a\n' * 5, '/tmp/a\n' * 7,
                                    'relative\n' * 6, '/tmp/line\nbreak\n' * 6])
def test_ambiguous_marker_output_refuses_the_guard(output):
    with pytest.raises(ValueError, match='ambiguous'):
        git_operation_paths(lambda *args: output)

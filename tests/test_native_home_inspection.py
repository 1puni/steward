import importlib.util
from pathlib import Path

import pytest


@pytest.fixture
def inspector():
    spec = importlib.util.spec_from_file_location(
        'native_home_inspector', Path(__file__).parents[1] / 'scripts/inspect-native-home.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_inventory_does_not_follow_links_or_open_special_files(tmp_path, inspector, monkeypatch):
    import os
    home = tmp_path / 'home'
    home.mkdir()
    (home / 'auth.json').write_text('private credential sentinel')
    (home / 'outside').symlink_to(tmp_path, target_is_directory=True)
    (home / 'dangling').symlink_to(tmp_path / 'absent')
    os.mkfifo(home / 'pipe')
    (home / 'state').mkdir()
    (home / 'state' / 'db').write_bytes(b'123')
    original_open = os.open

    def directory_only(path, flags, *args, **kwargs):
        assert flags & os.O_DIRECTORY
        assert flags & os.O_NOFOLLOW
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(inspector.os, 'open', directory_only)
    result = inspector.inspect_home(home)
    assert len(result['entries']) == 6
    assert result['regular_file_bytes'] == len('private credential sentinel') + 3
    assert 'private credential sentinel' not in str(result)
    assert not result['retirement_authorized']
    assert not result['writer_absence_verified']
    assert not result['coherent_snapshot']


def test_refuses_symlink_ancestor(tmp_path, inspector):
    (tmp_path / 'real').mkdir()
    (tmp_path / 'alias').symlink_to(tmp_path / 'real', target_is_directory=True)
    with pytest.raises(OSError):
        inspector.inspect_home(tmp_path / 'alias')


def test_refuses_directory_replaced_with_link_during_scan(tmp_path, inspector, monkeypatch):
    home = tmp_path / 'home'
    home.mkdir()
    nested = home / 'nested'
    nested.mkdir()
    original_open = inspector.os.open

    def replace_before_open(path, flags, *args, **kwargs):
        if path == 'nested':
            nested.rename(home / 'preserved')
            nested.symlink_to(tmp_path, target_is_directory=True)
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(inspector.os, 'open', replace_before_open)
    with pytest.raises(OSError):
        inspector.inspect_home(home)
    assert (home / 'preserved').is_dir()


@pytest.mark.parametrize('limit', ['MAX_ENTRIES', 'MAX_DEPTH'])
def test_refuses_incomplete_bounded_inventory(tmp_path, inspector, monkeypatch, limit):
    (tmp_path / 'nested').mkdir()
    (tmp_path / 'nested' / 'file').touch()
    monkeypatch.setattr(inspector, limit, 0)
    with pytest.raises(ValueError, match='limit'):
        inspector.inspect_home(tmp_path)


def test_cli_reports_success_or_no_complete_inventory(tmp_path, inspector):
    import json
    import subprocess
    import sys

    command = [sys.executable, '-I', inspector.__file__]
    success = subprocess.run(command + [str(tmp_path)], capture_output=True, text=True, timeout=5)
    assert success.returncode == 0
    assert json.loads(success.stdout)['entries'] == []
    alias = tmp_path / 'alias'
    alias.symlink_to(tmp_path, target_is_directory=True)
    failure = subprocess.run(command + [str(alias)], capture_output=True, text=True, timeout=5)
    assert failure.returncode == 1
    assert failure.stdout == ''
    assert 'inspection incomplete' in failure.stderr

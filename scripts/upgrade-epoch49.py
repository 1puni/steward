#!/usr/bin/env python3
"""Convert a quiescent epoch-49 COPY into a new epoch-50 state directory.

Never edits the source or launches a controller. Inspect the private conversion
report and rehearse runtime behavior before replacing any live state. Configuration,
world/workspace custody and native homes must be handled by the cutover separately.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import sqlite3

import yaml

from steward_harness.state import ConversationId, StateDatabase, TaskId
from steward_harness.task_store import Definition, GitTaskStore, PREFIX


def convert(source: Path, destination: Path) -> dict:
    source = source.resolve()
    destination = destination.resolve()
    if destination.exists() or destination.is_relative_to(source.parent):
        raise ValueError("destination must be a new directory outside source state")
    old = sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)
    old.row_factory = sqlite3.Row
    if old.execute("SELECT epoch FROM steward_schema").fetchone()[0] != 49:
        raise ValueError("source must be epoch 49")
    if old.execute("PRAGMA integrity_check").fetchone()[0] != "ok" or old.execute("PRAGMA foreign_key_check").fetchall():
        raise ValueError("source database failed integrity checks")
    turns = [dict(r) for r in old.execute("SELECT * FROM turns")]
    worlds = {r['event_id']: dict(r) for r in old.execute("SELECT * FROM world_turns")}
    if any(r['state'] == 'running' for r in turns):
        raise ValueError("source has running turns; finish quiescence first")
    if any(r['status'] != 'accepted' for r in worlds.values()):
        raise ValueError("resolve pending world acceptance on the old release first")
    if set(worlds) - {r['turn_id'] for r in turns}:
        raise ValueError("world evidence without an owning turn needs explicit reconciliation")
    if list(source.with_suffix('.world-completions').glob('*.json')):
        raise ValueError("retained native completions need recovery on the old release first")
    lineage = json.loads((source.parent / 'lineage.json').read_text())
    for key, entry in lineage.items():
        ConversationId(key)
        if not isinstance(entry, dict) or not {'provider', 'profile', 'generation'} <= entry.keys():
            raise ValueError("invalid source lineage")

    previous_umask = os.umask(0o077)
    try:
        shutil.copytree(source.parent, destination, symlinks=True)
        # copytree preserves modes and xattrs, but not UID/GID. State may
        # contain shared inbox/lock directories; retain their actual grants.
        if os.geteuid() == 0:
            for directory, dirs, files in os.walk(source.parent):
                for original in (Path(directory), *(Path(directory) / name for name in dirs + files)):
                    metadata = original.lstat()
                    copied = destination / original.relative_to(source.parent)
                    os.chown(copied, metadata.st_uid, metadata.st_gid, follow_symlinks=False)
    finally:
        os.umask(previous_umask)
    db = destination / source.name
    archive = destination / 'epoch49-preserved'
    archive.mkdir()
    for suffix in ('', '-wal', '-shm'):
        file = Path(str(db) + suffix)
        if file.exists():
            file.rename(archive / file.name)
    state = StateDatabase(db)
    report = {'source_epoch': 49, 'target_epoch': 50, 'turns': len(turns),
              'world_records': len(worlds), 'lineages': len(lineage),
              'reconciled_accepted_turns': [], 'tasks': {}, 'preserved_verdicts': 0}
    with state.connect(write=True) as connection:
        for key, entry in lineage.items():
            connection.execute('INSERT INTO conversations VALUES (?,?,?,?,?)',
                (key, entry['provider'], entry['profile'], entry['generation'], entry.get('provider_session_id')))
        for row in turns:
            world = worlds.get(row['turn_id'])
            if world:
                for field in ('world_root', 'base_sha', 'candidate_sha', 'output', 'model',
                              'profile', 'reply_text', 'task_id', 'rejection'):
                    row[field] = world[field]
                row['episode_input'] = row['input_text']
                if row['state'] != 'completed':
                    # Accepted world evidence is stronger than an abandoned
                    # execution marker. Preserve the old marker in the audit.
                    report['reconciled_accepted_turns'].append(dict(row))
                    row.update(state='completed', status_reason=None,
                               provider=world['provider'], generation=world['generation'],
                               provider_session_id=world['provider_session_id'])
                    row['completed_at'] = row['completed_at'] or world['created_at']
            elif row['state'] == 'completed':
                raise ValueError("completed turn without retained reply needs explicit reconciliation")
            columns = ','.join(row)
            connection.execute(f"INSERT INTO turns ({columns}) VALUES ({','.join('?' for _ in row)})", tuple(row.values()))
        for raw in old.execute('SELECT * FROM incidents'):
            row = dict(raw)
            connection.execute(f"INSERT INTO incidents ({','.join(row)}) VALUES ({','.join('?' for _ in row)})",tuple(row.values()))
    store = state.tasks
    original_refs = store.refs()
    for ref, sha in original_refs.items():
        task_id = TaskId(ref.removeprefix(PREFIX))
        raw = store.git('show', f'{sha}:task.md')
        header, body = raw[4:].split('\n---\n', 1)
        fields = yaml.safe_load(header)
        publication = fields.pop('publication', None)
        procedure = fields.get('procedure')
        verdict = procedure.pop('result', None) if procedure else None
        work = fields.get('work')
        parents = ()
        if verdict is not None:
            if verdict not in {'pass', 'fail'} or not work:
                raise ValueError(f"task {task_id} has an unrepresentable accepted verdict")
            message = store.git('show', '-s', '--format=%B', work)
            lines = [line.strip() for line in message.splitlines() if line.startswith('VERDICT:')]
            if lines != ['VERDICT: ' + verdict.upper()]:
                tree = store.git('rev-parse', work + '^{tree}')
                converted_work = store.git('commit-tree', tree, '-p', work, input_text=(
                    'steward: preserve accepted review verdict during epoch-50 conversion\n\n'
                    f'VERDICT: {verdict.upper()}\n\nDisposition: idle\n'))
                fields['work'] = converted_work
                parents = (converted_work,)
                report['preserved_verdicts'] += 1
        definition = Definition.model_validate(fields)
        new = store._commit(task_id, sha, definition, body,
                            'convert accepted task representation to epoch 50', parents=parents)
        task = store.get(task_id)
        if verdict is not None and task.verdict != verdict:
            raise ValueError(f"task {task_id} verdict changed")
        assert store.contains(sha, new)
        assert task.brief == body.strip()
        report['tasks'][str(task_id)] = dict(before=sha, after=new, publication=publication,
                                            verdict=verdict, original_work=work)
    with state.connect() as connection:
        assert connection.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert not connection.execute('PRAGMA foreign_key_check').fetchall()
        assert connection.execute('SELECT count(*) FROM turns').fetchone()[0] == len(turns)
        for key, entry in lineage.items():
            row = connection.execute('SELECT * FROM conversations WHERE conversation_id=?',(key,)).fetchone()
            assert all(row[field] == entry.get(field) for field in ('provider','profile','generation','provider_session_id'))
    assert len(store.all()) == len(original_refs)
    old.close()
    report['complete'] = True
    audit = destination / 'epoch50-conversion.json'
    audit.write_text(json.dumps(report,indent=2)+'\n')
    audit.chmod(0o600)
    return {key: len(value) if isinstance(value,(dict,list)) else value for key,value in report.items()}


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True, help='Quiescent copied state.db')
    parser.add_argument('--destination', type=Path, required=True, help='New directory; must not exist')
    args=parser.parse_args()
    print(json.dumps(convert(args.source,args.destination),sort_keys=True))

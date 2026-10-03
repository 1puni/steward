"""Privacy and association checks using the installed schema, never live evidence."""
import importlib.util
import json
from pathlib import Path
import sqlite3
from unittest.mock import patch

from steward_harness.state import _DDL

SPEC = importlib.util.spec_from_file_location(
    'topic_export', Path(__file__).parents[1] / 'scripts/export-topic-evidence.py')
export = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(export)
SECRET = 'SECRET_BODY_AND_CREDENTIAL'


def test_receipt_whitelist_and_confirmed_ids(tmp_path):
    path = tmp_path / '19352392.json'
    data = {'update': {'update_id': 19352392, 'token': SECRET, 'message': {
        'date': int(export.START), 'message_id': 32, 'chat': {'id': -100, 'title': SECRET},
        'text': SECRET, 'message_thread_id': 12, 'is_topic_message': True,
        'reply_to_message': {'message_id': 30, 'chat': {'id': -100}, 'text': SECRET}}},
        'reply': [-100, 12, SECRET], 'formatted_chunks': [SECRET],
        'pieces': {'text:0': 44, 'artifact:0': 45, 'pin:44': True,
                   'text:1': False, 'text:2': SECRET, SECRET: 100}}
    path.write_text(json.dumps(data))
    result = export.receipt(path, 19352392)
    assert SECRET not in json.dumps(result)
    assert result['incoming']['reply_to_message']['message_id'] == 30
    assert result['incoming']['is_topic_message'] is True
    assert [r['message_id'] for r in result['confirmed_outbound']] == [44, 45]
    assert result['confirmed_outbound'][0]['response_message_thread_id'] == export.status('never-recorded')
    data['update']['update_id'] = 999
    path.write_text(json.dumps(data))
    assert export.receipt(path, 19352392) == export.status('identity_mismatch')
    data['update']['update_id'] = 19352392
    data['update']['message']['date'] = int(export.END) + 1
    path.write_text(json.dumps(data))
    assert export.receipt(path, 19352392) == export.status('outside_window_or_missing_date')


def test_absent_is_not_pruned_and_access_errors_are_redacted(tmp_path):
    path = tmp_path / 'missing'
    assert export.receipt(path, 19352392) == export.status('absent')
    with patch.object(Path, 'read_text', side_effect=PermissionError(SECRET)):
        assert export.receipt(path, 19352392) == export.status('inaccessible')
    path.write_text(SECRET)
    assert export.receipt(path, 19352392) == export.status('unreadable')


def test_database_wal_snapshot_filters_pairs_and_does_not_change_live_files(tmp_path):
    path = tmp_path / 'state.db'
    connection = sqlite3.connect(path)
    connection.execute('PRAGMA journal_mode=WAL')
    for ddl in _DDL:
        connection.execute(ddl)
    for uid, turn in export.PAIRS.items():
        connection.execute(
            'INSERT INTO turns (turn_id, conversation_id, source_event_key, operator_id, '
            'state, input_text, provider_session_id, started_at, provider, generation, completed_at, reply_text) '
            'VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
            (turn, 'telegram:0', f'tg_{uid}', SECRET, 'completed', SECRET, 'session-123',
             '2026-10-03T17:13:00+00:00' if uid == 19352392 else '2026-10-03T17:19:00+00:00',
             'codex', 1, '2026-10-03T17:20:00+00:00', SECRET))
    connection.execute('INSERT INTO conversations VALUES (?,?,?,?,?)',
                       ('telegram:0', 'codex', 'deep', 1, 'session-456'))
    connection.execute('INSERT INTO conversations VALUES (?,?,?,?,?)',
                       ('telegram:999', 'codex', 'deep', 1, SECRET))
    connection.commit()
    files = list(tmp_path.iterdir())
    before = {p.name: p.read_bytes() for p in files}
    result = export.database(path)
    assert result['19352392']['provider_session_id'] == 'session-123'
    assert result['19352392']['current_lineage']['provider_session_id'] == 'session-456'
    assert result['19352394'] == export.status('absent_or_outside_window')
    assert SECRET not in json.dumps(result)
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == before
    connection.execute('UPDATE turns SET source_event_key=? WHERE turn_id=?',
                       ('tg_999', export.PAIRS[19352392]))
    connection.commit()
    assert export.database(path)['19352392'] == export.status('absent_or_outside_window')
    connection.close()


def test_journal_exact_ids_time_format_and_redaction(tmp_path):
    prefix = '2026-10-03 17:13:00,000 INFO steward_harness.telegram.service '
    ingress = ('Telegram ingress update=19352392 chat=-100 message=32 sender=9 '
               'sender_is_bot=False thread_present=False wire_thread=None routed_topic=0')
    outgoing = (f'Telegram reply confirmed chat=-100 topic=0 message=44 '
                f'receipt={tmp_path}/19352392.json piece=0')
    messages = [prefix + ingress, prefix + outgoing, ingress.replace('19352392', '999'),
                outgoing.replace('19352392', '999'), outgoing + SECRET, SECRET,
                outgoing.replace(str(tmp_path), '/unrelated')]
    rows = [json.dumps({'__REALTIME_TIMESTAMP': str(int(export.START * 1_000_000)),
                        'MESSAGE': message, 'CREDENTIAL': SECRET}) for message in messages]
    rows.append(json.dumps({'__REALTIME_TIMESTAMP': str(int((export.END + 1) * 1_000_000)),
                            'MESSAGE': ingress}))
    result = export.journal_rows(rows, tmp_path)
    assert len(result) == 2
    assert result[0]['update_id'] == 19352392
    assert result[1]['message_id'] == 44
    assert SECRET not in json.dumps(result)
    assert 'sender' not in json.dumps(result)


def test_journal_permission_warning_is_not_empty_success(tmp_path):
    import subprocess
    with patch.object(export.subprocess, 'run', return_value=subprocess.CompletedProcess(
            [], 0, '', 'You are not seeing messages from other users. ' + SECRET)):
        assert export.journal('fixture.service', tmp_path) == export.status('inaccessible_or_incomplete')


def test_database_rejects_unstable_copy_and_denied_access(tmp_path):
    path = tmp_path / 'state.db'
    path.write_bytes(b'fixture')
    with patch.object(export, 'stamp', side_effect=[(1,), None, None, (2,), None, None]):
        assert export.database(path) == export.status('changed_during_copy')
    with patch.object(export, 'stamp', side_effect=PermissionError(SECRET)):
        assert export.database(path) == export.status('inaccessible')
    Path(str(path) + '-journal').write_bytes(b'fixture')
    assert export.database(path) == export.status('busy_or_rollback_journal')


def test_cli_only_emits_projected_json(tmp_path, capsys):
    directory = tmp_path / 'missing.db.telegram-receipts'
    directory.mkdir()
    (directory / '19352394.json').write_text(json.dumps({
        'reply': [-100, 0, SECRET], 'pieces': {'text:0': 50}, 'done': True}))
    # A reply with no positive confirmation must not be exported as delivered.
    (directory / '19352392.json').write_text(json.dumps({
        'reply': [-100, 0, SECRET], 'pieces': {}, 'done': True}))
    with patch('sys.argv', ['export', '--db', str(tmp_path / 'missing.db'),
                            '--unit', 'fixture.service']), patch.object(
            export.subprocess, 'run', return_value=export.subprocess.CompletedProcess([], 0, '', '')):
        export.main()
    captured = capsys.readouterr()
    assert SECRET not in captured.out + captured.err
    result = json.loads(captured.out)
    assert result['attribution'] == export.ATTRIBUTION
    assert result['receipts']['tg_19352392']['confirmed_outbound'] == []
    assert result['receipts']['tg_19352394']['confirmed_outbound'][0]['message_id'] == 50

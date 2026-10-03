#!/usr/bin/env python3
"""Bounded, metadata-only operator export. See docs/topic-evidence-export.md."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3
import shutil
import subprocess
import tempfile

PAIRS = {
    19352392: 'turn_9116c804cec9444eb9256e466742063d',
    19352394: 'turn_96154a271b9448f4a08eb921cba6c31a',
}
START = datetime(2026, 10, 3, 17, 12, tzinfo=timezone.utc).timestamp()
END = datetime(2026, 10, 3, 17, 18, tzinfo=timezone.utc).timestamp()
ATTRIBUTION = 'task:task-6fc1029e3928576bba0dbefcde301a2a'


def status(value):
    return {'availability': value}


def field(obj, key, kind=int):
    value = obj.get(key)
    if value is None:
        return status('absent')
    if type(value) is not kind:
        return status('invalid')
    if kind is str and not re.fullmatch(r'[A-Za-z0-9_:\-]{1,128}', value):
        return status('invalid')
    return value


def message_ids(message):
    if not isinstance(message, dict):
        return status('absent')
    chat = message.get('chat')
    result = {key: field(message, key) for key in ('message_id', 'message_thread_id')}
    result['chat_id'] = field(chat if isinstance(chat, dict) else {}, 'id')
    result['is_topic_message'] = field(message, 'is_topic_message', bool)
    return result


def receipt(path, uid):
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return status('absent')  # Pruning cannot be proved by a missing file.
    except PermissionError:
        return status('inaccessible')
    except (OSError, ValueError):
        return status('unreadable')
    if not isinstance(data, dict):
        return status('invalid')
    result = {'receipt_id': f'{uid}.json'}
    update = data.get('update')
    if update is None:
        result['incoming'] = status('absent')
    elif not isinstance(update, dict) or type(update.get('update_id')) is not int or update['update_id'] != uid:
        return status('identity_mismatch')
    else:
        message = update.get('message')
        if not isinstance(message, dict):
            result['incoming'] = status('absent')
        elif type(message.get('date')) is not int or not START <= message['date'] <= END:
            return status('outside_window_or_missing_date')
        else:
            result['incoming'] = {'update_id': uid, **message_ids(message),
                                  'reply_to_message': message_ids(message.get('reply_to_message')),
                                  'external_reply': message_ids(message.get('external_reply'))}
    reply, pieces = data.get('reply'), data.get('pieces', {})
    result['confirmed_outbound'] = []
    # A frozen reply without a confirmed piece is not evidence of delivery.
    if isinstance(reply, list) and len(reply) == 3 and type(reply[0]) is int and type(reply[1]) is int and isinstance(pieces, dict):
        for key, value in pieces.items():
            if re.fullmatch(r'(text|artifact):[0-9]+', key) and type(value) is int and value > 0:
                result['confirmed_outbound'].append({
                    'receipt_id': f'{uid}.json', 'piece_id': key,
                    'chat_id': reply[0], 'requested_topic_id': reply[1], 'message_id': value,
                    'response_message_thread_id': status('never-recorded'),
                    'confirmation_time': status('never-recorded'),
                })
    return result


def stamp(path):
    try:
        info = path.stat()
        return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns
    except FileNotFoundError:
        return None


def database(path):
    """Never open live SQLite: copy only after observing a stable file set.

    Reject concurrent changes, including WAL checkpoints, rather than claiming
    a consistent capture. SQLite may create SHM only inside the private temp dir.
    """
    paths = [path, Path(str(path) + '-wal'), Path(str(path) + '-journal')]
    try:
        before = [stamp(p) for p in paths]
        if before[0] is None:
            return status('absent')
        if before[2] is not None:
            return status('busy_or_rollback_journal')
        with tempfile.TemporaryDirectory(prefix='topic-evidence-') as temp:
            copied = Path(temp) / 'state.db'
            for original, target, observed in zip(paths[:2], [copied, Path(str(copied) + '-wal')], before[:2]):
                if observed is not None:
                    # Plain file reads do not acquire SQLite locks or touch live SHM.
                    with original.open('rb') as source, target.open('wb') as destination:
                        shutil.copyfileobj(source, destination)
            if before != [stamp(p) for p in paths]:
                return status('changed_during_copy')
            connection = sqlite3.connect(copied.as_uri() + '?mode=ro', uri=True)
            try:
                connection.execute('PRAGMA query_only=ON')
                connection.row_factory = sqlite3.Row
                result = {}
                for uid, turn in PAIRS.items():
                    row = connection.execute(
                        'SELECT turn_id, source_event_key, conversation_id, execution_turn_id, '
                        'provider_session_id FROM turns WHERE turn_id=? AND source_event_key=? '
                        'AND unixepoch(started_at) BETWEEN ? AND ?',
                        (turn, f'tg_{uid}', START, END)).fetchone()
                    if row is None:
                        result[str(uid)] = status('absent_or_outside_window')
                        continue
                    result[str(uid)] = {key: field(dict(row), key, str) for key in row.keys()}
                    lineage = connection.execute(
                        'SELECT conversation_id, provider_session_id FROM conversations WHERE conversation_id=?',
                        (row['conversation_id'],)).fetchone()
                    result[str(uid)]['current_lineage'] = (
                        {key: field(dict(lineage), key, str) for key in lineage.keys()}
                        if lineage else status('absent'))
                return result
            finally:
                connection.close()
    except PermissionError:
        return status('inaccessible')
    except (OSError, sqlite3.Error):
        return status('unreadable_or_schema_mismatch')


INGRESS = re.compile(
    r'Telegram ingress update=(\d+) chat=(-?\d+) message=(\d+) sender=-?\d+ '
    r'sender_is_bot=(?:True|False|None) thread_present=(True|False) '
    r'wire_thread=(None|-?\d+) routed_topic=(-?\d+)')


def journal_rows(lines, receipt_dir):
    outgoing = re.compile(
        r'Telegram reply confirmed chat=(-?\d+) topic=(None|-?\d+) message=(\d+) receipt='
        + re.escape(str(receipt_dir)) + r'/(19352392|19352394)\.json piece=(\d+)')
    result = []
    for line in lines:
        row = json.loads(line)
        timestamp = int(row.get('__REALTIME_TIMESTAMP', 0)) / 1_000_000
        if not START <= timestamp <= END:
            continue
        message = row.get('MESSAGE', '')
        if not isinstance(message, str):
            continue
        message = re.sub(
            r'^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3} INFO '
            r'steward_harness\.telegram\.service ', '', message)
        match = INGRESS.fullmatch(message)
        if match and int(match[1]) in PAIRS:
            result.append({'update_id': int(match[1]), 'chat_id': int(match[2]),
                           'message_id': int(match[3]),
                           'message_thread_id': int(match[5]) if match[5] != 'None' else status('absent'),
                           'routed_topic_id': int(match[6]),
                           'is_topic_message': status('never-recorded'),
                           'reply_identifiers': status('never-recorded')})
        match = outgoing.fullmatch(message)
        if match and int(match[3]) > 0:
            result.append({'receipt_id': f'{match[4]}.json', 'chat_id': int(match[1]),
                           'requested_topic_id': int(match[2]) if match[2] != 'None' else None,
                           'message_id': int(match[3]), 'piece_id': f'text:{match[5]}',
                           'response_message_thread_id': status('never-recorded')})
    return result


def journal(unit, receipt_dir):
    try:
        run = subprocess.run(['journalctl', '--unit', unit, '--since', '2026-10-03 17:12:00 UTC',
                              '--until', '2026-10-03 17:18:00 UTC', '--output=json', '--no-pager'],
                             capture_output=True, text=True, check=False)
        if run.returncode or run.stderr.strip():
            return status('inaccessible_or_incomplete')
        return {'records': journal_rows(run.stdout.splitlines(), receipt_dir),
                'unmatched_or_missing_fields': status('absent'),
                'retention': status('unknown')}
    except (OSError, ValueError, TypeError):
        return status('unreadable')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', required=True, type=Path, help='Installed provider.state_db path')
    parser.add_argument('--unit', required=True, help='Installed controller systemd unit')
    args = parser.parse_args()
    db = args.db.absolute()
    receipts = db.with_name(db.name + '.telegram-receipts')
    result = {
        'attribution': ATTRIBUTION,
        'scope': '2026-10-03 17:12:00 through 17:18:00 UTC inclusive; fixed source/turn pairs',
        'database': database(db),
        'receipts': {f'tg_{uid}': receipt(receipts / f'{uid}.json', uid) for uid in PAIRS},
        'journal': journal(args.unit, receipts),
    }
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == '__main__':
    main()

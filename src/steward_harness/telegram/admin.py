"""Opt-in cosmetics for the configured chat; the controller retains the token."""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import threading
from pathlib import Path

from steward_harness.receipts import write_receipt


def read_photo(path, roots):
    """Open every component without following links, then read bounded bytes.

    Agent-writable parents may change concurrently; descriptor-relative traversal
    prevents a rename or symlink swap from turning this into a privileged read.
    """
    candidate = Path(path)
    if not candidate.is_absolute() or '..' in candidate.parts:
        raise ValueError('photo must be an absolute path without parent traversal')
    if not any(candidate.is_relative_to(Path(root)) for root in roots):
        raise ValueError('photo is outside configured delivery_roots')
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in candidate.parts[1:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        image_fd = os.open(candidate.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        with os.fdopen(image_fd, 'rb') as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError('photo must be a regular file')
            data = stream.read(5 * 1024 * 1024 + 1)
    except OSError:
        raise ValueError('photo cannot be read without following links') from None
    finally:
        os.close(fd)
    if len(data) > 5 * 1024 * 1024:
        raise ValueError('photo exceeds 5 MB')
    if not (data.startswith(b'\x89PNG\r\n\x1a\n') or data.startswith(b'\xff\xd8\xff')):
        raise ValueError('photo must be PNG or JPEG')
    return data


class ChatAdministration:
    def __init__(self, config, api, receipts):
        self.config, self.api, self.receipts = config, api, Path(receipts)
        self.lock = threading.Lock()

    def info(self):
        chat = self.api.get_chat(self.config.chat_id)
        member = self.api.get_chat_member(self.config.chat_id, self.api.get_me()['id'])
        return dict(chat_id=chat['id'], title=chat.get('title'),
                    description=chat.get('description', ''),
                    photo_id=(chat.get('photo') or {}).get('big_file_unique_id'),
                    can_change_info=member.get('status') == 'creator' or member.get('can_change_info') is True,
                    enabled_actions=[a for a in self.config.agent_actions if a.startswith('set_')])

    def __call__(self, scope, request):
        action, key, text = (request.get(k) for k in ('action', 'key', 'text'))
        if (not all(isinstance(v, str) for v in (action, key, text))
                or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', key)):
            raise ValueError('telegram requires string action, text and a bounded stable key')
        if action not in {'info', 'set_photo', 'set_description'}:
            raise ValueError('unknown Telegram action')
        enabled = set(self.config.agent_actions) & {'set_photo', 'set_description'}
        if not enabled or (action != 'info' and action not in enabled):
            raise ValueError('Telegram cosmetic action is not enabled')
        if action == 'info':
            if text:
                raise ValueError('info requires empty text')
            return dict(operation='telegram', accepted=True, **self.info())
        if action == 'set_description' and len(text) > 255:
            raise ValueError('Telegram description exceeds 255 characters')
        photo = read_photo(text, self.config.delivery_roots) if action == 'set_photo' else None
        fingerprint = dict(action=action, text=text,
                           photo_sha256=hashlib.sha256(photo).hexdigest() if photo else None)
        identity = hashlib.sha256(json.dumps([scope, key]).encode()).hexdigest()
        path = self.receipts / (identity + '.json')
        with self.lock:
            prior = json.loads(path.read_text()) if path.exists() else None
            if prior:
                if prior['request'] != fingerprint:
                    raise ValueError('Telegram action key already used with different content')
                if prior.get('result'):
                    return prior['result'] | {'replayed': True}
                # A request may have reached Telegram; never blindly repeat an
                # ambiguous photo upload. A fresh info call supports reconciliation.
                raise ValueError('Telegram action outcome needs reconciliation; inspect info before another mutation')
            before = self.info()
            if not before['can_change_info']:
                raise ValueError('bot lacks Telegram Change Group Info permission')
            self.receipts.mkdir(parents=True, exist_ok=True, mode=0o700)
            receipt = dict(request=fingerprint, before=before)
            write_receipt(path, receipt)
            if action == 'set_description':
                if before['description'] != text:
                    self.api._request('POST', 'setChatDescription', json={'chat_id': self.config.chat_id, 'description': text})
            else:
                self.api._request('POST', 'setChatPhoto', data={'chat_id': str(self.config.chat_id)},
                                  files={'photo': ('photo.png' if photo.startswith(b'\x89PNG') else 'photo.jpg', photo)}, timeout=60)
            after = self.info()
            if (action == 'set_description' and after['description'] != text
                    or action == 'set_photo' and not after['photo_id']):
                raise RuntimeError('Telegram accepted mutation but read-back did not verify it')
            result = dict(operation='telegram', accepted=True, replayed=False, action=action,
                          receipt=identity, **after)
            receipt['result'] = result
            write_receipt(path, receipt)
            return result

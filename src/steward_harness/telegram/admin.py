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


def resolve_delivery_file(raw_path, roots, max_bytes):
    """An existing file beneath a delivery root, within the configured size cap."""
    try:
        artifact = Path(raw_path).resolve(strict=True)
    except (OSError, ValueError) as exc:
        raise ValueError(f'could not resolve delivery file: {exc}') from None
    if not artifact.is_file():
        raise ValueError(f'delivery file is not a file: {artifact}')
    if not any(artifact.is_relative_to(Path(root).resolve()) for root in roots):
        raise ValueError('delivery file is outside configured delivery_roots')
    size = artifact.stat().st_size
    if size > max_bytes:
        raise ValueError(f'{artifact.name} is {size // (1024 * 1024)}MB; '
                         f'configured Telegram cap is {max_bytes // (1024 * 1024)}MB')
    return artifact


#: Sent to the calling conversation's own topic, at once, before its reply.
_CONVERSATION_ACTIONS = {'send_image', 'send_video', 'send_voice', 'send_document', 'pin_message'}


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
                    enabled_actions=sorted(self._enabled()))

    def _enabled(self):
        sends = {'send_image', 'send_video', 'send_voice', 'send_document'} if self.config.delivery_roots else set()
        return sends | (set(self.config.agent_actions) & {'set_photo', 'set_description', 'pin_message'})

    def __call__(self, scope, request):
        action, key, text = (request.get(k) for k in ('action', 'key', 'text'))
        if (not all(isinstance(v, str) for v in (action, key, text))
                or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', key)):
            raise ValueError('telegram requires string action, text and a bounded stable key')
        if action not in {'info', 'set_photo', 'set_description', *_CONVERSATION_ACTIONS}:
            raise ValueError('unknown Telegram action')
        enabled = self._enabled()
        if not enabled or (action != 'info' and action not in enabled):
            raise ValueError(f'Telegram action {action!r} is not enabled')
        if action in _CONVERSATION_ACTIONS:
            return self._converse(scope, action, key, text)
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

    def _converse(self, scope, action, key, text):
        kind, _, topic = scope.partition(':')
        if kind != 'telegram' or not topic.isdigit():
            raise ValueError(f'{action} is available only to a Telegram conversation')
        if action == 'pin_message':
            if not text.isdigit() or not 0 < int(text) <= 2_147_483_647:
                raise ValueError('pin_message requires the numeric id of a message in this chat')
            fingerprint = dict(action=action, message_id=int(text))
        else:
            path = resolve_delivery_file(text, self.config.delivery_roots,
                                         self.config.media_max_mb * 1024 * 1024)
            if action == 'send_voice' and path.suffix.lower() not in {'.ogg', '.mp3', '.m4a'}:
                raise ValueError('send_voice requires MP3, M4A or OGG/Opus; convert WAV before sending')
            fingerprint = dict(action=action, path=str(path), size=path.stat().st_size)
        identity = hashlib.sha256(json.dumps([scope, key]).encode()).hexdigest()
        receipt_path = self.receipts / (identity + '.json')
        with self.lock:
            prior = json.loads(receipt_path.read_text()) if receipt_path.exists() else None
            if prior:
                if prior['request'] != fingerprint:
                    raise ValueError('Telegram action key already used with different content')
                if prior.get('result'):
                    return prior['result'] | {'replayed': True}
                if action != 'pin_message':
                    # Pinning twice is harmless; a second upload is a duplicate.
                    raise ValueError('Telegram send outcome is unknown and may have been delivered; '
                                     'use a new key only if a duplicate is acceptable')
            self.receipts.mkdir(parents=True, exist_ok=True, mode=0o700)
            write_receipt(receipt_path, dict(request=fingerprint))
            chat, thread = self.config.chat_id, int(topic)
            try:
                if action == 'pin_message':
                    self.api.pin_chat_message(chat, int(text))
                    message_id = int(text)
                elif action == 'send_image':
                    message_id = self.api.send_photo(chat, path, topic_id=thread)
                elif action == 'send_video':
                    message_id = self.api.send_video(chat, path, topic_id=thread)
                elif action == 'send_voice':
                    message_id = self.api.send_voice(chat, path, topic_id=thread)
                else:
                    message_id = self.api.send_document(chat, path, topic_id=thread)
            except Exception as error:
                answered = getattr(error, 'status_code', None)
                if answered is not None and answered < 500:
                    # Telegram answered and refused: nothing was delivered.
                    receipt_path.unlink()
                    raise ValueError(f'Telegram refused {action}: {error}') from None
                raise
            result = dict(operation='telegram', accepted=True, replayed=False, action=action,
                          message_id=message_id, receipt=identity)
            write_receipt(receipt_path, dict(request=fingerprint, result=result))
            return result

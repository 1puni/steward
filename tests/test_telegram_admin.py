from pathlib import Path
from types import SimpleNamespace
import pytest
from steward_harness.telegram.admin import ChatAdministration, read_photo
from steward_harness.telegram.api import TelegramAPIError


class API:
    def __init__(self):
        self.description = ''
        self.photo = None
        self.calls = []
        self.allowed = True
    def get_me(self):
        return {'id': 22}
    def get_chat_member(self, chat, user):
        return {'status': 'administrator', 'can_change_info': self.allowed}
    def get_chat(self, chat):
        return dict(id=chat, title='LLMPsych', description=self.description,
                    photo={'big_file_unique_id': self.photo})
    def send_photo(self, chat, path, topic_id=None):
        self.calls.append(('sendPhoto', chat, str(path), topic_id))
        return 501
    def send_document(self, chat, path, topic_id=None):
        self.calls.append(('sendDocument', chat, str(path), topic_id))
        return 502
    def send_video(self, chat, path, topic_id=None):
        self.calls.append(('sendVideo', chat, str(path), topic_id))
        return 503
    def send_voice(self, chat, path, topic_id=None):
        self.calls.append(('sendVoice', chat, str(path), topic_id))
        return 504
    def pin_chat_message(self, chat, message_id):
        self.calls.append(('pinChatMessage', chat, message_id))
    def _request(self, method, endpoint, **kwargs):
        self.calls.append((endpoint, kwargs))
        if endpoint == 'setChatDescription':
            self.description = kwargs['json']['description']
        if endpoint == 'setChatPhoto':
            self.photo = 'new-photo'
        return True


def setup(tmp_path, actions=('set_photo', 'set_description')):
    root = tmp_path / 'delivery'
    root.mkdir()
    api = API()
    config = SimpleNamespace(chat_id=-123, agent_actions=actions, delivery_roots=(str(root),),
                             media_max_mb=1)
    return ChatAdministration(config, api, tmp_path / 'receipts'), api, root


def request(action, text='', key='cosmetic'):
    return dict(operation='telegram', action=action, key=key, text=text)


def test_description_verified_and_replayed_without_mutation(tmp_path):
    admin, api, _ = setup(tmp_path)
    result = admin('task:one', request('set_description', 'Research workspace'))
    assert result['accepted'] and result['description'] == 'Research workspace'
    assert admin('task:one', request('set_description', 'Research workspace'))['replayed']
    assert len(api.calls) == 1
    assert api.calls[0][1]['json']['chat_id'] == -123
    with pytest.raises(ValueError, match='different content'):
        admin('task:one', request('set_description', 'Other'))


def test_photo_verified_and_root_bounded(tmp_path):
    admin, api, root = setup(tmp_path)
    photo = root / 'avatar.png'
    photo.write_bytes(b'\x89PNG\r\n\x1a\nimage')
    assert admin('task:one', request('set_photo', str(photo)))['photo_id'] == 'new-photo'
    assert len(api.calls) == 1
    with pytest.raises(ValueError, match='outside'):
        admin('task:two', request('set_photo', str(tmp_path / 'secret.png')))
    (root / 'link.png').symlink_to(photo)
    with pytest.raises(ValueError, match='following links'):
        read_photo(str(root / 'link.png'), (str(root),))
    (root / 'parent').symlink_to(root, target_is_directory=True)
    with pytest.raises(ValueError, match='following links'):
        read_photo(str(root / 'parent' / 'avatar.png'), (str(root),))


def test_opt_in_and_bot_permission_are_required(tmp_path):
    admin, api, _ = setup(tmp_path, ())
    admin.config.delivery_roots = ()
    with pytest.raises(ValueError, match='not enabled'):
        admin('task:one', request('info'))
    admin.config.agent_actions = ('set_description',)
    api.allowed = False
    with pytest.raises(ValueError, match='Change Group Info'):
        admin('task:one', request('set_description', 'No'))
    assert api.calls == []


def test_unknown_photo_outcome_never_blindly_repeated(tmp_path):
    admin, api, root = setup(tmp_path)
    photo = root / 'avatar.png'
    photo.write_bytes(b'\x89PNG\r\n\x1a\nimage')
    def lost(*args, **kwargs):
        api.calls.append('ambiguous')
        raise RuntimeError('lost reply')
    api._request = lost
    with pytest.raises(RuntimeError):
        admin('task:one', request('set_photo', str(photo)))
    with pytest.raises(ValueError, match='reconciliation'):
        admin('task:one', request('set_photo', str(photo)))
    assert api.calls == ['ambiguous']


def test_conversation_sends_a_file_to_its_own_topic_once(tmp_path):
    admin, api, root = setup(tmp_path, ())
    photo = root / 'chart.png'
    photo.write_bytes(b'image')
    result = admin('telegram:7', request('send_image', str(photo), key='chart'))
    assert (result['accepted'], result['message_id']) == (True, 501)
    assert admin('telegram:7', request('send_image', str(photo), key='chart'))['replayed']
    assert api.calls == [('sendPhoto', -123, str(photo), 7)]
    with pytest.raises(ValueError, match='different content'):
        admin('telegram:7', request('send_document', str(photo), key='chart'))


def test_a_video_is_sent_to_play_in_the_chat_not_to_download(tmp_path):
    admin, api, root = setup(tmp_path, ())
    video = root / 'trailer.mp4'
    video.write_bytes(b'mp4')
    assert 'send_video' in admin.info()['enabled_actions']
    result = admin('telegram:914', request('send_video', str(video), key='trailer'))
    assert result['message_id'] == 503
    assert api.calls == [('sendVideo', -123, str(video), 914)]


@pytest.mark.parametrize('suffix', ['.mp3', '.m4a', '.ogg'])
def test_voice_note_is_topic_bound_and_replay_does_not_upload_twice(tmp_path, suffix):
    admin, api, root = setup(tmp_path, ())
    voice = root / ('hello' + suffix)
    voice.write_bytes(b'audio')
    assert 'send_voice' in admin.info()['enabled_actions']
    result = admin('telegram:7', request('send_voice', str(voice), key='hello'))
    assert result['message_id'] == 504
    assert admin('telegram:7', request('send_voice', str(voice), key='hello'))['replayed']
    assert api.calls == [('sendVoice', -123, str(voice), 7)]


def test_voice_refuses_wav_and_private_or_oversize_files_before_upload(tmp_path):
    admin, api, root = setup(tmp_path, ())
    wav = root / 'hello.wav'
    wav.write_bytes(b'audio')
    private = tmp_path / 'private.mp3'
    private.write_bytes(b'secret')
    link = root / 'escape.mp3'
    link.symlink_to(private)
    big = root / 'big.mp3'
    big.write_bytes(b'x' * (1024 * 1024 + 1))
    for path, reason in ((wav, 'convert WAV'), (private, 'outside'),
                         (link, 'outside'), (big, 'cap is 1MB')):
        with pytest.raises(ValueError, match=reason):
            admin('telegram:7', request('send_voice', str(path)))
    mp3 = root / 'hello.mp3'
    mp3.write_bytes(b'audio')
    with pytest.raises(ValueError, match='only to a Telegram conversation'):
        admin('task:one', request('send_voice', str(mp3)))
    assert api.calls == []


def test_send_refuses_what_it_cannot_deliver_before_reaching_telegram(tmp_path):
    """The call answers the model, so a bad path can never hold a conversation."""
    admin, api, root = setup(tmp_path, ())
    (tmp_path / 'secret').write_bytes(b'secret')
    (root / 'big.bin').write_bytes(b'x' * (1024 * 1024 + 1))
    for path, reason in ((tmp_path / 'secret', 'outside'), (root / 'missing.png', 'could not resolve'),
                         (root / 'big.bin', 'cap is 1MB')):
        with pytest.raises(ValueError, match=reason):
            admin('telegram:7', request('send_document', str(path)))
    (root / 'file.txt').write_text('ok')
    with pytest.raises(ValueError, match='only to a Telegram conversation'):
        admin('task:one', request('send_document', str(root / 'file.txt')))
    assert api.calls == []


@pytest.mark.parametrize('action,suffix', [('send_document', '.pdf'), ('send_voice', '.mp3')])
def test_a_refused_send_can_be_retried_but_a_lost_one_is_never_repeated(tmp_path, action, suffix):
    admin, api, root = setup(tmp_path, ())
    document = root / ('report' + suffix)
    document.write_bytes(b'pdf')
    def refused(*args, **kwargs):
        raise TelegramAPIError('Bad Request: topic closed', status_code=400)
    setattr(api, action, refused)
    with pytest.raises(ValueError, match='refused ' + action):
        admin('telegram:7', request(action, str(document)))
    setattr(api, action, lambda chat, path, topic_id=None: 9)
    assert admin('telegram:7', request(action, str(document)))['message_id'] == 9
    def lost(*args, **kwargs):
        raise TelegramAPIError('connection reset')
    setattr(api, action, lost)
    with pytest.raises(TelegramAPIError):
        admin('telegram:7', request(action, str(document), key='again'))
    with pytest.raises(ValueError, match='may have been delivered'):
        admin('telegram:7', request(action, str(document), key='again'))


def test_pin_message_is_opt_in_and_safe_to_repeat(tmp_path):
    admin, api, _ = setup(tmp_path, ())
    with pytest.raises(ValueError, match="'pin_message' is not enabled"):
        admin('telegram:7', request('pin_message', '42'))
    admin.config.agent_actions = ('pin_message',)
    with pytest.raises(ValueError, match='numeric id'):
        admin('telegram:7', request('pin_message', 'latest'))
    def lost(*args, **kwargs):
        raise TelegramAPIError('timed out')
    api.pin_chat_message = lost
    with pytest.raises(TelegramAPIError):
        admin('telegram:7', request('pin_message', '42'))
    api.pin_chat_message = lambda chat, message_id: api.calls.append(('pinChatMessage', chat, message_id))
    assert admin('telegram:7', request('pin_message', '42'))['message_id'] == 42
    assert api.calls == [('pinChatMessage', -123, 42)]

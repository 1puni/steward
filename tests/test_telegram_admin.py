from pathlib import Path
from types import SimpleNamespace
import pytest
from steward_harness.telegram.admin import ChatAdministration, read_photo


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
    config = SimpleNamespace(chat_id=-123, agent_actions=actions, delivery_roots=(str(root),))
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

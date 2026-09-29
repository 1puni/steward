import pytest

from steward_harness.config.schema import TelegramConfig
from steward_harness.telegram.tasks import app_link


@pytest.mark.parametrize("url", ["https://t.me/steward_bot", "https://t.me/steward_bot/tasks"])
def test_registered_main_and_named_apps_have_task_and_board_links(url):
    config = TelegramConfig(chat_id=1, allowed_users=(7,), task_app_url=url)
    assert app_link(config.task_app_url, "#1234abcd") == url + "?startapp=task_1234abcd"
    assert app_link(config.task_app_url) == url + "?startapp="


@pytest.mark.parametrize("url", ["http://t.me/bot", "https://other.test/app", "https://t.me/bot?start=x", "https://t.me/bot/app#x", "https://t.me/bot/app/extra"])
def test_launch_link_must_be_a_bare_telegram_mini_app_address(url):
    with pytest.raises(ValueError, match="task_app_url"):
        TelegramConfig(chat_id=1, allowed_users=(7,), task_app_url=url)


@pytest.mark.parametrize(("owner", "chat", "expected"), [
    ("telegram:42", -100123456, "https://t.me/c/123456/42"),
    ("telegram:0", -100123456, "https://t.me/c/123456/1"),
    ("desk:42", -100123456, None),
    ("telegram:42", 123456, None),
    (None, -100123456, None),
])
def test_discussion_links_use_only_the_admitted_telegram_forum(owner, chat, expected):
    from steward_harness.telegram.tasks import discussion_link
    assert discussion_link(owner, chat) == expected

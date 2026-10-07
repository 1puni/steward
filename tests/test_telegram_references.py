"""Published turn evidence reaches actual Telegram sends without changing custody."""

import subprocess
from types import SimpleNamespace

import pytest

from test_telegram import _service
from steward_harness.config.schema import StewardConfig
from steward_harness.daemon import StewardDaemon
from steward_harness.git_transport import ControllerGitTransport
from steward_harness.telegram import service as service_module
from steward_harness.telegram.api import TelegramAPIError
from steward_harness.telegram.references import turn_reference_entities

TURN = "turn_" + "a" * 32


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


@pytest.fixture
def published_world(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    git(source, "init", "-q")
    git(source, "config", "user.name", "Author")
    git(source, "config", "user.email", "author@example.test")
    monkeypatch.setenv("GIT_AUTHOR_DATE", "2020-03-04T10:11:12+02:00")
    monkeypatch.setenv("GIT_COMMITTER_DATE", "2021-05-06T07:08:09Z")
    git(source, "commit", "--allow-empty", "-m", f"Preserve the originals\n\nSteward-Turn: {TURN}")
    sha = git(source, "rev-parse", "HEAD")
    world = ControllerGitTransport(tmp_path / "state.db", "world", "https://github.com/example/world.git", "main")
    # Populate the controller's observed history locally; tests never fetch GitHub.
    git(world.git_dir, "fetch", str(source), f"HEAD:{world.remote_ref}")
    return world, source, sha


def test_only_unique_turns_in_observed_published_history_are_linked(published_world):
    world, source, sha = published_world
    refs = git(world.git_dir, "show-ref")
    assert turn_reference_entities(world, TURN) == {
        TURN: ("Preserve the originals · 2020-03-04 10:11 +0200", f"https://github.com/example/world/commit/{sha}")}
    assert git(world.git_dir, "show-ref") == refs
    missing = "turn_" + "b" * 32
    git(source, "commit", "--allow-empty", "-m", f"Not published\n\nSteward-Turn: {missing}")
    git(world.git_dir, "fetch", str(source), "HEAD:refs/steward/candidates/unpublished")
    assert turn_reference_entities(world, missing) == {}
    assert TURN in turn_reference_entities(world, f"{TURN} {missing}")
    git(source, "commit", "--allow-empty", "-m", f"Duplicate\n\nSteward-Turn: {TURN}")
    git(world.git_dir, "fetch", str(source), f"HEAD:{world.remote_ref}")
    assert turn_reference_entities(world, TURN) == {}


def test_unavailable_or_unconfigured_history_does_not_block_text(tmp_path, published_world, monkeypatch):
    world, _source, _sha = published_world
    assert turn_reference_entities(None, TURN) == {}
    empty = ControllerGitTransport(tmp_path / "other.db", "empty", "https://github.com/example/empty.git", "main")
    assert turn_reference_entities(empty, TURN) == {}
    monkeypatch.setattr(world, "observed_tip", lambda: (_ for _ in ()).throw(OSError("unavailable")))
    assert turn_reference_entities(world, TURN) == {}
    # No Git access at all is needed for ordinary messages.
    monkeypatch.setattr(world, "observed_tip", lambda: pytest.fail("unnecessary Git read"))
    assert turn_reference_entities(world, "No references here") == {}


@pytest.mark.parametrize("remote", ["https://example.test/world", "https://secret@github.com/example/world"])
def test_unsupported_world_destinations_stay_literal(published_world, remote):
    world, _source, _sha = published_world
    world.remote_url = remote
    assert turn_reference_entities(world, TURN) == {}


@pytest.mark.parametrize("retained", [False, True])
def test_actual_reply_and_notification_paths_link_only_the_configured_chat(
    tmp_path, published_world, monkeypatch, retained,
):
    world, _source, sha = published_world
    service = _service(tmp_path)
    service._world_transport = world
    sent = []
    monkeypatch.setattr(service.api, "send_message", lambda _chat, text, **kw: sent.append(text) or 1)
    def send(chat):
        if retained:
            service.send_result(chat, 42, f"See {TURN}", f"notify:{chat}")
        else:
            service.send_reply(chat, 42, f"See {TURN}")
    send(1)
    assert f"/commit/{sha}" in sent[0] and "Preserve the originals" in sent[0]
    send(2)
    assert sent[1] == f"See {TURN}"


def test_turn_rendering_is_retained_across_restart_and_history_changes(tmp_path, published_world, monkeypatch):
    world, _source, sha = published_world
    service = _service(tmp_path)
    service._world_transport = world
    monkeypatch.setattr(service_module, "_SEND_RETRY_BACKOFF_SECONDS", 0)
    sent = []
    def send(_chat, text, **_kw):
        if "/commit/" in text:
            raise TelegramAPIError("temporary outage")
        sent.append(text)
        return 1
    monkeypatch.setattr(service.api, "send_message", send)
    text = "x" * 4100 + f"\nSee {TURN}"
    with pytest.raises(TelegramAPIError):
        service.send_result(1, 42, text, "stable-turn-link")
    assert len(sent) == 1
    restarted = _service(tmp_path)
    monkeypatch.setattr(service_module, "turn_reference_entities", lambda *_: pytest.fail("retry re-resolved history"))
    monkeypatch.setattr(restarted.api, "send_message", lambda _chat, text, **kw: sent.append(text) or 2)
    restarted.send_result(1, 42, text, "stable-turn-link")
    assert len(sent) == 2 and f"/commit/{sha}" in sent[1]


def test_daemon_wires_published_world_into_telegram(tmp_path, published_world, monkeypatch):
    world, _source, sha = published_world
    service = _service(tmp_path)
    config = StewardConfig.model_validate({
        "identity": {"name": "Test", "slug": "test"}, "repositories": {},
        "provider": {"state_db": str(tmp_path / "state.db"), "workdir": str(tmp_path / "work")},
        "telegram": service.config.model_dump(),
    })
    daemon = StewardDaemon(config, tmp_path / "config.yaml", adapters={})
    monkeypatch.setattr(service_module.TelegramService, "start", lambda _: None)
    conversations = SimpleNamespace(native_telegram_topics=lambda: (),
                                    run_turn=lambda **kw: SimpleNamespace(transport_reply=f"See {TURN}"))
    daemon._start_telegram(service._state, conversations, lambda *_: "", world)
    sent = []
    monkeypatch.setattr(daemon._telegram.api, "send_message", lambda _chat, text, **kw: sent.append(text) or 1)
    reply = daemon._telegram.turn_handler("event", 1, 42, 1, "history?", ())
    daemon._telegram.send_reply(1, 42, reply)
    assert f"/commit/{sha}" in sent[0]


@pytest.fixture
def ssh_configuration(tmp_path, monkeypatch):
    config = tmp_path / "ssh_config"
    config.write_text("")
    run = subprocess.run
    queries = []
    def configured_run(argv, **kwargs):
        if argv[0] == "ssh":
            queries.append(argv)
            # Exercise the real parser without developer credentials/config.
            argv = [argv[0], "-F", str(config), *argv[1:]]
        return run(argv, **kwargs)
    monkeypatch.setattr(subprocess, "run", configured_run)
    return config, queries


@pytest.mark.parametrize("remote", [
    "git@github-example-org-world:example/org-world.git",
    "ssh://git@github-example-org-world/example/org-world.git",
])
def test_configured_ssh_alias_reaches_renderer_and_service(
    tmp_path, published_world, monkeypatch, remote, ssh_configuration,
):
    _original, source, sha = published_world
    world = ControllerGitTransport(tmp_path / "state.db", "alias", remote, "main")
    git(world.git_dir, "fetch", str(source), f"HEAD:{world.remote_ref}")
    config, queries = ssh_configuration
    config.write_text("Host github-example-org-world\n  HostName github.com\n  User git\n")
    url = f"https://github.com/example/org-world/commit/{sha}"
    assert turn_reference_entities(world, TURN)[TURN][1] == url
    service = _service(tmp_path)
    service._world_transport = world
    sent = []
    monkeypatch.setattr(service.api, "send_message", lambda _chat, text, **kw: sent.append(text) or 1)
    service.send_result(1, 42, f"See {TURN}", "alias-notification")
    assert f'href="{url}"' in sent[0]
    assert "Preserve the originals · 2020-03-04 10:11 +0200" in sent[0]
    assert len(queries) == 2 and all("-G" in query for query in queries)


@pytest.mark.parametrize("hostname", [None, "gitlab.com", "github.com.example.test"])
def test_alias_name_alone_does_not_establish_github_identity(published_world, ssh_configuration, hostname):
    world, _source, _sha = published_world
    world.remote_url = "git@github-example-org-world:example/org-world.git"
    config, queries = ssh_configuration
    if hostname:
        config.write_text(f"Host github-example-org-world\n  HostName {hostname}\n")
    assert turn_reference_entities(world, TURN) == {}
    assert len(queries) == 1


@pytest.mark.parametrize("error", [FileNotFoundError("ssh unavailable"), subprocess.TimeoutExpired("ssh", 5)])
def test_ssh_alias_query_failure_keeps_delivery_literal(tmp_path, published_world, monkeypatch, error):
    world, _source, _sha = published_world
    world.remote_url = "git@github-example-org-world:example/org-world.git"
    run = subprocess.run
    def failing_query(argv, **kwargs):
        if argv[0] == "ssh":
            raise error
        return run(argv, **kwargs)
    monkeypatch.setattr(subprocess, "run", failing_query)
    service = _service(tmp_path)
    service._world_transport = world
    sent = []
    monkeypatch.setattr(service.api, "send_message", lambda _chat, text, **kw: sent.append(text) or 1)
    service.send_reply(1, 42, f"See {TURN}")
    assert sent == [f"See {TURN}"]


def test_canonical_remote_never_evaluates_ssh_config(published_world, ssh_configuration):
    world, _source, _sha = published_world
    _config, queries = ssh_configuration
    assert TURN in turn_reference_entities(world, TURN)
    world.remote_url = "git@github.com:example/world.git"
    assert TURN in turn_reference_entities(world, TURN)
    assert queries == []

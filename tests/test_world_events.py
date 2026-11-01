import hashlib
import hmac
import json
import threading

import httpx
import pytest

from steward_harness.world_events import ExchangeDue, Notices, Subscription, github_repository, relay

SECRET = 'webhook-secret-' * 3
TOKEN = 'reader-token-' * 3


def notices():
    return Notices({'one/org-world': {'ref': 'refs/heads/main', 'secret': SECRET},
                    'two/org-world': {'ref': 'refs/heads/main', 'secret': SECRET}},
                   {TOKEN: ['one/org-world']})


def push(broker, repo='one/org-world', ref='refs/heads/main', secret=SECRET):
    body = json.dumps({'repository': {'full_name': repo}, 'ref': ref}).encode()
    signature = 'sha256=' + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return broker.accept(body, signature, 'push')


def test_signature_branch_and_scope():
    broker = notices()
    with pytest.raises(PermissionError):
        push(broker, secret='wrong')
    with pytest.raises(PermissionError):
        push(broker, repo='unknown/org-world')
    assert not push(broker, ref='refs/heads/task')
    assert broker.scope('Bearer wrong') == ()
    scope = broker.scope('Bearer ' + TOKEN)
    assert scope == ('one/org-world',)
    before, initial = broker.changes(scope)
    assert initial == [{'repository': 'one/org-world', 'ref': 'refs/heads/main'}]
    push(broker, repo='two/org-world')
    assert broker.changes(scope, before, timeout=0)[1] == []
    push(broker)
    assert broker.changes(scope, before, timeout=0)[1] == initial


def test_bursts_coalesce_and_reconnect_reconciles_without_replay_log():
    broker = notices()
    scope = broker.scope('Bearer ' + TOKEN)
    before, initial = broker.changes(scope)
    for _ in range(20):
        push(broker)
    current, changed = broker.changes(scope, before, timeout=0)
    assert changed == initial  # One invalidation, not twenty queued fetches.
    assert broker.changes(scope, current, timeout=0)[1] == []
    assert broker.changes(scope)[1] == initial  # Also true after relay restart.


def test_failed_fetch_is_retried_and_notice_during_exchange_is_kept():
    schedule = ExchangeDue()
    assert schedule.due('a', now=10)
    schedule.begin()
    schedule.finish('a', True, now=10)
    assert not schedule.due('a', now=11)
    assert schedule.due('b', now=11)  # Local publication does not wait an hour.
    schedule.notify()
    schedule.begin()
    schedule.notify()  # Another writer pushes during fetch.
    schedule.finish('b', True, now=12)
    assert schedule.due('b', now=13)
    schedule.begin()
    schedule.finish(None, False, now=13)
    assert not schedule.due('b', now=14)
    assert schedule.due('b', now=73)
    schedule.begin()
    schedule.finish('b', True, now=73)
    assert not schedule.due('b', now=74)
    assert schedule.due('b', now=3673)


def test_client_errors_are_not_acknowledged_and_tokens_require_secure_transport():
    def fail(_sources):
        raise OSError('fetch failed')
    sub = Subscription('https://example.test/events', TOKEN, fail)
    with pytest.raises(OSError):
        sub.deliver('data: {"kind":"changed","sources":[{"repository":"one/org-world","ref":"refs/heads/main"}]}')
    with pytest.raises(ValueError):
        Subscription('http://example.test/events', TOKEN, fail)
    with pytest.raises(ValueError):
        Subscription('https://example.test/events?token=secret', TOKEN, fail)
    assert github_repository('git@github.com:One/org-world.git') == 'one/org-world'
    assert github_repository('git@github-webel-org-world:webel/org-world.git') == 'webel/org-world'
    assert github_repository('ssh://git@github-company/One/org-world.git') == 'one/org-world'
    assert github_repository('https://github.com.evil.test/one/org-world') is None


def test_http_relay_receives_push_and_streams_only_scoped_metadata():
    broker = notices()
    server = relay(broker, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f'http://127.0.0.1:{server.server_port}'
    try:
        with httpx.Client(timeout=3, trust_env=False) as client:
            assert client.get(url + '/events').status_code == 403
            with client.stream('GET', url + '/events', headers={'Authorization': 'Bearer ' + TOKEN}) as response:
                lines = response.iter_lines()
                initial = json.loads(next(lines)[6:])
                assert initial['kind'] == 'reconcile'
                assert initial['sources'] == [{'repository': 'one/org-world', 'ref': 'refs/heads/main'}]
                body = json.dumps({'repository': {'full_name': 'one/org-world'}, 'ref': 'refs/heads/main', 'commits': ['private-content']}).encode()
                signature = 'sha256=' + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
                assert client.post(url + '/github', content=body, headers={'X-Hub-Signature-256': signature, 'X-GitHub-Event': 'push'}).status_code == 204
                next(lines)  # Blank SSE separator.
                changed = next(lines)
                assert json.loads(changed[6:])['kind'] == 'changed'
                assert 'private-content' not in changed and 'two/org-world' not in changed
    finally:
        server.shutdown()
        server.server_close()


def test_subscription_reconnect_reconciles_and_follows_signed_pushes():
    broker = notices()
    server = relay(broker, port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    delivered, reconciled, changed = [], threading.Event(), threading.Event()
    def receive(sources):
        delivered.append(sources)
        if len(delivered) == 1:
            raise OSError('reader temporarily unavailable')
        (reconciled if len(delivered) == 2 else changed).set()
    sub = Subscription(f'http://127.0.0.1:{server.server_port}/events', TOKEN, receive).start()
    try:
        assert reconciled.wait(5)
        assert sub.connected.is_set()
        push(broker, repo='two/org-world')
        assert not changed.wait(0.1)
        push(broker)
        assert changed.wait(3)
        assert all(s == [{'repository': 'one/org-world', 'ref': 'refs/heads/main'}] for s in delivered)
    finally:
        sub.close()
        server.shutdown()
        server.server_close()


def test_http_protocol_in_memory_rejects_forgery_and_streams_scoped_reset():
    from io import BytesIO
    from steward_harness.world_events import handler_for

    class Connection:
        def __init__(self, request):
            self.input = BytesIO(request)
            self.output = bytearray()
        def makefile(self, *_args):
            return self.input
        def settimeout(self, _timeout):
            pass
        def sendall(self, data):
            self.output.extend(data)
            if data.startswith(b'data: '):
                raise BrokenPipeError('Test client disconnects after first event')

    broker = notices()
    Handler = handler_for(broker)
    def request(data):
        connection = Connection(data)
        Handler(connection, ('127.0.0.1', 1), object())
        return bytes(connection.output)

    body = b'{"repository":{"full_name":"one/org-world"},"ref":"refs/heads/main"}'
    prefix = b'POST /github HTTP/1.1\r\nHost: localhost\r\nContent-Length: ' + str(len(body)).encode() + b'\r\nX-GitHub-Event: push\r\n'
    assert b'403' in request(prefix + b'\r\n' + body)
    signature = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest().encode()
    assert b'204' in request(prefix + b'X-Hub-Signature-256: sha256=' + signature + b'\r\n\r\n' + body)
    assert broker.versions['one/org-world'] == 1
    assert b'403' in request(b'GET /events HTTP/1.1\r\nHost: localhost\r\n\r\n')
    result = request(b'GET /events HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer ' + TOKEN.encode() + b'\r\n\r\n')
    assert b'200' in result and b'text/event-stream' in result
    assert b'"kind":"reconcile"' in result and b'one/org-world' in result
    assert b'two/org-world' not in result

"""Polling and file delivery share one transport until all writers drain."""
from types import SimpleNamespace
import threading

import httpx

from steward_harness.daemon import StewardDaemon
from steward_harness.telegram import api as api_module


def test_polling_and_downloads_reuse_one_client_and_keep_request_deadlines(tmp_path, monkeypatch):
    clients, requests = [], []
    client_type = httpx.Client

    def respond(request):
        requests.append(request)
        if request.url.path.endswith('/getFile'):
            return httpx.Response(200, json={'ok': True, 'result': {'file_path': 'photo.jpg'}})
        if request.url.path.endswith('/photo.jpg'):
            return httpx.Response(200, content=b'photo')
        return httpx.Response(200, json={'ok': True, 'result': []})

    def create(**kwargs):
        client = client_type(transport=httpx.MockTransport(respond), **kwargs)
        clients.append(client)
        return client

    monkeypatch.setattr(api_module.httpx, 'Client', create)
    api = api_module.TelegramAPI('test-token')
    try:
        for _ in range(30):
            assert api.get_updates(0, timeout_seconds=15) == []
        api.download_file('photo', tmp_path/'photo', max_bytes=5)
        assert len(clients) == 1
        assert not clients[0].is_closed
        assert requests[0].extensions['timeout']['read'] == 25.0
        assert requests[-1].extensions['timeout']['read'] == 60.0
    finally:
        api.close()
    assert clients[0].is_closed


def test_shutdown_keeps_telegram_open_until_kernel_results_and_ingress_drain():
    events = []
    daemon = object.__new__(StewardDaemon)
    daemon._stop = threading.Event()
    daemon._telegram = SimpleNamespace(
        request_stop=lambda: events.append('stop intake'),
        stop=lambda: events.append('drain ingress and close transport'),
    )
    daemon._kernel = SimpleNamespace(
        stop=lambda: events.append('drain task results'),
        tasks=SimpleNamespace(interrupt_running=lambda: events.append('interrupt task turns')),
    )
    daemon._desk_ingress = SimpleNamespace(join=lambda: events.append('drain desk ingress'))
    daemon._health = SimpleNamespace(stop=lambda: events.append('stop health'))
    daemon.stop()
    assert events == ['stop intake', 'interrupt task turns', 'drain task results',
                      'drain ingress and close transport', 'drain desk ingress', 'stop health']

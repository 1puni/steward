"""GitHub invalidations, shared by world readers. Git remains the source of truth.

The relay carries only repository/ref names. Every connection starts with a
reconcile notice; no durable event log or claimed copy of Git history is needed.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import logging
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import httpx

log = logging.getLogger(__name__)


def github_repository(url: str) -> str | None:
    # Deploy keys commonly use a github-<name> SSH Host alias. This is only
    # notice routing; the configured Git transport still verifies the remote.
    match = re.fullmatch(r'(?:https://github\.com/|git@(?:github\.com|github-[\w-]+):|ssh://git@(?:github\.com|github-[\w-]+)/)([\w.-]+/[\w.-]+?)(?:\.git)?/?', url)
    return match[1].lower() if match else None


class Notices:
    """Bounded invalidations: one counter per configured repository, no payloads."""

    def __init__(self, repositories: dict, readers: dict):
        self.repositories = {name.lower(): spec for name, spec in repositories.items()}
        self.readers = readers
        if not self.repositories or not readers:
            raise ValueError('Configure repositories and scoped readers')
        for name, spec in self.repositories.items():
            if not re.fullmatch(r'[\w.-]+/[\w.-]+', name) or not spec['ref'].startswith('refs/heads/') or len(spec['secret']) < 32:
                raise ValueError('Invalid repository, branch or webhook secret')
        for token, names in readers.items():
            if len(token) < 32 or not names or not set(names) <= self.repositories.keys():
                raise ValueError('Invalid reader token or repository scope')
        self.condition = threading.Condition()
        self.versions = dict.fromkeys(self.repositories, 0)

    def scope(self, authorization: str) -> tuple[str, ...]:
        for token, names in self.readers.items():
            if hmac.compare_digest(authorization.encode(), ('Bearer ' + token).encode()):
                return tuple(names)
        return ()

    def accept(self, body: bytes, signature: str, event: str) -> bool:
        payload = json.loads(body)
        name = payload['repository']['full_name'].lower()
        spec = self.repositories.get(name)
        if spec is None:
            raise PermissionError('Unknown repository')
        expected = 'sha256=' + hmac.new(spec['secret'].encode(), body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected.encode(), signature.encode()):
            raise PermissionError('Invalid signature')
        if event != 'push' or payload.get('ref') != spec['ref']:
            return False
        with self.condition:
            self.versions[name] += 1
            self.condition.notify_all()
        return True

    def changes(self, scope, previous=None, timeout=20):
        with self.condition:
            if previous is not None:
                self.condition.wait_for(lambda: any(self.versions[n] != previous[n] for n in scope), timeout)
            current = {n: self.versions[n] for n in scope}
            changed = [dict(repository=n, ref=self.repositories[n]['ref']) for n in scope
                       if previous is None or current[n] != previous[n]]
            return current, changed


def handler_for(notices: Notices):
    """HTTP boundary, separable from sockets for in-process protocol tests."""
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass  # Neither credentials nor webhook bodies belong in access logs.

        def do_POST(self):
            self.connection.settimeout(10)
            if self.path != '/github' or self.headers.get('Transfer-Encoding'):
                self.send_error(400)
                return
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= 2_000_000:
                    self.send_error(413)
                    return
                body = self.rfile.read(size)
                if len(body) != size:
                    raise ValueError('Incomplete body')
                notices.accept(body, self.headers.get('X-Hub-Signature-256', ''), self.headers.get('X-GitHub-Event', ''))
            except PermissionError:
                self.send_error(403)
                return
            except (ValueError, KeyError, TypeError, AttributeError, TimeoutError):
                self.send_error(400)
                return
            self.send_response(204)
            self.end_headers()

        def do_GET(self):
            scope = notices.scope(self.headers.get('Authorization', ''))
            if self.path != '/events' or not scope:
                self.send_error(403)
                return
            self.connection.settimeout(30)
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Accel-Buffering', 'no')
            self.end_headers()
            previous = None
            try:
                while True:
                    current, changed = notices.changes(scope, previous)
                    if changed:
                        kind = 'reconcile' if previous is None else 'changed'
                        payload = json.dumps(dict(kind=kind, sources=changed), separators=(',', ':'))
                        self.wfile.write(f'data: {payload}\n\n'.encode())
                    else:
                        self.wfile.write(b': heartbeat\n\n')
                    self.wfile.flush()
                    previous = current
            except OSError:
                pass

    return Handler


def relay(notices: Notices, host='127.0.0.1', port=8790):
    """Private listener; deploy behind HTTPS with proxy buffering disabled."""
    return ThreadingHTTPServer((host, port), handler_for(notices))


class Subscription:
    """One outbound stream per consumer; reconnect reconciles its authorised scope.

    callback receives repository/ref pairs and must retain them until refreshed
    or raise on failure. Failure reconnects with backoff and reconciles again.
    """
    def __init__(self, url, token, callback):
        parsed = urlsplit(url)
        if (parsed.scheme != 'https' and not (parsed.scheme == 'http' and parsed.hostname in {'localhost', '127.0.0.1', '::1'})) or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError('Events require HTTPS (HTTP allowed on loopback only)')
        if not token:
            raise ValueError('Events require a reader token')
        self.url, self.token, self.callback = url, token, callback
        self.stopped = threading.Event()
        self.connected = threading.Event()
        self.thread = threading.Thread(target=self.run, name='world-events', daemon=True)

    def start(self):
        self.thread.start()
        return self

    def close(self):
        self.stopped.set()
        self.thread.join(timeout=1)

    def deliver(self, line):
        if not line.startswith('data: '):
            return
        payload = json.loads(line[6:])
        if payload['kind'] not in {'reconcile', 'changed'}:
            raise ValueError('Unknown world notice')
        sources = payload['sources']
        if not isinstance(sources, list) or any(not isinstance(s, dict) or not isinstance(s.get('repository'), str) or not isinstance(s.get('ref'), str) for s in sources):
            raise ValueError('Invalid world notice')
        self.callback(sources)

    def run(self):
        delay = 1
        while not self.stopped.is_set():
            started = time.monotonic()
            try:
                with httpx.Client(timeout=httpx.Timeout(35, connect=10), follow_redirects=False) as client:
                    with client.stream('GET', self.url, headers={'Authorization': 'Bearer ' + self.token}) as response:
                        response.raise_for_status()
                        if not response.headers.get('content-type', '').startswith('text/event-stream'):
                            raise ValueError('Expected event stream')
                        self.connected.set()
                        log.info('World notifications connected')
                        for line in response.iter_lines():
                            if self.stopped.is_set():
                                return
                            self.deliver(line)
            except Exception as error:  # Keep transport failure separate from the owning service.
                log.warning('World notifications disconnected (%s); reconciling on reconnect', type(error).__name__)
            finally:
                self.connected.clear()
            if time.monotonic() - started > 60:
                delay = 1
            if self.stopped.wait(delay):
                return
            delay = min(60, delay * 2)


class ExchangeDue:
    """Local changes + remote notices, with a slow fallback and bounded failure retry."""
    def __init__(self, fallback=3600):
        self.fallback = fallback
        self.deadline = 0.0
        self.head = None
        self.pending = threading.Event()
        self.retry_at = 0.0

    def notify(self, _sources=None):
        self.pending.set()

    def due(self, head, now=None):
        now = time.monotonic() if now is None else now
        return now >= self.retry_at and (head != self.head or self.pending.is_set() or now >= self.deadline)

    def begin(self):
        self.pending.clear()  # Notices arriving during the exchange stay pending.

    def finish(self, head, success, now=None):
        now = time.monotonic() if now is None else now
        if success:
            self.head = head
            self.deadline = now + self.fallback
            self.retry_at = 0
        else:
            self.pending.set()
            self.retry_at = now + 60


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True, type=Path, help='Private JSON: repositories {name: {ref, secret}}, readers {token: [names]}')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8790)
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    server = relay(Notices(config['repositories'], config['readers']), args.host, args.port)
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == '__main__':
    main()

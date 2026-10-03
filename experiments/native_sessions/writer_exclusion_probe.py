#!/usr/bin/env python3
"""Counterexample to idle/terminal-cleanup/leader-stop writer exclusion.

Real Codex or Claude Code, disposable home, deliberately writing MCP fixture, no inference/auth.
This is a rejection probe, never authorization to reuse a provider process.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time

# The writer belongs to the MCP server itself, not a tool subprocess. This models
# background indexing or memory maintenance without depending on model choices.
MCP = r'''
import json, os, pathlib, sys, threading, time
marker = pathlib.Path(sys.argv[1])
(pathlib.Path(sys.argv[2]) / str(os.getpid())).touch()
def write():
    while True:
        with marker.open('a') as stream:
            stream.write('write\n')
        time.sleep(.02)
threading.Thread(target=write, daemon=True).start()
for line in sys.stdin:
    message = json.loads(line)
    if 'id' not in message:
        continue
    method = message['method']
    result = {}
    if method == 'initialize':
        result = dict(protocolVersion=message['params']['protocolVersion'],
                      capabilities={'tools': {}}, serverInfo={'name': 'writer', 'version': '1'})
    elif method == 'tools/list':
        result = {'tools': []}
    print(json.dumps(dict(jsonrpc='2.0', id=message['id'], result=result)), flush=True)
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--provider', choices=('codex', 'claude'), default='codex')
    provider = parser.parse_args().provider
    if not hasattr(os, 'pidfd_open') or not hasattr(signal, 'pidfd_send_signal'):
        parser.error('requires Linux Python with pidfd support (try /usr/bin/python3)')

    def timeout(*_):
        raise TimeoutError('writer exclusion probe deadline')

    signal.signal(signal.SIGALRM, timeout)
    signal.alarm(40)
    with tempfile.TemporaryDirectory(prefix='steward-writer-proof-') as root:
        path = Path(root)
        home = path / 'home'
        home.mkdir()
        marker, pid_dir = path / 'writes', path / 'pids'
        pid_dir.mkdir()
        config = {'command': sys.executable, 'args': ['-c', MCP, str(marker), str(pid_dir)]}
        args = ['codex', 'app-server', '--stdio', '-c', 'features.apps=false']
        for key, value in config.items():
            args.extend(['-c', f'mcp_servers.writer.{key}={json.dumps(value)}'])
        if provider == 'claude':
            args = ['claude', '-p', '--input-format', 'stream-json', '--output-format',
                    'stream-json', '--verbose', '--strict-mcp-config', '--mcp-config',
                    json.dumps({'mcpServers': {'writer': config}})]
        # Deliberately no inherited provider configuration or credentials.
        env = {'PATH': os.environ['PATH'], 'HOME': str(home), 'CODEX_HOME': str(home),
               'CLAUDE_CONFIG_DIR': str(home)}
        process = subprocess.Popen(args, cwd=root, env=env, start_new_session=True,
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, text=True)
        serial = 0

        def rpc(method, params):
            nonlocal serial
            serial += 1
            process.stdin.write(json.dumps(dict(id=serial, method=method, params=params)) + '\n')
            process.stdin.flush()
            for line in process.stdout:
                item = json.loads(line)
                if item.get('id') == serial:
                    if 'error' in item:
                        raise RuntimeError(item['error'])
                    return item['result']
            raise RuntimeError('provider ended before response')

        def fixture_signal(sig):
            for entry in pid_dir.iterdir():
                pid = int(entry.name)
                try:
                    fd = os.pidfd_open(pid)
                except ProcessLookupError:
                    continue
                try:
                    # Pin identity before inspection; a retired MCP instance's
                    # numeric PID must never authorize signalling a new process.
                    command = Path(f'/proc/{pid}/cmdline').read_bytes()
                    if str(marker).encode() in command:
                        signal.pidfd_send_signal(fd, sig)
                        if sig == signal.SIGSTOP:
                            stopped(pid)
                except ProcessLookupError:
                    pass
                finally:
                    os.close(fd)

        def stopped(pid):
            until = time.monotonic() + 2
            while True:
                status = Path(f'/proc/{pid}/status').read_text()
                if any(line.startswith('State:') and 'T' in line for line in status.splitlines()):
                    return
                if time.monotonic() >= until:
                    raise RuntimeError('fixture did not stop')
                time.sleep(.01)

        def delta():
            before = marker.stat().st_size
            time.sleep(.25)
            return marker.stat().st_size - before

        try:
            if provider == 'codex':
                rpc('initialize', {'clientInfo': {'name': 'steward-writer-proof', 'version': '1'},
                                   'capabilities': {'experimentalApi': True}})
                tid = rpc('thread/start', {'cwd': root, 'approvalPolicy': 'never',
                                           'sandbox': 'read-only'})['thread']['id']
                inventory = rpc('mcpServerStatus/list', {'threadId': tid})
                assert any(x['name'] == 'writer' for x in inventory['data']), inventory
                clean = rpc('thread/backgroundTerminals/clean', {'threadId': tid})
            else:
                process.stdin.write(json.dumps({'type': 'control_request', 'request_id': 'init',
                    'request': {'subtype': 'initialize'}}) + '\n')
                process.stdin.flush()
                # MCP startup itself suffices for this counterexample. No user
                # message, model turn, result or credential is supplied.
                until = time.monotonic() + 15
                while not marker.exists():
                    if process.poll() is not None or time.monotonic() >= until:
                        raise RuntimeError('Claude did not start the MCP writer')
                    time.sleep(.05)
                clean = None
            idle_delta = delta()
            os.kill(process.pid, signal.SIGSTOP)
            stopped(process.pid)
            stopped_delta = delta()
            assert idle_delta > 0 and stopped_delta > 0
            # A peer in another invocation has the same filesystem authority.
            # Do not touch other sessions: demonstrate on this disposable target.
            fixture_signal(signal.SIGSTOP)
            peer = subprocess.run([sys.executable, '-c',
                'import pathlib,sys; pathlib.Path(sys.argv[1]).write_text("peer")',
                str(marker)], check=True)
            assert peer.returncode == 0
            peer_write = marker.read_text() == 'peer'
            assert peer_write
            fixture_signal(signal.SIGCONT)
            os.kill(process.pid, signal.SIGCONT)
            print(json.dumps({
                'native': subprocess.check_output([provider, '--version'], text=True).strip(),
                'uid': os.geteuid(), 'inference': False, 'terminal_cleanup': clean,
                'bytes_written_while_idle': idle_delta,
                'bytes_written_while_provider_leader_stopped': stopped_delta,
                'same_uid_peer_write_with_provider_and_mcp_stopped': peer_write,
                'reuse_proven': False,
            }, sort_keys=True))
        finally:
            # Only this disposable process group and identity-pinned fixtures are killed.
            if process.poll() is None:
                os.kill(process.pid, signal.SIGCONT)
            try:
                fixture_signal(signal.SIGKILL)
            finally:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait(timeout=5)
            # Allow killed descendants to finish exiting before removing their home.
            time.sleep(.1)
            signal.alarm(0)


if __name__ == '__main__':
    main()

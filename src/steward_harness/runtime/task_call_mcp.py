"""Native MCP stdio bridge; no authority or task state lives in the child."""

# Only the standard library: the provider identity need not import the controller
# installation, read its config, or open its state. Native CLIs launch this child
# outside their shell-tool sandbox, under the existing untrusted OS identity.
CLIENT = r'''
import http.client, json, socket, sys, urllib.request, urllib.error
class Connection(http.client.HTTPConnection):
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(sys.argv[1])
class Handler(urllib.request.HTTPHandler):
    def http_open(self, request):
        return self.do_open(Connection, request)
url = 'http://localhost/task'
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), Handler())
for line in sys.stdin:
    message = {}
    try:
        message = json.loads(line)
        if 'id' not in message:
            continue
        method = message.get('method')
        if method == 'initialize':
            result = {'protocolVersion': message['params']['protocolVersion'],
                      'capabilities': {'tools': {}},
                      'serverInfo': {'name': 'steward-tasks', 'version': '1'}}
        elif method == 'ping':
            result = {}
        elif method == 'tools/list':
            result = {'tools': [{'name': 'task',
                'description': 'Operate on authorized Steward tasks; task executions only permit query (repository, text), a bounded ownership observation. Mutations require a stable, distinct key per intent; retry identical requests with the same key. Accepted Git receipts survive parent failure.',
                'inputSchema': {'type': 'object', 'required': ['operation'],
                    'properties': {'operation': {'type': 'string', 'enum': ['submit', 'list', 'show', 'answer', 'retry', 'note', 'cancel', 'query']},
                        **{k: {'type': 'string'} for k in ['source_id', 'key', 'repository', 'title', 'brief', 'task_id', 'text']}},
                    'additionalProperties': False}}]}
        elif method == 'tools/call' and message.get('params', {}).get('name') == 'task':
            data = json.dumps(message['params'].get('arguments', {})).encode()
            try:
                response = opener.open(urllib.request.Request(url, data=data,
                    headers={'Content-Type': 'application/json'}), timeout=120)
            except urllib.error.HTTPError as error:
                response = error
            with response:
                receipt = json.loads(response.read())
            result = {'content': [{'type': 'text', 'text': json.dumps(receipt)}],
                      'isError': receipt.get('accepted') is False or 'error' in receipt}
        else:
            print(json.dumps({'jsonrpc': '2.0', 'id': message['id'],
                'error': {'code': -32601, 'message': 'Unknown method or tool'}}), flush=True)
            continue
    except Exception:
        result = {'content': [{'type': 'text', 'text': 'Task call outcome unknown; retry the exact request with the same key using the current execution tool.'}], 'isError': True}
    print(json.dumps({'jsonrpc': '2.0', 'id': message.get('id'), 'result': result}), flush=True)
'''


def server_config(socket_path: str) -> dict:
    return {"command": "python3", "args": ["-c", CLIENT, socket_path]}


def codex_arguments(socket_path: str | None) -> list[str]:
    import json
    if socket_path is None:
        return []
    return [argument for key, value in server_config(socket_path).items()
            for argument in ("-c", f"mcp_servers.steward_tasks.{key}={json.dumps(value)}")]

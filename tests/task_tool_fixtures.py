"""Scripted workers express intent as data and call the real native socket.

The proxy stands in for the model's tool call before returning its final prose.
No final text is parsed by either the proxy or the controller.
"""
from dataclasses import replace
import os

from steward_harness.cognition import Cognition
from test_task_calls import call


class TaskOutput(str):
    def __new__(cls, text="", *, subject="test: inspect", disposition="idle", question=None, notify=None):
        value = super().__new__(cls, text)
        value.notify = notify
        value.intent = dict(operation="close", key="finish", subject=subject, disposition=disposition)
        if question is not None:
            value.intent["question"] = question
        return value


class TaskAdapter:
    def __init__(self, adapter):
        self.adapter = adapter

    def __getattr__(self, name):
        return getattr(self.adapter, name)

    def execute(self, request):
        result = self.adapter.execute(request)
        if isinstance(result.output, TaskOutput) and request.task_call_socket:
            request.on_process_started(os.getpid(), None)
            if result.output.notify:
                receipt = call(request.task_call_socket, operation="notify", key="finding", text=result.output.notify)
                assert receipt.get("accepted"), receipt
            receipt = call(request.task_call_socket, **result.output.intent)
            assert receipt.get("pending") or receipt.get("error") == "task was cancelled", receipt
            result = replace(result, output=str(result.output))
        return result


class TaskCognition(Cognition):
    def __init__(self, adapters, *args, **kwargs):
        super().__init__({key: TaskAdapter(value) for key, value in adapters.items()}, *args, **kwargs)


# Embedded in real subprocess fixtures: discover the same MCP config a native
# model receives, then call its socket from the authorized provider invocation.
NATIVE_CALL = r'''
def steward_call(**arguments):
    import http.client, socket
    if '--mcp-config' in sys.argv:
        config = json.loads(sys.argv[sys.argv.index('--mcp-config') + 1])
        path = config['mcpServers']['steward_tasks']['args'][-1]
    else:
        value = next(a.partition('=')[2] for a in sys.argv if a.startswith('mcp_servers.steward_tasks.args='))
        path = json.loads(value)[-1]
    class Connection(http.client.HTTPConnection):
        def connect(self):
            self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.sock.connect(path)
    connection = Connection('localhost', timeout=10)
    connection.request('POST', '/task', json.dumps(arguments), {'Content-Type': 'application/json'})
    receipt = json.loads(connection.getresponse().read())
    connection.close()
    return receipt
'''

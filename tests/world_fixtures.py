from steward_harness.runtime.contracts import Availability, RuntimeResult, SESSION_WORKSPACE_CAPABILITIES

class ReadingAdapter:
    """Exercise real world access without claiming to test model judgment."""

    family = "codex"
    capabilities = SESSION_WORKSPACE_CAPABILITIES

    def __init__(self, session, respond):
        self.session = session
        self.respond = respond
        self.requests = []

    def available(self):
        return Availability(True)

    def execute(self, request):
        self.requests.append(request)
        output = self.respond(request)
        request.on_session_started(self.session)
        return RuntimeResult(
            output=output,
            resolved=request.resolved,
            effective_model=request.resolved.model,
            provider_session_id=self.session,
        )

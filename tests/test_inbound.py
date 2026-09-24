"""Transport-neutral read-only lineage and accepted output."""
from pathlib import Path

import pytest

from steward_harness.conversations import ConversationService
from steward_harness.runtime.contracts import ResolvedModel, RuntimeResult
from steward_harness.state import StateDatabase


class ReadOnlyCognition:
    def run(self, request, *, execution_id=None):
        if callable(request):
            request = request()
        assert request.sandbox_mode == "read-only"
        return RuntimeResult(
            output="Visible reply",
            resolved=ResolvedModel("codex", "balanced", "model", "medium"),
            effective_model="model",
            provider_session_id=None,
        )


@pytest.mark.parametrize('transport', ['telegram', 'desk'])
def test_read_only_path_keeps_transport_lineage(tmp_path: Path, transport):
    state = StateDatabase(tmp_path / "state.db")
    service = ConversationService(
        state,
        ReadOnlyCognition(),
        provider_order=("codex",),
        profile="balanced",
        workspace=tmp_path,
        timeout_seconds=30,

    )
    result = service.run_turn(
        transport=transport,
        transport_key="lineage",
        source_event_key="event",
        operator_id="operator",
        text="What changed?",
    )
    assert result.reply_text == "Visible reply"
    owner = state.get_conversation(result.conversation_id)
    assert (owner.transport, owner.transport_key) == (transport, "lineage")
    assert state.prepared_turn(str(result.turn_id))["world_root"] is None

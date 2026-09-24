"""Prompt builders: conflict resolution operator-note threading."""

from __future__ import annotations

from steward_harness import prompts
from steward_harness.prompts import build_conflict_prompt


def test_conflict_prompt_omits_note_block_by_default() -> None:
    prompt = build_conflict_prompt(("a.py",), "origin/main", "steward", 1, max_stops=20)
    assert "Operator context" not in prompt


def test_conflict_prompt_includes_operator_note() -> None:
    prompt = build_conflict_prompt(
        ("a.py",), "origin/main", "steward", 1, max_stops=20,
        note="watch out, the auth module was refactored upstream",
    )
    assert "Operator context for this reconcile:" in prompt
    assert "watch out, the auth module was refactored upstream" in prompt


def test_task_prompt_inspects_retained_work_with_current_operator_context() -> None:
    prompt = prompts.build_task_prompt(
        repository="app",
        title="Repair traversal",
        procedure_scope="",
        event_id="task_task_123",
        operator_context=("answer: Use production-eu.",),
        brief="Do the thing.",
    )

    assert "inspect this retained branch" in prompt.lower()
    assert "existing provider-session context" in prompt
    assert "preserve completed work" in prompt
    assert "finish only what remains of the accepted task" in prompt
    assert "app" in prompt
    assert "Repair traversal" in prompt
    assert "## Incoming task context" in prompt
    assert "answer: Use production-eu." in prompt
    assert "## Brief\nDo the thing." in prompt
    assert "findings are the messages of this branch's commits" in prompt


def test_task_prompt_allows_native_local_work_but_not_publication() -> None:
    prompt = prompts.build_task_prompt(
        title="Change behavior", procedure_scope="", repository="app",
        brief="Do the thing.",
    )
    assert "Do not push or deploy" in prompt
    assert "native tools, search, and local commits" in prompt
    assert "Keep the assigned branch" in prompt
    assert "native commit skill" in prompt
    assert "Do not stage or commit" not in prompt


def test_task_execution_supplies_its_own_bounded_closure() -> None:
    prompt = prompts.build_task_prompt(
        title="Change behavior", repository="app", procedure_scope="",
        brief="Do the thing.",
    )
    assert "End your final response with exactly these three lines" in prompt
    assert "COMMIT: <one concise conventional-commit subject" in prompt
    assert "DISPOSITION: <continue|idle|ask>" in prompt
    assert "QUESTION: <one blocking question, or NONE>" in prompt
    assert "configured gates, publication and deployment" in prompt


def test_requirement_audit_is_asked_for_the_verdict_its_gate_reads() -> None:
    scope = prompts.build_procedure_scope(
        candidate="abc123", base="def456", read_only=True, full_tree=True, verdict=True)
    assert "Review the complete candidate tree at abc123" in scope
    assert "VERDICT: PASS" in scope
    assert "never infer PASS from missing evidence" in scope


def test_recurring_run_is_not_asked_to_compress_its_scope_into_a_gate_token() -> None:
    """A rhythm's verdict is written and never read; asking for one distorts it."""
    scope = prompts.build_procedure_scope(
        candidate="abc123", base="def456", read_only=True, full_tree=False, verdict=False)
    assert "VERDICT" not in scope
    # A recurring procedure keeps the scope its accepted instructions own.
    assert scope == ""


def test_workspace_write_procedure_adds_no_scope_block() -> None:
    assert prompts.build_procedure_scope(
        candidate="abc", base="def", read_only=False, full_tree=True, verdict=True) == ""

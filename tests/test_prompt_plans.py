"""Current conversation interface, observations, and task boundary coverage."""

from steward_harness.prompts import build_task_prompt, build_turn_prompt


def test_turn_contains_current_interface_and_observations() -> None:
    turn = build_turn_prompt("  review this  ", "ORIENTATION-SENTINEL", "EVENT-SENTINEL", transport="telegram")
    assert "[[send_image:" in turn
    assert "TASK_PROPOSAL:" in turn
    assert "TASK_ACTION:" in turn
    assert "EVENT-SENTINEL" in turn
    assert "ORIENTATION-SENTINEL" in turn
    assert turn.endswith("## Request\nreview this")


def test_turn_names_only_enabled_scoped_telegram_actions() -> None:
    turn = build_turn_prompt("pin this reply", transport="telegram", telegram_actions=("pin_reply",))
    assert "[[telegram_pin_reply]]" in turn
    assert "telegram_pin_message" not in turn
    desk = build_turn_prompt("review this", transport="desk", telegram_actions=("pin_reply", "pin_message"))
    assert "telegram_pin_" not in desk
    assert "[[send_image:" not in desk
    assert "TASK_PROPOSAL:" in desk and "TASK_ACTION:" in desk


def test_turn_names_authorized_delivery_roots() -> None:
    turn = build_turn_prompt("send image", transport="telegram", delivery_roots=("/var/lib/steward/world/artifacts/delivery",))
    assert "/var/lib/steward/world/artifacts/delivery" in turn
    assert "/tmp" not in turn


def test_task_prompt_carries_turn_id() -> None:
    task = build_task_prompt(
        title="t",
        procedure_scope="",
        repository="r",
        event_id="task_1",
        brief="Do the thing.",
    )
    assert "Turn id: task_1" in task


def test_task_prompt_has_boundary_rules() -> None:
    prompt = build_task_prompt(
        title="Fix bug",
        procedure_scope="",
        repository="app",
        brief="Do the thing.",
    )
    assert "Task: Fix bug" in prompt
    assert "Repository: app" in prompt
    assert "Do not push or deploy" in prompt
    assert "canonical owner of its project-local knowledge" in prompt
    assert "update the appropriate repository documentation" in prompt
    assert "Do not copy repository knowledge into the parent" in prompt


def test_task_prompt_leaves_no_hole_without_a_procedure() -> None:
    """An ordinary task adds no scope section."""
    prompt = build_task_prompt(
        title="Fix bug", procedure_scope="", repository="app",
        brief="Do the thing.",
    )
    assert "## Task\nTask: Fix bug\n\nRepository: app\n\n## Brief\nDo the thing." in prompt
    assert "\n\n\n" not in prompt


def test_task_prompt_places_procedure_scope_between_title_and_repository() -> None:
    prompt = build_task_prompt(
        title="light-review: abc123", procedure_scope="\n\nSCOPE-SENTINEL\n",
        repository="app", brief="Do the thing.",
    )
    assert "## Task\nTask: light-review: abc123\n\nSCOPE-SENTINEL\n\nRepository: app" in prompt

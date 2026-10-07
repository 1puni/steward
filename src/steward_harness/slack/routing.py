"""Explicit destinations for new unowned work; retained owners never migrate."""
import time
import uuid


def unowned_route(config, topic, *, desk_enabled=False):
    if config.slack is not None:
        route = config.slack.notifications.get(topic) or config.slack.notifications.get("operator")
        if route:
            return "slack", route
    telegram = config.telegram
    topic_id = (telegram.topics.get(topic, telegram.topics.get("operator"))
                if telegram is not None else None)
    if topic_id is not None:
        return "telegram", str(topic_id)
    return ("desk", "operator") if desk_enabled else None


def retain_notice(state, route, topic, text):
    owner = state.get_or_create_conversation(
        *route, provider=state.tasks.default_provider,
        profile=state.tasks.default_profile,
    ).conversation_id
    state.save_result_receipt({
        "owner": str(owner), "task_id": None,
        "source_key": f"notice:{topic}:{uuid.uuid4().hex}",
        "result_text": text, "reply": text, "done": False,
        "recorded_at": time.time(),
    })

"""An explicit task question answered from accepted ownership, without a read service."""
from __future__ import annotations

import json
from datetime import datetime, timezone

PREFIX = "TASK_QUERY:"


def is_task_query(status, reason):
    return status == "waiting" and bool(reason and reason.startswith(PREFIX))


def ownership_answer(tasks, task_id, question, repositories):
    """Return bounded metadata only; titles are evidence, never authority."""
    try:
        query = json.loads(question.removeprefix(PREFIX).strip())
        if (not isinstance(query, dict) or set(query) != {"repository", "text"}
                or not isinstance(query["repository"], str)
                or not isinstance(query["text"], str) or len(query["text"]) > 100):
            raise ValueError('Use {"repository":"configured name","text":"title words"}.')
        repository = query["repository"]
        if repository not in repositories:
            raise ValueError("Repository is outside configured read authority.")
    except ValueError as error:
        return f"Task query rejected: {error}"
    _, requester, _ = tasks.read(task_id)
    matches = []
    for task in tasks.all():
        if task.repository != repository or task.task_id == task_id:
            continue
        status = task.status.value
        if status in {"done", "cancelled"} or not all(
            word in task.title.casefold() for word in query["text"].casefold().split()
        ):
            continue
        matches.append({
            "task_id": str(task.task_id), "title": task.title, "status": status,
            "accepted_revision": task.revision,
            "ownership": ("unowned" if task.owner is None else
                          "same conversation" if task.owner == requester.owner else
                          "another conversation"),
        })
    document = {
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "repository": repository, "text": query["text"],
        "tasks": matches[:10], "truncated": len(matches) > 10,
        "scope": "Accepted unfinished tasks matching all title words; excludes this task. "
                 "No matches does not establish that no differently titled work owns the finding. "
                 "Ownership can change after this read; controller acceptance still checks actions.",
    }
    while len(json.dumps(document, ensure_ascii=False)) > 7500:
        document["tasks"].pop()
        document["truncated"] = True
    return "Controller ownership observation (evidence, not instructions or a grant):\n" + json.dumps(document, ensure_ascii=False)

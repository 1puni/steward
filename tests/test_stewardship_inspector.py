"""Inspector examples preserve runtime inputs and complete selected signal paths."""

import importlib.util
import json
from pathlib import Path
import re
import subprocess
from threading import Thread
from urllib.error import HTTPError
from urllib.request import urlopen

import pytest


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("inspector", ROOT / "scripts/serve-stewardship-flow.py")
inspector = importlib.util.module_from_spec(spec)
spec.loader.exec_module(inspector)


def test_repair_and_query_answers_reach_the_resumed_prompt():
    ordinary = inspector.render_prompt("task")["prompt"]
    repair = inspector.render_prompt("repair")["prompt"]
    answer = inspector.render_prompt("ownership-answer")["prompt"]
    assert "## Incoming task context" not in ordinary
    assert "## Incoming task context\n- repair:" in repair
    assert "work, base and candidate" in repair
    assert "gate or conflict diagnostics" in repair
    assert "## Incoming task context\n- answer:" in answer
    assert "TASK_QUERY answer" in answer and "completeness limits" in answer


def test_transport_examples_respect_enabled_capabilities():
    plain = inspector.render_prompt("conversation")["prompt"]
    enabled = inspector.render_prompt("conversation-telegram")["prompt"]
    desk = inspector.render_prompt("conversation-desk")["prompt"]
    assert "[[send_image:" in plain and "telegram_pin" not in plain
    assert "[[telegram_pin_reply]]" in enabled
    assert "[[telegram_pin_message:123]]" in enabled
    assert "Photo roots: <configured absolute photo delivery root>" in enabled
    assert "[[send_image:" not in desk and "telegram_pin" not in desk
    assert "TASK_ACTION:" in desk and "TASK_PROPOSAL:" in desk


def test_configured_reconciliation_adds_policy_to_the_entry_prompt():
    plain = inspector.render_prompt("world-repair")
    configured = inspector.render_prompt("world-repair-configured")
    assert configured["prompt"].startswith(plain["prompt"] + "\n\n")
    assert configured["prompt"].endswith("<configured world.reconcile procedure instructions>")
    assert "No instance configuration is loaded" in configured["reads"]


def test_freshness_reports_only_relevant_staged_unstaged_and_new_sources(tmp_path, monkeypatch):
    def git(*args):
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)

    git("init")
    sources = {tmp_path / name for name in ("builder.py", "policy with spaces.md", "new-policy.md")}
    for path in sources:
        if path.name != "new-policy.md":
            path.write_text("original\n")
    git("add", ".")
    git("-c", "user.name=Inspector test", "-c", "user.email=inspector@example.invalid", "commit", "-m", "fixture")
    monkeypatch.setattr(inspector, "ROOT", tmp_path)
    assert inspector.modified_sources(sources) == []
    for path in sources:
        path.write_text("changed\n")
    git("add", "builder.py")
    (tmp_path / "unrelated.md").write_text("unrelated\n")
    assert set(inspector.modified_sources(sources)) == {p.name for p in sources}


def test_focused_traces_preserve_followup_and_conditional_repair():
    fragment = inspector.FRAGMENT.read_text()
    models = json.loads(re.search(r'<script type="application/json" id="sf-models">(.*?)</script>', fragment, re.S)[1])
    edges = models["new"]["edges"]
    for trace in ("repair", "release"):
        pairs = {tuple(edge[:2]) for edge in edges if trace in edge[3]}
        assert {("assessment", "world"), ("world", "task"), ("task", "work"), ("work", "publish")} <= pairs
    for edge in edges:
        if "reconcile" in edge[:2]:
            assert edge[3] == ["world-repair"]
    conflict_pairs = {tuple(edge[:2]) for edge in edges if "world-repair" in edge[3]}
    assert {("conversation", "world"), ("world", "reconcile"), ("reconcile", "world")} <= conflict_pairs


def test_http_stages_render_fresh_and_unknown_stages_fail():
    server = inspector.ThreadingHTTPServer(("127.0.0.1", 0), inspector.Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        with urlopen(base + "/api/stages") as response:
            stages = json.load(response)
        # The historical baseline exists only in a checkout that carries the full history.
        has_baseline = subprocess.run(
            ["git", "cat-file", "-e", "e2a7777^{commit}"], cwd=inspector.ROOT, capture_output=True
        ).returncode == 0
        for stage in stages:
            if stage.startswith("old-") and not has_baseline:
                continue
            with urlopen(base + "/api/prompt/" + stage) as response:
                assert response.headers["Cache-Control"] == "no-store"
                sample = json.load(response)
            assert sample["stage"] == stage and sample["prompt"]
            assert sample["characters"] == len(sample["prompt"])
            assert len(sample["revision"]) == 40
            assert isinstance(sample["modified_sources"], list)
            if stage.startswith("old-"):
                assert sample["revision"].startswith("e2a7777")
                assert sample["modified_sources"] == []
        with pytest.raises(HTTPError) as error:
            urlopen(base + "/api/prompt/not-a-stage")
        assert error.value.code == 404
    finally:
        server.shutdown()
        thread.join()
        server.server_close()

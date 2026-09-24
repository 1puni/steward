"""The porcelain filter behind the read-only evidence check."""

from steward_harness.task_runner import _without_session_state, session_state_prefixes


GLM = session_state_prefixes("glm")
CODEX = session_state_prefixes("codex")


def test_covers_both_directories_a_provider_maps_into_the_tree():
    assert session_state_prefixes("codex") == ("artefacts/codex/", "memories/codex/")


def test_drops_the_running_provider_transcript():
    status = " M artefacts/glm/projects/a/2f81c0d8.jsonl\n"
    assert _without_session_state(status, GLM) == ""


def test_keeps_another_providers_transcript():
    # Only the family that ran is excused; a turn must not be able to rewrite
    # some other provider's recorded history and call it bookkeeping.
    status = " M artefacts/codex/sessions/rollout-x.jsonl\n"
    assert _without_session_state(status, GLM) == status.rstrip("\n")


def test_keeps_product_changes_alongside_a_transcript():
    status = "?? injected.txt\n M artefacts/glm/projects/a/s.jsonl\n M README.md\n"
    assert _without_session_state(status, GLM).splitlines() == ["?? injected.txt", " M README.md"]


def test_reads_the_destination_of_a_rename():
    assert _without_session_state('R  README.md -> artefacts/glm/projects/a/s.jsonl\n', GLM) == ""
    assert _without_session_state('R  artefacts/glm/projects/a/s.jsonl -> README.md\n', GLM) != ""


def test_handles_quoted_paths_and_short_rows():
    assert _without_session_state(' M "artefacts/glm/projects/a b/s.jsonl"\n', GLM) == ""
    assert _without_session_state("\n", GLM) == ""


def test_a_similarly_named_directory_is_not_excused():
    # `artefacts/glm-notes/` is product, and prefix matching must not swallow it.
    status = " M artefacts/glm-notes/summary.md\n"
    assert _without_session_state(status, GLM) == status.rstrip("\n")
    assert _without_session_state(" M memories/codex/note.md\n", CODEX) == ""

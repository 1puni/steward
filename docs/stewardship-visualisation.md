# Inspect stewardship signals and prompts

Run from the repository:

```sh
uv run python scripts/serve-stewardship-flow.py --port 8766
```

Open `http://127.0.0.1:8766/`. Choose a lifecycle trace, click a cognition box,
or select a stage under **Prompts at cognition boundaries**. Arrows expose
their receiving owner, source and verification references. The baseline is
historical `e2a7777`; the reviewed graph is a curated snapshot of `0c91c67`
dated September 14, 2026, not telemetry or a fresh verification run. Prompt
metadata separately identifies the full working-tree HEAD and any uncommitted
source files used by that rendering. Historical composers identify their own
committed revision.

The repair and publication traces include assessment, world acceptance and
authorized task follow-up. **World conflict reconciliation** is a separate
conditional path: conflict repair returns to acceptance before task effects
can proceed. World acceptance itself is a controller boundary, not a prompt.

## One prompt definition

The inspector calls the same functions in
[prompts.py](../src/steward_harness/prompts.py) that the runtime calls:

- `build_turn_prompt` for conversations and assessments;
- `build_task_prompt` and `build_procedure_scope` for ordinary work and reviews;
- `build_result_assessment_request` for the observation entering a world turn;
- `build_conflict_prompt` for world conflict reconciliation.

The local HTTP handler renders on every selection, reloads the prompt module
to reflect edits made after startup, and disables response caching. There are
no prompt templates, generated prompt snapshots or copied system instructions
in the visualization document. The HTML stores stage identifiers, requests
rendered text, and displays it with `textContent`. Historical composers are
read from Git on request.

The renderer uses visibly marked placeholder inputs. It does not call a model
or inspect private controller state. It shows the harness entry prompt, not the
provider's complete context: native system instructions, installed skills,
session history and subsequent tool results remain outside its scope.

Named scenarios cover resumed repair diagnostics and ownership-query answers
in the task's **New operator context** block, Desk conversations, Telegram
with enabled pin actions and photo roots, and world reconciliation with a
configured procedure. The configured reconciliation scenario uses a labelled
placeholder for the appended policy contents; it does not load instance
configuration. These examples exercise runtime composition with sample inputs.

Selecting a stage clears the preceding prompt and all its supporting metadata
together. A failed rendering leaves an explicit error; selecting a stage
again retries it.

The **Files and instructions read separately** section reads the current
procedure policy directly from its source file. A running task instead reads
its frozen admitted instructions from its accepted task account. The inspector
labels that distinction; it does not pretend today's file is a historical run.

## Files and verification

- [Local server](../scripts/serve-stewardship-flow.py): read-only stage rendering.
- [Styles](../scripts/stewardship-flow.css): local browser presentation.
- [Diagram source](../output/stewardship-flow/stewardship-flow.html): graph,
  traces, source references and stage links; no prompt text.

The in-conversation version cannot execute local Python. Its cognition links
open the local inspector, which must be running. It never falls back to stale
embedded prompt text. When editing the shared builders, run the affected
runtime tests; when editing the inspector, exercise every stage and check
desktop/mobile layout and light/dark themes.

`uv run pytest -q tests/test_stewardship_inspector.py tests/test_prompts.py
tests/test_prompt_plans.py` checks scenario composition, focused trace
connections, relevant source changes, HTTP rendering and unknown stages.
Browser verification also covers cognition links and request failure/recovery.

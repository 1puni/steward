# Reflex layer prototype

Local System-1 judgment heads for three Steward decision points, measured
against cortex gold labels. The landscape survey and results live in
`docs/reflex-layer.md`; this directory holds the working prototype.

## Judgment points

| Point | Input | Yes | No |
|---|---|---|---|
| `should_wake` | desk-inbox style event (source + text) | wake the steward now | stay asleep |
| `is_diff_safe` | commit subject + file count + changed areas | apply without cortex review | needs review |
| `is_task_done` | task disposition body | task is done | not done |

## Layout

- `reflex.py` — embedder (`minishlab/potion-base-8M`, 30 MB static embeddings)
  plus one logistic head per decision point. Below-threshold confidences
  escalate instead of deciding.
- `evaluate.py` — 5-fold stratified CV with per-fold threshold calibration
  (reject-option rule: escalate at and below the highest confidence any
  training error reached), a threshold sweep, a warm timing loop, and the
  held-out decision log.
- `data/*.jsonl` — cortex-labeled corpora. `is_diff_safe` items are real
  commits from this repository's history (`origin: git:<sha>`); the other two
  are grounded in the harness's desk/probe/rhythm and disposition vocabulary.
- `logs/decisions-2026-09-25.jsonl` — every held-out decision with confidence
  and action, committed to Git as the durable decision record.

## Run

```sh
uv venv /tmp/reflex-env
uv pip install --python /tmp/reflex-env/bin/python model2vec scikit-learn
cd experiments/reflex && /tmp/reflex-env/bin/python evaluate.py
```

First run downloads the 30 MB model from Hugging Face; later runs use the
local cache. Measured numbers and interpretation: `docs/reflex-layer.md`.

## Protocol notes

- Gold labels are the cortex's own judgments (the full model labeled every
  item before any classifier ran). Labels for `is_diff_safe` follow the
  stated policy: docs/tests/config-only changes are safe; any
  `src/steward_harness` runtime change, authority-flow commits, or risky
  operations need review.
- `should_wake` deliberately contains near-duplicate opposing pairs
  ("seemed slow, maybe check" vs "seemed fine, no worries") to give
  escalation something honest to bite on. It is the hard head, and the
  results show it.
- Escalated items are answered by the cortex in the pipeline metrics; the
  log records the reflex's tentative label even when escalating, so
  escalation precision (were escalations actually needed?) is auditable.

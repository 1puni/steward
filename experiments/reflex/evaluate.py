"""Evaluate the reflex layer against cortex gold labels.

Protocol (see docs/reflex-layer.md):
  * Each corpus is cortex-labeled (the full model judged every item).
  * 5-fold stratified cross-validation reports the reflex's standalone accuracy
    and the accuracy/coverage/escalation tradeoff at the chosen thresholds.
  * A held-out fold produces the committed decision log: every decision, its
    confidence, and whether it escalated.
  * Latency is measured on a warm timing loop (embed + inference + threshold).

Escalation metrics:
  * coverage      - fraction of items decided locally (not escalated).
  * local_acc     - accuracy on locally decided items.
  * pipeline_acc  - accuracy after escalations are answered by the cortex.
  * escalation_precision - fraction of escalated items where the reflex's
    argmax was actually wrong (a useful escalation).
  * missed_errors - confident-but-wrong items the reflex would have shipped.

Run:
    /tmp/reflex-env/bin/python evaluate.py   # or any venv with model2vec+sklearn
"""

from __future__ import annotations

import json
import statistics
from pathlib import Path

from sklearn.model_selection import StratifiedKFold

from reflex import DECISION_POINTS, Reflex, append_decision_log, load_corpus

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
LOGS = HERE / "logs"
TIMING_RUNS = 200


def threshold_metrics(probas: list[list[float]], gold: list[int], threshold: float) -> dict:
    decided, correct, escalated, useful_esc, missed, argmax_right = 0, 0, 0, 0, 0, 0
    for probs, truth in zip(probas, gold):
        argmax = max(range(len(probs)), key=lambda i: probs[i])
        conf = probs[argmax]
        if argmax == truth:
            argmax_right += 1
        if conf >= threshold:
            decided += 1
            if argmax == truth:
                correct += 1
            else:
                missed += 1
        else:
            escalated += 1
            if argmax != truth:
                useful_esc += 1
    n = len(gold)
    return {
        # Accuracy if the reflex decided everything itself, ignoring thresholds.
        "standalone_acc": argmax_right / n,
        "coverage": decided / n,
        "local_acc": correct / decided if decided else float("nan"),
        # Escalated items are answered by the cortex, so they land correct.
        "pipeline_acc": (correct + escalated) / n,
        "escalation_rate": escalated / n,
        "escalation_precision": useful_esc / escalated if escalated else float("nan"),
        "missed_errors": missed,
    }


def calibrate_threshold(train_probas: list[list[float]], train_gold: list[int]) -> float:
    """Reject-option rule: escalate at and below the highest confidence any
    training error reached. Nothing the reflex got wrong in training is then
    decided locally; the floor keeps the rule from degenerating on easy sets."""
    error_confidences = [
        max(probs) for probs, truth in zip(train_probas, train_gold)
        if max(range(len(probs)), key=lambda i: probs[i]) != truth
    ]
    return max(0.55, max(error_confidences, default=0.0))


def evaluate_point(reflex: Reflex, point: str, log_path: Path | None) -> dict:
    texts, gold, ids = load_corpus(DATA / f"{point}.jsonl")
    folds = StratifiedKFold(n_splits=5, shuffle=True, random_state=11)
    per_fold = []
    calibrated = []
    sweep = {t: [] for t in (0.55, 0.65, 0.75, 0.85)}
    for train_idx, test_idx in folds.split(texts, gold):
        train_texts = [texts[i] for i in train_idx]
        train_gold = [gold[i] for i in train_idx]
        reflex.train(point, train_texts, train_gold)
        head = reflex._heads[point]
        train_probas = head.predict_proba(reflex._embed(train_texts)).tolist()
        threshold = calibrate_threshold(train_probas, train_gold)
        calibrated.append(threshold)
        probas = head.predict_proba(reflex._embed([texts[i] for i in test_idx])).tolist()
        truths = [gold[i] for i in test_idx]
        per_fold.append(threshold_metrics(probas, truths, threshold))
        for t in sweep:
            sweep[t].append(threshold_metrics(probas, truths, t))

    def mean(key: str) -> float:
        return statistics.mean(f[key] for f in per_fold)

    threshold = statistics.mean(calibrated)

    # Held-out decision log: train on the first 80%, calibrate on it, log the rest.
    cut = len(texts) - max(1, len(texts) // 5)
    reflex.train(point, texts[:cut], gold[:cut])
    head = reflex._heads[point]
    train_probas = head.predict_proba(reflex._embed(texts[:cut])).tolist()
    log_threshold = calibrate_threshold(train_probas, gold[:cut])
    held_out = list(range(cut, len(texts)))
    logged = []
    for i in held_out:
        decision = reflex.decide(point, texts[i], log_threshold)
        if log_path is not None:
            append_decision_log(log_path, ids[i], texts[i], decision)
        logged.append((decision, gold[i]))

    # Warm timing loop over every corpus item, then a tight single-item loop.
    for _ in range(10):
        reflex.decide(point, texts[0], threshold)
    samples = []
    for text in texts[:TIMING_RUNS]:
        samples.append(reflex.decide(point, text, threshold).latency_ms)
    samples.sort()

    return {
        "n": len(texts),
        "threshold": round(threshold, 3),
        "standalone_acc": mean("standalone_acc"),
        "mean_coverage": mean("coverage"),
        "mean_local_acc": mean("local_acc"),
        "mean_pipeline_acc": mean("pipeline_acc"),
        "mean_escalation_rate": mean("escalation_rate"),
        "mean_escalation_precision": mean("escalation_precision"),
        "total_missed_errors": sum(f["missed_errors"] for f in per_fold),
        "latency_ms_median": samples[len(samples) // 2],
        "latency_ms_p95": samples[int(len(samples) * 0.95)],
        "latency_ms_mean": statistics.mean(samples),
        "sweep_coverage": {str(t): round(statistics.mean(m["coverage"] for m in ms), 3) for t, ms in sweep.items()},
        "sweep_local_acc": {str(t): round(statistics.mean(m["local_acc"] for m in ms), 3) for t, ms in sweep.items()},
        "sweep_missed": {str(t): sum(m["missed_errors"] for m in ms) for t, ms in sweep.items()},
    }


def main() -> None:
    log_path = LOGS / "decisions-2026-09-25.jsonl"
    if log_path.exists():
        log_path.unlink()
    reflex = Reflex()
    results = {}
    for point in DECISION_POINTS:
        results[point] = evaluate_point(reflex, point, log_path)
    print(json.dumps(results, indent=2))
    print(f"decision log: {log_path}")


if __name__ == "__main__":
    main()

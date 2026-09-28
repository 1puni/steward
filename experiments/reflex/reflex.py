"""Reflex layer: fast local judgment heads over a static embedding model.

A tiny System-1 classifier for the three Steward judgment points prototyped in
docs/reflex-layer.md: should-wake, is-diff-safe, is-task-done. Each head is a
logistic classifier over `minishlab/potion-base-8M` static embeddings (30 MB,
256-dim, pure CPU inference via model2vec). Decisions below the head's
confidence threshold are escalated to the cortex instead of decided locally.

Dependencies (kept out of the project's runtime deps on purpose):
    uv venv && uv pip install model2vec scikit-learn
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from model2vec import StaticModel
from sklearn.linear_model import LogisticRegression

DECISION_POINTS = ("should_wake", "is_diff_safe", "is_task_done")

# Confidence below which the reflex defers to the cortex. Calibrated per head
# by the reject-option rule in evaluate.py on the labeled corpora. Hand-set
# values in the 0.75-0.85 range escalated everything: with corpora this small
# the heads are rarely that confident, so thresholds must come from data.
DEFAULT_THRESHOLDS = {
    "should_wake": 0.55,
    "is_diff_safe": 0.62,
    "is_task_done": 0.55,
}

MODEL_NAME = "minishlab/potion-base-8M"


@dataclass
class Decision:
    decision_point: str
    label: int
    label_name: str
    confidence: float
    action: str  # "decide" | "escalate"
    latency_ms: float
    tentative_label: int  # the reflex's argmax even when escalating


class Reflex:
    """One embedding model shared by all judgment heads."""

    def __init__(self, model_name: str = MODEL_NAME) -> None:
        self._embedder = StaticModel.from_pretrained(model_name)
        self._heads: dict[str, LogisticRegression] = {}

    def train(self, decision_point: str, texts: list[str], labels: list[int]) -> None:
        vectors = self._embed(texts)
        head = LogisticRegression(max_iter=2000, C=1.0)
        head.fit(vectors, labels)
        self._heads[decision_point] = head

    def decide(
        self,
        decision_point: str,
        text: str,
        threshold: float | None = None,
    ) -> Decision:
        if threshold is None:
            threshold = DEFAULT_THRESHOLDS[decision_point]
        start = time.perf_counter()
        vector = self._embed([text])[0]
        head = self._heads[decision_point]
        proba = head.predict_proba([vector])[0]
        tentative = int(np.argmax(proba))
        confidence = float(proba[tentative])
        action = "decide" if confidence >= threshold else "escalate"
        latency_ms = (time.perf_counter() - start) * 1000.0
        return Decision(
            decision_point=decision_point,
            label=tentative if action == "decide" else -1,
            label_name=_label_name(decision_point, tentative) if action == "decide" else "escalated",
            confidence=confidence,
            action=action,
            latency_ms=latency_ms,
            tentative_label=tentative,
        )

    def _embed(self, texts: list[str]) -> np.ndarray:
        return np.asarray(self._embedder.encode(texts, normalize_embeddings=True))


def _label_name(decision_point: str, label: int) -> str:
    names = {
        "should_wake": ("sleep", "wake"),
        "is_diff_safe": ("review", "apply"),
        "is_task_done": ("not-done", "done"),
    }
    return names[decision_point][label]


def load_corpus(path: Path) -> tuple[list[str], list[int], list[str]]:
    texts, labels, ids = [], [], []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            row = json.loads(line)
            ids.append(row["id"])
            texts.append(row["text"])
            labels.append(int(row["label"]))
    return texts, labels, ids


def append_decision_log(log_path: Path, item_id: str, text: str, decision: Decision) -> None:
    """Every reflex decision and its confidence lands in a committed JSONL log."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "decision_point": decision.decision_point,
        "id": item_id,
        "input": text,
        "tentative": _label_name(decision.decision_point, decision.tentative_label),
        "confidence": round(decision.confidence, 4),
        "action": decision.action,
        "label": decision.label_name,
        "latency_ms": round(decision.latency_ms, 3),
    }
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")

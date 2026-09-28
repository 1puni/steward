# The reflex layer: fast local judgment for the Steward

Status: survey + local prototype, 2026-09-25. Working code and data live in
`experiments/reflex/`.

The Steward's cortex (a frontier model) currently makes every judgment:
whether an incoming desk event is worth waking for, whether a diff may be
applied without review, whether a task is actually done. Each of those is a
full model turn — seconds of latency and real cost — for decisions that are
usually obvious. A **reflex layer** is a small, local, decision-only
classifier in front of the cortex: it answers the obvious cases in
milliseconds and escalates the uncertain ones. This document surveys that
landscape and reports a measured prototype on this harness's own machine.

What "reflex" means here, precisely:

- **Decision-only.** No generation. Typed input in, a label and a confidence
  out. A wrong reflex answer is a wrong routing decision, never invented
  prose.
- **Local and cheap.** Runs on the controller's CPU, no API spend, no network
  dependency in the decision path.
- **Escalating.** Confidence below a calibrated threshold defers to the
  cortex. The reflex never *blocks* the cortex; it only filters.

## Landscape survey

### Jev — closed System-One decision model

[Jev](https://typesafe.ai/blog/introducing-system-one-models-and-jev) is
TypeSafe AI's hosted "System One Model" (announced 2026-09-15): unstructured
state in, typed probabilistic decisions out. It never generates text; its
question primitives are **Choice** (pick from a list), **Score** (rubric
scoring) and **Noul** (0–1 statement truth), each answered with a typed value,
a probability distribution and a confidence in a single parallel (non-
autoregressive) forward evaluation. Training is RLCD — reinforcement learning
against proper scoring rules — so confidence is a first-class trained output.

- **Latency:** vendor-claimed 70–500 ms end to end; an independent measurement
  (published in Laya's README, adversarially sourced) measured 236–276 ms p50
  through the API.
- **Cost:** $0.042/M input tokens, output free; claimed 40–400× cheaper than
  a frontier model on decision-shaped work.
- **Local feasibility: none.** Closed weights, hosted only. From this VPS it
  is a remote fast path, subject to network round trips and availability.
- **Escalation:** no built-in abstention; the caller thresholds on confidence.
  The [REFLEX paper](https://arxiv.org/abs/2609.26532) builds exactly this
  pattern — Jev decides, a strong LLM catches low-confidence cases — and
  reports 95 % task success with 72.7 % fewer strong-model calls.

### Laya — the open alternative

[Laya](https://github.com/NandhaKishorM/laya) (Convai Innovations,
Apache-2.0, v0.3.x as of 2026-09-25) is an explicitly positioned open-source
Jev alternative: an encoder-only decision engine (ModernBERT-large backbone,
421 M parameters, English; 322 M multilingual) with a trained decision head
that scores options at `[MASK]` tokens in one forward pass. Same three
question primitives as Jev, plus per-answer probability, `confidence`
(1 − normalized entropy) and `answer_confidence`.

- **Latency:** 33–40 ms/question on a T4 GPU; measured **193–464 ms/question
  on CPU** with the checkpoint resident. Cold reload after eviction is ~7 s on
  CPU — keep it loaded.
- **Cost:** free, open weights (~0.8 GB fp32).
- **Local feasibility: good.** Fits this box (8 cores, ~11 GB free) easily,
  via PyTorch or the `laya[onnx]` runtime. No llama.cpp/GGUF support.
- **Escalation:** first-class. Threshold-based branching plus an act/escalate
  head. The docs are candid about calibration: shipped checkpoints are
  over-confident (ECE 0.47/0.31), `answer_confidence` is the recommended gate
  (AUROC 0.77), and thresholds should be refit per task on held-out data —
  the same conclusion our prototype reached independently.

### Semantic routers

**[RouteLLM](https://github.com/lm-sys/RouteLLM)** (LMSYS/Anyscale,
Apache-2.0) routes each query to a strong or weak model. Routers are trained
on ~80k Chatbot Arena preference battles; four strategies ship (matrix
factorization, similarity-weighted Elo, a fine-tuned BERT classifier, a
fine-tuned causal LLM). The router emits a win-probability score and a
per-request cost threshold sets the strong-model percentage — the same
calibrate-your-threshold lesson everywhere in this landscape. Measured
savings at 95 % of GPT-4 quality: **>85 % on MT Bench, 45 % on MMLU, 35 % on
GSM8K** ([LMSYS blog](https://www.lmsys.org/blog/2024-07-01-routellm/)).
Routing latency is unpublished; third-party estimates put ML routers at
5–50 ms. Local feasibility is partial: the `bert` router is CPU-friendly, but
the default `mf`/`sw_ranking` routers need OpenAI embeddings — not fully
offline. It is also a generation proxy, not a decision-only call.

**[vLLM Semantic Router](https://github.com/vllm-project/semantic-router)**
(Red Hat/IBM/AMD-affiliated, Apache-2.0, launched 2025-09) is the most
capable classifier in the survey: a fine-tuned **ModernBERT** (~150 M) doing
multi-task inline classification (domain, reasoning need, PII, jailbreak) in
a Rust core with Candle/ONNX/OpenVINO bindings, feeding boolean
signal→decision→policy trees — semantic caching, semantic load balancing,
selective reasoning. The ["When to Reason" evaluation](https://arxiv.org/html/2510.08731v1)
reports +10.2 pp accuracy with −47 % latency and −48.5 % tokens by routing
simple queries away from chain-of-thought; follow-up performance work cut
routing latency 4,918 ms → **50 ms with a <800 MB footprint**, explicitly
sized to run without a dedicated GPU. But it is architected as an Envoy +
Kubernetes serving-layer component, not a library you call for one decision;
standalone use of the classifier core is possible but unsupported.

**[semantic-router](https://github.com/aurelio-labs/semantic-router)**
(Aurelio AI, MIT) is the closest existing thing to what this prototype
needs: a purely decision-only Python library. Routes are example utterances;
incoming text is embedded and matched by similarity with a configurable
threshold, and below-threshold inputs return no route rather than a wrong
one. Vendor-claimed **~4 ms per decision** (encoder-dominated; low tens of
ms on CPU with a local ONNX encoder, estimated — no independent CPU
benchmark published). Fully offline-capable (FastEmbed/ONNX + NumPy index)
and trivially within this box's budget. Its threshold-optimization docs again
insist per-route thresholds are tuned data, not defaults.

Also notable, one line each: **LiteLLM** routes on load/latency/cost
strategies, not semantics; **FrugalGPT** cascades after partial generation
(up to 98 % cost cut on some benchmarks) rather than before;
**NotDiamond/RoRF** offers a random-forest-on-embeddings router;
**OpenRouter `:auto`** is hosted-only; **GraphRouter** and **Smoothie** are
academic follow-ups (GNN query↔LLM modeling; label-free weak supervision).

| | RouteLLM | vLLM Semantic Router | Aurelio semantic-router | Laya | Jev |
|---|---|---|---|---|---|
| License | Apache-2.0 | Apache-2.0 | MIT | Apache-2.0 | closed |
| Decision latency | 5–50 ms (est.) | ~50 ms optimized | ~4–10 ms | 193–464 ms CPU | 70–500 ms hosted |
| Decision-only | router yes, product no | no (serving layer) | yes | yes | yes |
| Local on this box | partial | classifier core yes, stack no | yes | yes | no |
| Confidence + escalation | threshold on score | policy plugins | threshold → no-route | act/escalate head | caller thresholds |

### Open process reward models

PRMs score *intermediate reasoning steps*; outcome reward models (ORMs) score
*final results*. OpenAI's ["Let's Verify Step by Step"](https://arxiv.org/abs/2305.20050)
(ICLR 2024) established the case — process supervision beat outcome
supervision 78 % vs 72 % on MATH — and released PRM800K. For this harness's
three decisions, the relevant distinction is: `is-task-done` and `is-diff-safe`
are *outcome* judgments (ORM / preference-model territory), while a step-level
PRM would matter for judging an agent's intermediate work.

What is open and how fast:

- **Math-specialist classifier-head PRMs** — [Skywork-PRM-Qwen-2.5-1.5B](https://huggingface.co/Skywork/Skywork-o1-Open-PRM-Qwen-2.5-1.5B)
  and [Qwen2.5-Math-PRM-7B](https://huggingface.co/Qwen/Qwen2.5-Math-PRM-7B)
  score a step with a **token-probability readout after one forward pass** —
  no decoding, prefill-only cost. The 1.5B is ~1 GB at Q4 and plausibly
  tens-to-hundreds of ms per judgment on this box's 8 cores (engineering
  estimate; no public PRM-specific CPU benchmark exists, and llama.cpp does
  not natively expose the custom heads, so a runtime shim is needed).
  Math-Shepherd (7B) and RLHFlow's Llama3.1-8B-PRM are stronger but heavier
  and equally math-bound.
- **Domain-matched agent PRMs** — [Web-Shepherd](https://arxiv.org/abs/2505.15277)
  (web-navigation steps, beats GPT-4o as a judge on WEBPRMBench) and
  [AgentPRM](https://arxiv.org/abs/2502.10325) (soft value estimates over
  general agent trajectories) prove the pattern generalizes past math, at
  3B–70B scale.
- **The ORM that fits our decisions** — [Skywork-Reward-V2](https://huggingface.co/Skywork/Skywork-Reward-V2-Llama-3.1-8B)
  (0.6B–8B) is a general preference scorer, the right shape for "is this diff
  acceptable"; 0.6B is CPU-realistic (CC-BY-NC — verify terms before product
  use).
- **Trend warning:** the field is moving toward *generative* PRMs
  (GenPRM, ThinkPRM — critique-then-score), which are slower, not faster.
  The compact "PRM-as-classifier" slot is currently filled by small
  classifier-head models or self-distilled heads in the
  [PRIME](https://arxiv.org/abs/2502.01456) implicit-PRM style (an
  outcome-trained RM converted to step scores cheaply, no process labels).

For this prototype, none of these beat a 30 MB static embedder at
sub-millisecond latency on 30–50 training examples. They become the right
tool when the reflex must judge *reasoning quality* rather than surface
structure, or when thousands of labeled outcomes make a trained head worth
its weight.

### Learning to defer

The theory under every threshold in this survey:

- **Learning to defer with an expert** ([Mozannar & Sontag, ICML 2020](https://dl.acm.org/doi/10.5555/3524938.3525594);
  survey: [Hendrickx et al. 2024](https://link.springer.com/article/10.1007/s10994-024-06534-x)):
  defer when the expected loss of deferring (expert cost + expert error)
  beats the classifier's expected loss. The rule is cost-sensitive thresholding
  on estimated correctness, and the rejector should train against the strong
  model's answers, not just ground truth.
- **LLM cascades** — [FrugalGPT](https://arxiv.org/abs/2305.05176) (up to
  98 % cost cut on some benchmarks, escalating on a tuned threshold);
  [Yue et al. ICLR 2024](https://arxiv.org/abs/2310.03094) replace greedy
  threshold tuning with a joint probabilistic model of cascade correctness
  under a budget; [Zhang et al. 2025](https://arxiv.org/abs/2502.09054) add a
  final *abstain* option distinct from escalation.
- **Routers as learned deferral** — RouteLLM's BERT router is exactly a
  binary cost-sensitive classifier; conformal variants ([RouteNLP](https://openreview.net/forum?id=route-nlp))
  add distribution-free risk control on escalation quality — 58 % cost cut at
  91 % acceptance in a multi-week production pilot.
- **Small judges gating big models** — ["Reject Before You Run"](https://ceur-ws.org/Vol-3169/paper4.pdf)
  trains tiny assessors to predict big-LLM correctness per instance and
  reject likely failures *before* inference — the exact shape of this
  prototype. [Calibration research](https://arxiv.org/abs/2305.14975) says
  verbalized confidence beats logprobs as a zero-training signal; a trained
  head wins once ~1K labeled outcomes exist.

Realistic expectations from published systems: **40–60 % escalation-driven
cost reduction at ≤1–5 % quality loss**. And the rule our prototype hit
empirically is in the literature too: keep *abstain* (no answer, no
escalation) distinct from *defer* (escalate), and tune thresholds jointly on
held-out data under an explicit budget — never by hand.

## What was actually built here

Three judgment points, grounded in this harness's real surfaces:

| Point | Where it lives today | Reflex input |
|---|---|---|
| `should_wake` | desk inbox (`steward_harness/desk/drain.py`) → full model turn per accepted message | `source=<desk|telegram|probe|rhythm|deploy> \| text` |
| `is_diff_safe` | integration review: cortex reads every candidate diff | commit subject + file count + changed top-level areas |
| `is_task_done` | disposition reading: cortex parses every task's final report | disposition body text |

Substrate choice: **the fastest local option that exists** — a static
embedding model (`minishlab/potion-base-8M`, 30 MB, 256-dim, distilled to a
lookup table) plus one logistic head per decision point. No llama.cpp, no
torch serving: model2vec's static encoder is a tokenizer plus a matrix
multiply, which is why a decision costs **under a millisecond**. Laya was not
used because its CPU decision latency (193–464 ms) is 200× slower for the
same decision-only shape; it becomes interesting when a *trained* decision
head's accuracy is needed rather than a trained-on-30-examples classifier.

### Method

- **Corpora** (54 + 47 + 35 items): `is_diff_safe` items are real commits from
  this repository's history (provenance: `git:<sha>` in the data files);
  the others are written in the harness's own desk/probe/rhythm and
  disposition vocabulary, including deliberately near-duplicate opposing
  pairs so escalation has something honest to bite on.
- **Gold labels are the cortex's own judgments** — the full model (GLM,
  this session) labeled every item before any classifier ran. `is_diff_safe`
  policy: docs/tests/config-only changes are safe; any `src/steward_harness`
  runtime change, authority-flow commits, or risky operations need review.
- **Evaluation:** 5-fold stratified CV. Thresholds are **calibrated per fold**
  by a reject-option rule (escalate at and below the highest confidence any
  training error reached), not hand-picked. Latency is a warm timing loop.
- **Logging:** every held-out decision, its confidence and its action land in
  `experiments/reflex/logs/decisions-2026-09-25.jsonl`, committed to Git.

### Results

Measured on the controller box (8 cores, ~11 GB free RAM, model resident):

| Metric | `should_wake` | `is_diff_safe` | `is_task_done` |
|---|---|---|---|
| latency median | **0.82 ms** | **0.81 ms** | **0.98 ms** |
| latency p95 | 0.95 ms | 1.03 ms | 1.16 ms |
| standalone accuracy (forced) | 51 % | 91 % | 91 % |
| coverage (decided locally) | 46 % | 63 % | 80 % |
| local accuracy on decided | 80 % | 90 % | 91 % |
| escalation rate | 54 % | 37 % | 20 % |
| escalation precision | 0.66 | 0.13 | — |
| pipeline accuracy (with cortex on escalations) | 86 % | 95 % | 94 % |
| confident-but-wrong (5 folds total) | 7 | 3 | 2 |

Escalation precision = fraction of escalated items where the reflex's own
answer was actually wrong, i.e. the escalation saved an error. For contrast,
one cortex turn on these inputs costs seconds and API spend; the reflex
decides in **under a millisecond, roughly three orders of magnitude faster**,
and gets 80–91 % of what it does decide right.

### Findings

1. **The two structural judgment points work.** `is_diff_safe` and
   `is_task_done` are lexical-structural: "src/steward_harness" in the areas
   line, "Disposition: continue/ask" in the body. A 30 MB static embedder
   reaches 91 % standalone and 90–91 % local accuracy at 63–80 % coverage —
   on genuinely new commits and disposition texts, not memorized ones.
2. **`should_wake` does not work yet, honestly.** 51 % standalone is coin-flip
   territory. The corpus's hard pairs ("seemed slow, maybe check" vs "seemed
   fine, no worries") differ by one polarity word, and static embeddings
   cannot reliably carry that. The reflex still escalates 54 % of that corpus
   (0.66 precision — its uncertainty is *correct* here), but a wake filter
   that coin-flips half its inputs is not worth wiring in. This head needs a
   contextual decision model (Laya's trained head shape) or richer features
   (sender, thread state, time since last wake), not a bigger static embedder:
   potion-base-32M moved it only 51 % → 56 %.
3. **Thresholds must be calibrated, never hand-set.** Guessed 0.75–0.85
   thresholds escalated 100 % of items — small corpora never produce that
   much confidence. The reject-option calibration (train-error maximum) is
   what produced the coverage numbers above, and matches Laya's docs
   insisting thresholds are per-task policy to refit on held-out data.
4. **Escalation is cheap honesty.** Pipeline accuracy (86–95 %) exceeds
   standalone accuracy everywhere, and the committed decision log makes every
   reflex answer auditable after the fact: tentative label, confidence,
   action, latency.

## Recommendation

Wire `is_diff_safe` and `is_task_done` reflexes in as **advisory loggers
first** — run them beside the cortex on real traffic, commit their decisions,
and measure agreement on live data before letting them gate anything. Leave
`should_wake` to the cortex until it gets a contextual head (Laya CPU, or a
trained classifier on accumulated desk history) or richer input features.
The reflex layer's contract — decide in <1 ms, escalate uncertainty, log
everything to Git — fits the harness's existing shape: it is another
requirement-enforced observer, not a new trust boundary.

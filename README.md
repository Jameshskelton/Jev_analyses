# Jev vs. cosine similarity: what a semantic cache should actually ask

A semantic cache stores `(query, response)` pairs and serves a stored response when a new query looks "similar enough." Embedding similarity makes "similar enough" cheap — and wrong. This repo is the experiment behind the article in [`drafts/article_draft.md`](drafts/article_draft.md): it measures how often a cosine-similarity cache serves a wrong answer, and whether a model that instead asks *"does this stored response answer this new question?"* does better.

The headline, on 500 labeled pairs (customer support + open-domain QA), scored against an adjudicated gold set:

| System | AUC | Precision @ 0.5 | Hit @ 1% false-hit | Latency (p50) | Cost / 1k |
|---|---|---|---|---|---|
| **Jev (System One)** | **0.89** [0.86, 0.92] | **0.92** | **0.30** | ~416 ms | $0.017 |
| LLM `glm-5.3` (prompt) | 0.90 [0.87, 0.93] | 0.58 | 0.00 | ~7,858 ms | $0.68 |
| LLM `gpt-4o-mini` (prompt) | 0.85 [0.80, 0.89] | 0.64 | 0.06 | ~1,170 ms | $0.049 |
| LLM `claude-haiku-4.5` (prompt) | 0.85 [0.81, 0.89] | 0.63 | 0.29 | ~1,572 ms | $0.59 |
| Cosine, GTE-Large v1.5 (query→query) | 0.81 [0.76, 0.85] | 0.30 | 0.21 | ~44 ms | $0.002 |
| Cosine, GTE-Large v1.5 (query→response) | 0.81 [0.76, 0.85] | 0.33 | 0.09 | ~50 ms | $0.005 |

Read the precision column: at the default 0.5 threshold, cosine serves a **majority-wrong** set. A general-purpose LLM ranks about as well as Jev but returns probabilities that cannot be used as a tight threshold; Jev's edge is a calibrated, thresholdable probability at low cost and latency.

## Findings in brief

- **"Similar" ≠ "answers the same question."** Cosine similarity captures the topic and misses the proposition: swapped entities, changed numbers, narrowed scope, negated intent.
- **A default cosine cache is wrong most of the time.** Roughly two-thirds of the responses it serves at a natural threshold are incorrect.
- **Jev does not out-rank a strong LLM; it out-calibrates it.** At a 1% false-hit budget, Jev recovers more of the true hits, and at a fraction of the cost and latency.
- **Include the cached query in the verification state.** Dropping it costs ~0.12 AUC (0.89 → 0.77).

## Repo layout

```
.
├── article_draft.md / drafts/     # the write-up
├── build_pool.py                  # build the candidate pair pool from HuggingFace
├── build_pilot_pairs.py           # small pilot set; shared helpers for build_pool
├── make_labeling_sheet.py         # stratified sample + blinded duplicates
├── run_judge.py                   # independent LLM judge (labels)
├── build_adjudication.py          # group label disagreements
├── make_gold_sets.py              # strict / lenient gold sets
├── make_adjudicated_gold.py       # frozen adjudicated gold set
├── sheet_to_labels.py             # filled sheet -> labels JSONL
├── run_pilot.py                   # run Jev + cosine baselines; score vs gold
├── run_llm_baseline.py            # general-LLM yes/no baseline
├── analyze.py                     # headline table + bootstrap CIs
├── agreement.py                   # label agreement / test-retest
├── compare_runs.py                # hosted-model drift between two runs
├── baselines.py / costs.py        # embedding backends; pricing
└── tests/                         # unittest suite (no network, no key needed)
```

`results/` and `sessions/` are local-only (gitignored): generated datasets, labels, run outputs, and agent transcripts. The scripts rebuild everything in `results/`.

## Setup

Python 3.10+ (developed on 3.11).

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Environment variables:

| Variable | Used for |
|---|---|
| `MODEL_ACCESS_KEY` | DigitalOcean model access key (Jev, embeddings, LLM baselines) |
| `TYPESAFE_URL`, `TYPESAFE_MODEL` | override the Jev endpoint/model |
| `DO_EMBED_MODELS`, `OPENAI_API_KEY` | optional alternative embedding backends |

The systems under test are reached through DigitalOcean's OpenAI-compatible serverless endpoints (`https://inference.do-ai.run/v1/...`); Jev uses the non-OpenAI `/v1/systemone` endpoint.

## Pipeline

Run from the repository root. Every step reads/writes files in the working directory.

### 1. Build the candidate pool

```bash
python build_pool.py --out pool.jsonl --seed 13
```

Pulls real records from `bitext/Bitext-customer-support-llm-chatbot-training-dataset` and `mandarjoshi/trivia_qa`, assembles pairs (paraphrases, entity swaps, number changes, unrelated, same-answer), and assigns a leakage-safe dev/test split. No text is generated.

### 2. Build the labeling sheet

```bash
python make_labeling_sheet.py --pool pool.jsonl --n 500 --duplicates 50
```

Writes `labeling_sheet.csv` (500 unique + 50 blinded duplicates), `labeling_key.json` (rubric + mapping), and `sample.jsonl` (the unique pairs).

### 3. Label

Independent LLM judge (pick a model that is not Jev and not your other labeler):

```bash
export MODEL_ACCESS_KEY=...
python run_judge.py --pairs sample.jsonl --model deepseek-v4-pro-0813 --out judge_labels.jsonl --stamp
```

A second labeler is required for the two-rater adjudication. In the original run this was an LLM's labels stored as `assistant_prelabels.jsonl` with the schema `{"row_id", "label", "confidence"}` (one row per sheet row). To reproduce with scripts, run `run_judge.py` with a second model and map its `id` back to `row_id` via `labeling_key.json`, or use `sheet_to_labels.py` on a human-filled `labeling_sheet.csv`.

### 4. Build gold labels

```bash
python make_gold_sets.py --judge judge_labels.jsonl --prelabels assistant_prelabels.jsonl --key labeling_key.json
python make_adjudicated_gold.py --judge judge_labels.jsonl --prelabels assistant_prelabels.jsonl \
    --key labeling_key.json --out gold_adjudicated.jsonl
```

`make_gold_sets.py` writes `gold_strict.jsonl` and `gold_lenient.jsonl` (the sensitivity pair); `make_adjudicated_gold.py` writes the frozen policy used for the headline (same-answer → hit, non-answer templates → miss).

### 5. Run the systems and score them

```bash
python run_pilot.py --pairs sample.jsonl --state-mode all \
    --baselines tfidf,do --do-embed-model gte-large-en-v1.5 \
    --labels gold_adjudicated.jsonl --out results.jsonl --summary summary.json --stamp

python run_llm_baseline.py --pairs sample.jsonl \
    --models glm-5.3,openai-gpt-4o-mini,anthropic-claude-haiku-4.5 \
    --out llm_results.jsonl --stamp
```

### 6. Headline table with bootstrap CIs

```bash
python analyze.py --gold gold_adjudicated.jsonl \
    --results results_<ts>.jsonl,llm_results_<ts>.jsonl \
    --domain ALL --n-boot 2000 --out ci.json
```

Also useful: `agreement.py` (label agreement), `compare_runs.py` (hosted-model drift between two `run_pilot` outputs), `build_adjudication.py` (disagreement view).

## Tests

No network or API key required:

```bash
python -m unittest discover -s tests
```

## Costs

Prices used by `costs.py` are DigitalOcean serverless rates, USD per 1M tokens (see the file header for the date and sources): Jev input `$0.042`; embeddings `gte-large-en-v1.5` `$0.09`, `bge-m3`/`e5-large-v2` `$0.02`; chat models per the table above. LLM baseline figures include a requested `reason` field and, for `glm-5.3`, always-on reasoning, which inflates their cost/latency relative to a minimal-output prompt.

## Limitations

- **Ground truth is model-generated.** Labels came from two independent LLMs plus a fixed adjudication rule, not human annotation. The headline is reported under three labeling policies; the direction is stable, but a human audit of a stratified sample is the intended next step.
- **Small n.** 500 pairs (125 positive). Ranking and precision differences are statistically solid; the tight-budget hit-rate numbers have wide confidence intervals and are directional.
- **No held-out test presentation.** All metrics are reported at n=500; tightening the budget claims needs a larger labeled set.
- **Templated support responses** compress separability, since many stored replies are procedural acknowledgments that contain no answer.
- **Leakage.** Public datasets (TriviaQA) may appear in models' training data.
- **One run per system.** A hosted model is not bit-identical; see `compare_runs.py` (about 29% of Jev scores moved between back-to-back runs, with decisions stable at the chosen threshold).

## Data and licensing

Datasets belong to their upstream sources (`bitext`, `mandarjoshi/trivia_qa`) and are downloaded at build time; their licenses apply. Model access is subject to DigitalOcean's and TypeSafe's terms (Jev is subject to the TypeSafe Master Customer Agreement).

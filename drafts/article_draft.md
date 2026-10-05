# Similar isn't the same as answered

### We tested 500 query pairs to see whether a semantic cache can tell "sounds alike" from "answers the same question." Cosine similarity can't. Here's what does, and what it costs.

## The promise and the trap

A semantic cache is one of the most appealing ideas in applied LLM engineering. Keep a store of past `(query, response)` pairs. When a new query arrives, embed it, find the nearest stored query, and if it's "similar enough," serve the stored response instead of calling a model again. Done well, it can cut cost and latency dramatically.

There's a trap in the word *similar*. Embedding similarity measures whether two texts are about the same thing. A cache needs to know something narrower: **does this stored response actually answer this new question?** Those are different questions, and the gap between them is where production traffic goes wrong.

Consider two queries: *"What is the capital of Kenya?"* and *"What is the capital of Jamaica?"* As embeddings they are nearly indistinguishable. Their answers have nothing in common. A cosine cache tuned loosely enough to serve paraphrases will happily serve "Nairobi" for a question about Jamaica. That is a **false hit** — the cache reports a win and hands the user a confident, wrong answer, and no downstream model ever sees the query.

This article is an empirical look at how big that problem is, and at an alternative: instead of asking whether two queries are *similar*, ask directly whether the stored response **contains a correct answer to the new query**. That is an entailment-style judgment, and it's the kind of narrow, structured decision a purpose-built model can make directly.

## How we tested it

We assembled 500 pairs from public datasets. Each pair has a *cached query*, the *cached response* that was stored, and a *new query*. The ground-truth question for every pair is: **would serving the cached response be a correct answer to the new query?**

The pairs span two domains and several difficulty slices:

- **Customer support** (Bitext): paraphrases of the same intent, plus near-miss intents where the stored response is about a neighboring topic.
- **Open-domain QA** (TriviaQA): factual question pairs including entity swaps ("capital of Kenya" → "capital of Jamaica") and number changes ("first World Cup" → "first Six Nations").
- Deliberately adversarial slices where the two queries are lexically similar but the correct answer differs, plus unrelated pairs.

We compared five systems on the same pairs:

1. **Cosine, query-to-query** using GTE-Large v1.5 embeddings (and a TF-IDF lexical baseline for reference).
2. **Cosine, query-to-cached-response** — a variant some caches use.
3. **A general-purpose LLM** asked the same yes/no question (we used `glm-5.3`, `gpt-4o-mini`, and `claude-haiku-4.5`).
4. **Jev**, TypeSafe's "System One" model, asked, in one structured call: *"Does the cached response contain a complete and correct answer to the new query?"* The model returns a calibrated probability, which the cache thresholds.
5. A state-layout ablation (see below).

We labeled truth with two independent large language models plus a fixed adjudication rule, and we stress-tested the result under three different labeling policies (strict, lenient, and an adjudicated middle). We report 95% bootstrap confidence intervals. The core claims hold under all three policies. (Labels and full method caveats are in the Limitations section.)

## The headline: a default cosine cache is wrong most of the time

At the natural 0.5 threshold, here is what each system does on all 500 pairs:

| System | AUC | Precision @ 0.5 | Hit rate @ 1% false-hit | Hit rate @ 5% false-hit | Latency (p50) | Cost / 1k lookups |
|---|---|---|---|---|---|---|
| **Jev (query + response)** | **0.89** [0.86, 0.92] | **0.92** | **0.30** | **0.54** | 416 ms | $0.017 |
| LLM `glm-5.3` | 0.90 [0.87, 0.93] | 0.58 | 0.00 | 0.36 | 7,858 ms | $0.68 |
| LLM `gpt-4o-mini` | 0.85 [0.80, 0.89] | 0.64 | 0.06 | 0.58 | 1,170 ms | $0.049 |
| LLM `claude-haiku-4.5` | 0.85 [0.81, 0.89] | 0.63 | 0.29 | 0.29 | 1,572 ms | $0.59 |
| Cosine, GTE-L v1.5, query→query | 0.81 [0.76, 0.85] | 0.30 | 0.21 | 0.38 | 44 ms | $0.002 |
| Cosine, GTE-L v1.5, query→response | 0.81 [0.76, 0.85] | 0.33 | 0.09 | 0.26 | 50 ms | $0.005 |
| Cosine, TF-IDF, query→query | 0.53 [0.48, 0.58] | 0.20 | 0.02 | 0.08 | ~0 ms | $0 |

Read the precision column first. At the threshold most teams would ship, **cosine serves a set of responses of which roughly 70% are wrong.** It catches almost every true hit — but it also serves almost every near-miss. The false-hit rate is the number that matters, because a semantic cache's entire value proposition is that a hit is *correct*, and its entire risk is that a hit is confidently wrong.

On the query pairs where both of two independent labelers agreed on the ground truth — the adversarial entity-swap and number-change slices — cosine served essentially all of them and got essentially none right. That isn't a threshold-tuning problem; it's the definition of the task.

## Why it happens

The failure isn't random. It concentrates in exactly the categories a semantic cache is supposed to handle.

**Entity swaps.** *"What is the capital of Kenya?"* → stored answer "Nairobi." New query: *"What is the capital of Jamaica?"* Cosine similarity between the two queries is very high. "Nairobi" is served. Wrong.

**Number changes.** *"In what year was the first World Cup held?"* → "1930." New query: *"In what year was the first Six Nations Championship played?"* Again nearly identical embeddings, again the wrong year.

**Scope and action.** *"Getting invoices from last month"* → a stored reply about last month's invoice. New query: *"get invoices from nine months ago."* The stored response names the wrong period.

**Opposite intent.** *"Receive the corporate newsletter"* versus *"unsubscribe from the corporate newsletter."* Lexically close, semantically opposed.

In every case, the embedding captures the *topic* and misses the *proposition*. Cosine similarity has no mechanism to represent "this response answers that question," so no amount of threshold tuning recovers it: raising the threshold to filter the near-misses also filters the true paraphrases, because both are "similar."

## Is this just "cosine is bad"? No — and the LLM baselines make the story more interesting

The obvious objection is that we've strawmanned the baseline: of course a purpose-built verifier beats raw cosine. The honest test is whether Jev beats **simply asking an LLM the same question.** That's the real commercial question: if a prompt gets you there, why add a model?

We ran it, and the answer is more nuanced than a leaderboard.

**On clear-cut QA, Jev is excellent and the LLMs split.** Jev reaches AUC 0.92, precision 1.00 at the natural threshold, and a 0.70 hit rate at a 1% false-hit budget. `glm-5.3` ties the ranking (AUC 0.92) but its precision at the operating threshold is 0.68 and it recovers almost no hits at a tight budget. The small models rank worse (`gpt-4o-mini` AUC 0.67, `claude-haiku-4.5` 0.78).

**On messy customer-support text, the LLMs actually rank slightly *better* than Jev — but their scores are unusable at the operating point.** On the support slice, AUC is 0.90–0.91 for the LLMs versus 0.86 for Jev (intervals overlap), yet precision collapses to 0.55–0.63, and at a 1% false-hit budget Jev recovers 22% of the true hits while the LLMs recover about 2% — or, for `gpt-4o-mini`, literally cannot reach the budget at all. The LLMs know the ranking but return probabilities centered in the wrong place, so a fixed threshold can't use them.

This reframes the value of a model like Jev. Its advantage over a general LLM is **not raw accuracy** — on ranking, a big LLM is every bit as good. The advantage is that Jev returns a **calibrated probability you can threshold** to meet an error budget, and that it does so at a fraction of the cost and latency.

## One detail that quietly matters: put the cached query in the state

We ablated how the question is posed. Asking "does this response answer this query?" while giving the model **both** the cached query and the cached response is much better than giving it only the response:

| State given to the model | AUC |
|---|---|
| Cached query + response + new query | **0.89** |
| Plain-string version of the same | 0.88 |
| Response + new query only | 0.77 |

Without the cached query, the model can't see what the stored response was *for*, and its judgment degrades sharply. This is a free design choice with a large effect.

## Calibration, robustness, and the limits of a single threshold

Two operational findings that matter if you intend to ship this:

**The probabilities live low.** Jev's useful thresholds sit around 0.05–0.13, not 0.5. The default 0.5 threshold badly under-serves. A cache should tune its threshold on labeled data and pick a point with margin, not inherit a default.

**A hosted model is not bit-identical.** Across two back-to-back runs of the same 150 evaluations, about 29% of Jev's scores moved by a small amount (mean |Δ| 0.009, p95 0.05, max 0.20). At the chosen operating threshold (0.05) the decisions were stable — zero flips on the primary configuration — but thresholds placed in the dense low-score region (e.g., 0.02) flipped on roughly 5% of pairs. The practical lesson: **choose a threshold with margin, and don't over-interpret differences smaller than the model's own run-to-run noise.**

## Cost and latency

Accuracy isn't free, and a cache's whole point is economics. On the same pairs:

- **Cosine:** ~44–50 ms per lookup, ~$0.002–0.005 per 1,000 lookups. But you're buying a confident-wrong answer two thirds of the time.
- **A general LLM prompt:** 1.2–7.9 seconds and $0.05–$0.68 per 1,000 lookups depending on the model. (These figures are inflated somewhat because our prompt asked the model to include a short reason; a terser prompt would be cheaper.)
- **Jev:** ~416 ms and ~$0.017 per 1,000 lookups, with the best precision.

The commercial reading is a two-stage design: let embeddings do the cheap retrieval to shortlist candidates, then let Jev make the yes/no call on the shortlist. Embeddings are fast and cheap but can't be trusted as the final word; a verifier is cheap enough per lookup to be the gate. (We did not separately benchmark the full two-stage pipeline; that's the natural next experiment.)

## Limitations

We'd rather state these plainly than have a reader find them.

- **Ground truth is model-generated.** Labels came from two independent LLMs plus a fixed adjudication rule — not from human annotation. We stress-tested the headline under three labeling policies (strict, lenient, adjudicated); the direction is identical, which is reassuring, but a human audit of a stratified sample is the obvious next step before treating the numbers as final.
- **Small n.** 500 pairs, 125 positive hits. Ranking and precision differences are large enough to be statistically solid, but the hit-rate-at-tight-budget numbers have wide confidence intervals and should be read as directional. Scaling the labeled set is how those tighten.
- **The support responses are templated.** Many stored support replies are procedural acknowledgments ("I'm here to help — could you share your order number?"). Under a strict reading, those don't "contain an answer," which compresses how much any system can separate there. The QA domain is where the task is cleanest.
- **One run per system, one embedding family.** We used GTE-Large v1.5 and TF-IDF as the cosine baselines and three LLMs; other embeddings and models may shift the absolute numbers (though the near-miss failure mode is structural).
- **Leakage.** Public datasets like TriviaQA may appear in some models' training data; a fresh, private slice would strengthen the external validity.
- **Cost fairness.** The LLM cost and latency figures include a requested reason field and, for `glm-5.3`, always-on reasoning. A minimal-output prompt would narrow the cost gap versus the cheapest small LLM.

## Takeaways

- **Cosine similarity is not a correctness signal, and a semantic cache needs one.** "Similar" and "answers the same question" diverge exactly on the near-misses that dominate real traffic: swapped entities, changed numbers, narrowed scope, negated intent.
- **At a default threshold, cosine serves a majority-wrong set.** The precision number, not the hit rate, is what a product owner should watch.
- **You don't need a frontier LLM in the cache path, but you do need calibration.** A big LLM can rank as well as Jev; it just can't hand you a probability you can threshold to a budget, at least not at these prices and latencies.
- **Feed the verifier both the cached query and the response.** It's a one-line change with a large accuracy effect.
- **Pick your threshold with margin and validate it on labeled data.** The model's own run-to-run noise lives at the same scale as the differences near the low end of the score range.

The one-sentence version: a semantic cache that asks *"is this similar?"* ships wrong answers; one that asks *"does this response answer this question?"* — with a calibrated yes/no and a threshold you chose — is the one that actually works.

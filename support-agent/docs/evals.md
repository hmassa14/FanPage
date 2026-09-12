# Evals: pass@1 versus pass^k

## Why two numbers
Support automation fails in a specific way: it works on the demo and then handles the same
customer problem differently on Tuesday. A single accuracy number hides that.

* **pass@1** — the mean chance one attempt is fully correct. This is what most demos report.
* **pass^k** — the chance that *all k* independent attempts at the same case are correct
  (tau-bench, Yao et al. 2024). This is the reliability number a CTO should ask for. An agent
  at pass@1 = 0.90 and pass^4 = 0.62 is right most of the time and unpredictable often
  enough to lose a support lead's trust in a week.

We estimate pass^k from n trials per case with c successes as C(c,k)/C(n,k), which is
unbiased and lets you read off pass^1 … pass^n from one run.

## What "correct" means for one trial
All labeled checks for that case hold:
* gate decision (always labeled): auto_send / needs_approval / escalate / reject
* triage category (when labeled)
* the exact set of proposed side effects (when labeled): e.g. `["issue_refund"]` and nothing else
* reply content: must-include and must-not-include phrases

Separately, an **unsafe send** is any auto_send where a human was expected. One unsafe send
fails the run regardless of the averages. That asymmetry is deliberate: a wrong refund that
went out is not the same kind of error as a good reply that waited for approval.

## The component scorecard
End-to-end pass^k says *whether* the system is right. In the second week of a pilot you need
to know *which stage* is wrong. `--components` scores each one on its own:

| component | metric | how |
|---|---|---|
| triage | category accuracy, confusion pairs | labeled emails |
| retrieval | recall@1, recall@3, MRR | `evals/retrieval.jsonl`: 24 questions labeled with the policy section that answers them |
| draft | rubric (greeting, sign-off, length, cites KB, no banned phrase, no placeholder, language) | deterministic checks on every draft; plus the judge's grounding score on clean drafts |
| judge | seeded-error detection, false-positive rate | every clean draft is corrupted three ways (unsupported timeframe, unpromised $500 refund, liability admission); does the judge catch each? does it pass the clean one? |
| gates | decision agreement | current `policy.yaml` replayed over stored stage outputs |

It writes `evals/scorecard.json` and `evals/scorecard.md`, and the `/scorecard` page shows
the latest one next to live numbers from the database.

**What it caught on its first run.** Retrieval recall@3 was 0.83 on 24 queries. All four
misses had one cause: no stemming, so "return" never matched "returned". A twelve-line
suffix stripper took recall@3 to 0.92 and MRR from 0.75 to 0.85, with document-level
recall@3 at 1.0. The two remaining misses are vocabulary ("shoes" vs "footwear", "card" vs
"payment method"), which is exactly where hybrid or embedding retrieval starts to earn its
cost. That is the argument for measuring before adding infrastructure.

**BM25 versus hybrid, on the same table.** With `SA_RETRIEVER=hybrid` the scorecard adds a
row for Weaviate hybrid search next to BM25, same 24 queries. Offline, with the key-free
hash embedder, hybrid scores *below* stemmed BM25 (recall@3 0.875 vs 0.917): Weaviate's
own BM25 has no stemmer, and hashed trigrams are lexical, not semantic. That row is there
to be honest, not to look good. The row that decides anything is the one produced with
`SA_EMBEDDER=voyage` and a `VOYAGE_API_KEY`, which is part of the first live run. If Voyage
closes the two vocabulary misses without regressing the rest, hybrid earns its container.
If it doesn't, BM25 stays and you have the evidence.

An `alpha` sweep is one loop over `kb.search_hybrid(q, alpha=...)`; the harness records
whichever value is configured.

## Running it
```bash
make eval                                                        # offline: 3 trials + components, CI gate
make eval-hybrid                                                 # same, hybrid retrieval on embedded Weaviate
SA_LLM_PROVIDER=anthropic support-agent eval --trials 4 --components   # live: pass^k becomes meaningful
```
Outputs `evals/report.{json,md}` (end to end) and `evals/scorecard.{json,md}` (per component).

## The dataset
`evals/dataset.jsonl`: 14 cases covering every gate path, including a prompt-injection
attempt and a policy refusal. For a customer engagement this becomes 200–500 real tickets
labeled by their support leads, split into train (prompt tuning) and held-out test; the test
split is the only number reported.

## How the eval is used with a customer
1. Baseline the agent on their labeled set before any prompt work. Report pass@1, pass^k,
   unsafe sends, cost per ticket.
2. Every prompt, policy, or model change re-runs the eval. A change ships only if unsafe
   sends stay at zero and pass^k does not regress.
3. Human edits from the approval queue become new labeled cases. The eval grows with the
   deployment.
4. Per-stage cost and latency are on every report, so "we got 3 points of accuracy for 2x
   cost" is a visible tradeoff, not a surprise on the invoice.

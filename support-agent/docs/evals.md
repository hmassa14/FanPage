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

## Running it
```bash
make eval                                            # offline, deterministic, CI gate
SA_LLM_PROVIDER=anthropic support-agent eval --trials 4    # live, pass^k becomes meaningful
```
Outputs `evals/report.json` (machine) and `evals/report.md` (share with the customer).

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

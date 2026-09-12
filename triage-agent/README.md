# triage-agent

One small repo that covers the three prep items as a single system:

| Prep item | Where it lives |
|---|---|
| Function-calling loop, multi-turn, official Anthropic SDK | `agent.py` (manual loop), `agent_runner.py` (SDK `tool_runner`) |
| Pydantic-enforced JSON at every boundary | `schemas.py` (tool inputs, tool outputs, the decision) |
| Eval harness: per-class P/R/F1, tool pass-rate, pass^k | `eval.py` + `scoring.py` over 12 labelled cases in `cases.jsonl` |

The agent reads a support ticket, calls `lookup_customer` and (for anything
broken) `search_incidents`, and returns a `TriageDecision`: category, priority,
`needs_human`, summary, evidence.

## Run it

```bash
pip install -r requirements.txt
python -m pytest -q                      # 19 offline tests, no key needed

python eval.py --client oracle           # harness self-check: everything must read 1.000
python eval.py --client null             # constant answer: must read ~0.25 (majority baseline)

export ANTHROPIC_API_KEY=...
python eval.py                           # live, 1 rep per case
python eval.py --reps 3 --workers 4      # pass^k needs reps
python eval.py --rescore runs/<ts>/results.jsonl   # recompute metrics from saved rows

echo '{"customer_id":"cust_004","subject":"Not renewing","body":"..."}' | python agent.py
```

Model defaults to `claude-opus-5`; override with `TRIAGE_MODEL=...` or `--model`.

## The loop, and the three ways it breaks

`agent.py` is the file to delete and rewrite from empty. The loop is:

```
messages = [user ticket]
loop:
  response = client.messages.create(tools=..., output_config=..., messages=messages)
  if stop_reason == "tool_use":
      messages.append({"role": "assistant", "content": response.content})   # (1)
      results = [one tool_result per tool_use block, tool_use_id = block.id]  # (2)(3)
      messages.append({"role": "user", "content": results})
      continue
  if stop_reason == "end_turn":
      return TriageDecision.model_validate_json(text)
```

1. **Keep the assistant turn verbatim.** Append the whole `response.content`
   list, text blocks and all. Rebuilding it from just the `tool_use` blocks
   drops content the model expects to see on the next turn.
2. **One user message, one `tool_result` per `tool_use`.** Claude may emit
   several `tool_use` blocks in one turn (parallel calls). All their results go
   back in a single user message. Splitting them across messages, or omitting
   one, is a 400.
3. **Match the ids.** Each `tool_result.tool_use_id` must equal the `id` of the
   `tool_use` it answers. A failed tool still gets a result, with
   `is_error: true`, so Claude can recover; never drop it.

`tests/test_loop.py` pins all three, plus refusal / truncation / invalid-JSON /
runaway-turn handling, against a scripted fake client.

## Two API details worth having straight

**`strict: true` vs `output_config.format` are separate features that compose.**

- `strict: true` sits on a *tool definition*. It constrains what Claude passes
  to *your* function: `tool_use.input` is guaranteed to validate against
  `input_schema`. The schema needs `additionalProperties: false` and every
  property in `required`.
- `output_config: {"format": {"type": "json_schema", "schema": ...}}` sits on
  the *request*. It constrains what Claude *says* in its final text block.
  (`output_format` at the top level is the deprecated spelling; the SDK's
  `client.messages.parse(output_format=PydanticModel)` still takes a Pydantic
  type and merges it into `output_config` for you.)

Both are in one request here. Pydantic produces the right shape for both:
`ConfigDict(extra="forbid")` emits `additionalProperties: false`, and fields
without defaults land in `required`. We re-validate on our side anyway
(`model_validate` on tool inputs, `model_validate_json` on the decision)
because the harness and tests call those paths without the API in front of them.

**`client.beta.messages.tool_runner()` with `@beta_tool` writes the loop for you.**
`agent_runner.py` is the same agent in that form: decorate the functions, hand
them to the runner, iterate. Know why you'd still drop down to the manual loop:

- it's beta, and the request shapes it builds are the ones it knows about;
- it does not hand you the transcript, so anything that needs it (this eval
  harness, audit logs) has to mirror `history` itself;
- per-turn control (approval gates, retry policy, error classes, turn budgets)
  is easier in a loop you own;
- the Python runner does not auto-resume `pause_turn` (server tools).

## The harness

`eval.py` runs each case `--reps` times in a thread pool and writes
`runs/<ts>/results.jsonl` (one row per attempt that produced a decision, full
transcript included), `errors.jsonl` (refusal, truncation, invalid JSON, API
errors: classified, never scored as a wrong answer), and `report.json`.

`scoring.py` is pure functions over those rows:

- **per-class precision / recall / F1** on category, plus macro-F1, accuracy,
  and the majority-class baseline so the headline number has a floor;
- **priority exact-match**, reported separately because it's the most
  judgment-heavy label;
- **tool pass-rate**: every `required_tools` entry was called with a valid
  input (no `is_error`) and no unknown tool was called;
- **pass rate**: category AND priority AND tools all correct;
- **pass^k** = mean over cases of C(passes, k)/C(reps, k), the probability that
  k independent attempts *all* pass, and **pass@k** for contrast. Reported for
  every k up to `--reps`.

`tests/test_scoring.py` checks the math against planted failures: relabel one
churn-risk case as billing and make one bug case skip its tools, and the report
must show churn_risk recall 0.50, billing precision 0.75, tool pass-rate 11/12,
pass rate 10/12. A three-rep flaky run checks pass^k against closed-form values.

Case set notes (what an eval audit would ask): 12 hand-written cases, 3/3/2/2/2
across five classes; two decoys cover both directions of the churn-vs-billing
boundary (`b1` mentions nothing about leaving, `c2` is a billing complaint with
an explicit downgrade threat); three cases match seeded open incidents so the
`search_incidents` requirement is testable; labels are synthetic and hand-authored,
not model-generated.

## Layout

```
schemas.py       Pydantic models: Ticket, tool inputs/outputs, TriageDecision
tools.py         registry: strict tool definitions + handlers + execute()
agent.py         manual loop (rewrite this one cold)
agent_runner.py  same agent via client.beta.messages.tool_runner
fake_client.py   ScriptedClient / OracleClient / NullClient for offline runs
scoring.py       P/R/F1, tool pass-rate, pass^k, pass@k
eval.py          runner + report writer
cases.jsonl      12 labelled tickets
tests/           19 offline tests
```

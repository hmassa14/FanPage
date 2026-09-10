# Runbook

## First live run
1. `cp .env.example .env`, set `ANTHROPIC_API_KEY` and `SA_LLM_PROVIDER=anthropic`.
2. `.venv/bin/support-agent process data/samples/03_where_is_my_order.json` — one cheap,
   no-side-effect email. Confirm: triage JSON parses, research calls `get_customer` and
   `get_order`, the brief cites `shipping-policy`, the gate says `auto_send`, and the reply
   prints to the console.
3. `.venv/bin/support-agent show <ticket id>` — read the audit trail and per-stage cost.
4. `make eval` with the live provider. Expect decision accuracy ≥ 0.9 and `unsafe_sends: 0`.
   Anything else is a prompt or policy problem to fix before pointing this at a real inbox.
5. `make demo` for all 13 and look at every `needs_approval` draft in the UI.

The Claude provider was written against `anthropic` 1.5.0 but not executed live where this
was built, so step 2 is where an SDK-shape surprise would show up. The request shape is in
`llm/anthropic_provider.py::_request_kwargs`; if a beta header or parameter is rejected, the
error names it.

## Running it
* `support-agent serve` — approval UI/API. Set `SA_ADMIN_TOKEN`. Put it behind your SSO
  proxy; the auth dependency in `api/app.py` is the one thing to replace.
* `support-agent worker` — polls `data/inbox/` (drop `.eml` or `.json`) and IMAP if
  `SA_IMAP_*` are set. Run more than one for throughput; ticket claiming is exclusive.
* `docker compose up --build` — api + worker sharing one volume.
* Sending: `SA_SENDER=console|file|smtp`. Start with `file` in staging and read the `.eml`s.

## Failure modes and what you will see
| Symptom | Where to look | Likely cause |
|---|---|---|
| ticket `failed`, error names a stage | `events` table / `show` | model error after SDK retries, schema validation failed twice, tool bug |
| many `needs_approval` with `judge.missing` | logs at WARNING | judge stage failing; gates fail closed on purpose |
| `needs_approval` with `judge.grounding` | ticket page | draft asserting things not in the brief; tighten the draft prompt or research coverage |
| outbox rows `failed` | `outbox` table | SMTP rejected N times; fix transport, reset `status='pending', attempts=0` |
| `escalate.keyword` on benign mail | policy.yaml | regex too loose; add a boundary and a test |
| costs jump | `/metrics`, `usage` table | effort raised, research rounds up, cache misses (check `cache_read_tokens` > 0 after the first call) |

## Cost control
* Per-stage `SA_<STAGE>__EFFORT` is the first lever. Triage runs at `low` already.
* `SA_RESEARCH_MAX_TOOL_ROUNDS` bounds the loop.
* System prompts are cached; if `cache_read_tokens` stays 0 across tickets, something
  volatile got into the system prompt (`llm/prompts.py` must stay static).
* Try a cheaper model on one stage (`SA_JUDGE__MODEL=claude-sonnet-5`) and re-run `make eval`
  before and after. Decide on the numbers.

## Security notes
* Tools are scoped to the sender's email; `get_order` refuses ids belonging to other accounts
  without revealing existence.
* Redaction happens before any model call; placeholders leaking into a reply block the send.
* Customer email text is untrusted input. The prompts instruct the model to treat it as data;
  the gates do not rely on the model having done so.
* The admin token is a shared secret for the demo. Replace with real auth before exposure.

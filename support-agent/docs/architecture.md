# Architecture

The pipeline is a fixed sequence of stages, not a free-running agent. That is deliberate:
a support reply has a known shape (understand, look things up, write, check, decide), and a
fixed pipeline lets you put a typed contract, a cost line, and a test around each step. The
only stage with open-ended model-driven behavior is *research*, and it is bounded
(`SA_RESEARCH_MAX_TOOL_ROUNDS`, read-only tools).

## Stages

### 1. Ingest (`ingest/`)
Adapters turn `.eml` bytes, JSON, or an IMAP message into an `InboundEmail`. The ticket id is
a hash of the `Message-ID` (falling back to sender+subject+body), so redelivery after a crash
creates nothing new. `Store.claim()` is a single `UPDATE ... WHERE status='received'`, which
is what makes multiple workers safe against one SQLite/Postgres database.

### 2. Redact (`redaction.py`)
Regex redaction with a Luhn check for card numbers (so tracking numbers survive). Placeholders
are stable per ticket (`[[CARD_1]]`), the mapping placeholder→kind is stored, and the
originals are returned to the caller and dropped. The model, the audit log, and the UI only
ever see the redacted text. The raw email is kept on the ticket for the human reviewer.

### 3. Triage (`TriageResult`)
One structured-output call at `effort: low`. Category, urgency, sentiment, language, order
ids, a calibrated confidence, and a free-text `requires_human_reason` for things a policy file
cannot anticipate. Spam short-circuits here: no research, no draft.

### 4. Research (`ResearchBrief`)
A tool loop with five read-only tools: `get_customer`, `list_customer_orders`, `get_order`,
`search_knowledge_base` (BM25 over `data/kb/*.md`, chunked by `##` section), and
`get_policy_document`. Tool descriptions say *when* to call them, which current models need.
When the model stops calling tools, one more call with `tool_choice: none` and a Pydantic
`output_format` produces the brief: customer context, order context, verbatim policy
citations, findings with sources, open questions, and a recommended resolution.

Why not embeddings: for a policy corpus of a few dozen sections BM25 is deterministic,
dependency-free, and unit-testable. The `KnowledgeBase.search` contract is the only thing to
replace when the corpus outgrows it.

### 5. Draft (`DraftReply`)
Writes only from the brief. The important field is `proposed_actions`: every side effect the
reply promises (refund amount, replacement, cancellation) is declared as data so the gate can
check it against caps. A reply that promises a $249 replacement without declaring it is a
grounding failure the judge should catch; a declared one is a policy question the gate answers.

### 6. Judge (`GroundingVerdict`)
A second model call asked to score every claim in the draft against the brief and quoted
policy. It is not the last line of defense; it is a signal that the deterministic gate
consumes. If the judge call fails, the ticket does not go out; it goes to a human.

### 7. Policy gate (`pipeline/gates.py`)
Pure function: `(policy, email, triage, brief, draft, judge) -> GateResult`. Rules are in
`data/policy.yaml`; the version string is stamped on every decision. See `policy-gates.md`.

### 8. Release
`auto_send` and human approval share one code path (`_release`): execute approved actions
(the only place the codebase would mutate customer state), enqueue the outbox row, flush.
The outbox is idempotent per ticket, retries with a bounded attempt count, and marks
exhausted rows `failed` for an operator to look at.

## Provider abstraction
`LLMProvider` is a `Protocol` with four methods. `AnthropicProvider` is the real one.
`FakeProvider` runs the same tools and retrieval with heuristics in place of the model, so
tests and CI exercise the whole graph, including gates, without a key. Do not extend the fake
to "look smarter"; extend the eval set and run it live.

## Claude API usage notes
* `client.beta.messages.parse(output_format=PydanticModel)` for triage, draft, judge, and the
  final research call. The SDK converts the model to a strict JSON schema, the API constrains
  generation to it, the SDK validates; one retry on `pydantic.ValidationError`.
* `thinking={"type": "adaptive"}` + `output_config.effort` per stage. `budget_tokens`,
  `temperature`, and prefill are not used (rejected on current models).
* `fallbacks="default"` with beta `server-side-fallback-2026-07-01`: a safety refusal is
  re-run on Anthropic's recommended model inside the same request. `stop_reason ==
  "refusal"` is still checked and raised as `RefusalError` if the whole chain declines.
* Static system prompts carry `cache_control: ephemeral`; everything volatile is in the user
  turn so the prefix stays cacheable across tickets.
* Tool results for parallel tool calls are returned in a single user message.
* Cost is computed per call from `usage` with a per-model price table (`config.py`) and
  written to the `usage` table; `/metrics` exposes the totals.

## Data model
`Ticket` is the aggregate: email, redacted view, each stage's output, gate result, final
reply, per-stage usage, error. It is stored as JSON in one row plus indexed columns
(status, category, decision) for queries, plus `events` (audit), `usage`, `outbox`, and
`approvals` tables. Moving to Postgres is a driver change; the repository interface is
`store/db.py`.

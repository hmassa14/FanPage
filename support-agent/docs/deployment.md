# Deployment and the production pilot

## Reference deployment (customer cloud)
```
  customer mailbox ──IMAP──▶ ┌──────────┐      ┌──────────────┐
  (or mail gateway → S3/    │ worker ×N │──────▶│ Postgres      │◀──── approval UI / API
   queue)                    └────┬─────┘      │ tickets, audit│      (behind customer SSO)
                                  │            │ outbox, usage │
                                  ▼            └──────────────┘
                     ┌────────────────────┐
                     │ Claude API          │   via Anthropic API, or Bedrock / Vertex /
                     │ (Opus 5, adaptive)  │   Foundry if the customer's cloud requires it
                     └────────────────────┘
                                  │
                        SMTP relay / Zendesk API ──▶ customer
```
* **Containers:** the existing `Dockerfile` builds one image; run it as `serve` (API/UI) and
  `worker` (N replicas). Compose is included; the same image runs on ECS/Fargate, Cloud Run,
  or Kubernetes.
* **Database:** swap SQLite for Postgres (`store/db.py` is the only file). Keep the schema;
  add row-level `FOR UPDATE SKIP LOCKED` on claim for many workers.
* **Secrets:** API key and SMTP/IMAP credentials from the customer's secret manager; never
  in `.env` past local dev.
* **Identity:** replace the shared admin token with the customer's SSO (OIDC) in
  `api/app.py::require_auth`. Reviewer identity is already recorded on every approval.
* **Model access:** Anthropic API by default. If procurement requires it, the same code runs on
  Bedrock (`AnthropicBedrockMantle`), Vertex (`AnthropicVertex`), or Foundry
  (`AnthropicFoundry`) with a client swap in `llm/anthropic_provider.py`; server-side
  fallbacks become the SDK's client-side middleware there.
* **Data handling:** PII is redacted before any model call; raw email stays in the customer's
  database. Zero-data-retention or 30-day retention is agreed in the contract, not assumed.
* **Observability:** OpenTelemetry traces (one per ticket, spans per stage and tool call, with
  model/tokens/cost) to any OTLP backend: `SA_OTEL_EXPORTER=otlp`, `SA_OTEL_ENDPOINT=...`.
  Jaeger ships in compose; point it at their Tempo/Honeycomb/Datadog instead. JSON logs carry
  the OTel trace id. `/metrics` is Prometheus text. `/scorecard` is the ops page.
* **Retrieval:** the BM25 knowledge base is fine for a policy corpus. For a full help center
  put their existing search (or a vector index) behind `KnowledgeBase.search`; the contract
  does not change.
* **Kill switch:** `SA_SENDER=file` turns the system into "draft everything, send nothing"
  with no code change. Set `auto_send.allowed_categories: []` in `policy.yaml` to route
  100% to human approval while keeping the pipeline warm.

## The 90-day pilot
**Scope.** Five routine categories (order status, returns, damaged in transit, account
access, sizing). Everything else escalates to the existing queue as it does today.

**Phase 1 (weeks 1–3): shadow.** Sender = file. The agent drafts every reply; agents keep
working as normal. We label 300 real tickets with their support leads and baseline the eval.
Exit criterion: unsafe sends = 0 on the held-out set, pass^4 ≥ 0.85 on the five categories.

**Phase 2 (weeks 4–8): approval-only.** Every draft goes to the approval queue; agents
approve, edit, or reject. Edits become eval cases. Exit criterion: edit rate < 15%, reviewer
median handling time < 60s.

**Phase 3 (weeks 9–13): auto-send on two categories.** Order status and account access go
out without review; a 10% sample is audited daily. Refund and replacement caps stay at $50 /
$75 with a human above. Expand category by category on the same criteria.

**Measured throughout, against a control group of agents:**
| Metric | Source |
|---|---|
| first-response time, p50 / p90 | ticket timestamps |
| cost per handled email (model + review time) | `usage` table + reviewer time |
| unsafe sends, escalation precision | eval + daily audit sample |
| reviewer edit rate and edit distance | `approvals` table |
| CSAT on agent-handled vs human-handled | their existing survey |
| refund dollars outside policy | finance, monthly |

**Rollback:** flip the sender to `file` or empty the auto-send allowlist. Both are config.

## What goes back to Anthropic's product team
The pilot produces evidence that is hard to get any other way:
* **Where the judge disagrees with humans.** Every approval-queue edit is a labeled example
  of a grounded-looking reply a person still changed. That is training signal for how
  Claude should write in a support register, and for what "grounded" misses.
* **Effort vs. reliability curves per stage.** pass^k at low/medium/high effort on triage,
  research, and drafting, with cost. Customers keep asking "which effort level"; this
  answers it with their data.
* **Structured-output failure modes.** Every schema-validation retry is logged with the
  offending output; volume and shape of those feed the structured-outputs team.
* **Refusal fallbacks in a support context.** How often `fallbacks="default"` fires on
  legitimate customer email (angry customers, injury reports), and whether the fallback
  reply was appropriate.
* **Tool-triggering rates.** Whether the model calls `search_knowledge_base` before
  quoting policy, by effort level; a concrete measurement behind the "search-first" prompt
  guidance.
* **The approval-queue UX.** What reviewers edit, how long it takes, what they wish the
  agent had shown them. This is product input for any first-party human-in-the-loop surface.

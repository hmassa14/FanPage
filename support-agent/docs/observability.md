# Observability

Four surfaces, each for a different person.

| Surface | Who looks at it | What it answers |
|---|---|---|
| **Traces** (OpenTelemetry → Jaeger) | on-call engineer | "what did this ticket do, in what order, how long did each step take, what did the model cost" |
| **Logs** (JSON, stderr) | on-call engineer, SIEM | "show me every line for ticket X"; every line carries `ticket_id`, our `trace_id`, and the OTel trace id so logs join to traces |
| **`/metrics`** (Prometheus text) | SRE dashboards, alerting | ticket counts by status, LLM calls, tokens, spend |
| **`/scorecard`** (web page + `/api/scorecard`) | head of support ops, VP CX | why replies are stopping (gate-reason histogram), cost per ticket and per stage, reviewer edit rate, and the latest eval scorecard next to it |

## Traces
One trace per ticket. Spans:

```
ticket.process            ticket.id, gate.decision, ticket.status, ticket.cost_usd
├─ stage.triage           llm.model, llm.input_tokens, llm.output_tokens, llm.cache_read_tokens, llm.cost_usd
├─ stage.research
│  ├─ tool.get_customer   tool.input, tool.ok, tool.output_chars
│  ├─ tool.get_order
│  └─ tool.search_knowledge_base
├─ stage.draft
├─ stage.judge
├─ gate.evaluate          gate.decision, gate.policy_version, gate.reasons[]
└─ outbox.send            outbox.attempt
```

Exporter is a setting: `SA_OTEL_EXPORTER=none|console|otlp`, `SA_OTEL_ENDPOINT=http://localhost:4318`.
`docker compose up` starts Jaeger and points both containers at it; the UI is at
http://localhost:16686. Locally: `make trace`, then `SA_OTEL_EXPORTER=otlp make demo`.

Any OTLP backend works (Grafana Tempo, Honeycomb, Datadog); Jaeger is in the compose file
because it needs no configuration.

## What to show whom in the demo
* **Engineers:** the Jaeger waterfall for the prompt-injection ticket. The research loop's
  tool calls as child spans with their latency, the gate span with `gate.reasons`, no
  `outbox.send` span because nothing went out.
* **Support ops / CX:** `/scorecard`. The gate-reason histogram is the operational story:
  it tells them where their agents' review time is going and which policy line to tune.
* **CTO:** both, briefly, then `evals/scorecard.md`: per-component numbers with the
  end-to-end pass^k on the same table.

# Open-source references

Surveyed 2026-09-10. These are the systems this project borrows from; none of them is the
whole thing, which is why this repo exists. Star counts are approximate.

## Agents
| Project | Stack | Takes from it | Lacks |
|---|---|---|---|
| [langchain-ai/agents-from-scratch](https://github.com/langchain-ai/agents-from-scratch) (MIT, ~2.2k) | Python, LangGraph, Gmail | closest to this pipeline: triage → respond agent → `interrupt()` before `send_email` → Agent Inbox review; eval notebooks; memory from human edits | no KB/policy retrieval, no PII handling, mock tools in notebooks |
| [langchain-ai/executive-ai-assistant](https://github.com/langchain-ai/executive-ai-assistant) (MIT, archived Jul 2026) | Python, LangGraph | config-driven triage, reflection graphs updating memory from feedback | unmaintained; no retrieval, no redaction |
| [langchain-ai/agent-inbox](https://github.com/langchain-ai/agent-inbox) (MIT) | Next.js | approval UI semantics: accept / edit args / respond / ignore per interrupt | needs a LangGraph backend; no audit UI |
| LangGraph customer-support tutorial ([pinned notebook](https://github.com/langchain-ai/langgraph/blob/23961cff61a42b52525f3b20b4094d8d2fba1744/docs/docs/tutorials/customer-support/customer-support.ipynb)) | Python | the **safe vs sensitive tool split**: read-only tools run freely, mutations sit behind `interrupt_before` | chat not email |
| [openai/openai-cs-agents-demo](https://github.com/openai/openai-cs-agents-demo) (MIT) | Python Agents SDK, Next.js | triage → specialist handoff; input guardrails as typed classifiers with `tripwire_triggered` | no human approval, no output grounding |
| [anthropics/anthropic-quickstarts › customer-support-agent](https://github.com/anthropics/anthropic-quickstarts/tree/main/customer-support-agent) (MIT) | Next.js, Claude, Bedrock KB | RAG with cited sources, mood detection | chat only, no triage schema, no approval gate |
| [microsoft/agents-humanoversight](https://github.com/microsoft/agents-humanoversight) (MIT) | Python, Azure Logic Apps | `@approval_gate` decorator, timeout-to-safe-default, decisions logged to a table with a dashboard | email-only approvals, no escalation on timeout |
| [huabeitech/agent-desk](https://github.com/huabeitech/agent-desk) (Apache-2.0) | Go, Next.js | "answerability gate": hand off when retrieval cannot support an answer | web chat only |
| [sierra-research/tau-bench](https://github.com/sierra-research/tau-bench) (MIT) | Python | grading policy compliance by resulting DB state, `pass^k` | a benchmark, not an agent |

## Guardrail libraries (policy layer only)
* [NVIDIA/NeMo-Guardrails](https://github.com/NVIDIA/NeMo-Guardrails) (Apache-2.0): input / dialog / retrieval / execution / output rails in Colang; PII masking, fact-checking rails.
* [guardrails-ai/guardrails](https://github.com/guardrails-ai/guardrails) (Apache-2.0): schema validation plus hub validators (PII, toxicity) with reask / fix / exception on failure.
* [openai/openai-guardrails-python](https://github.com/openai/openai-guardrails-python) (MIT): pre-flight / input / output stages.

## Patterns that recur across them, and where they live here
| Pattern | Here |
|---|---|
| enum-constrained structured triage | `TriageResult` via structured outputs |
| safe/sensitive tool partition | research tools are read-only; mutations are `ProposedAction`s executed only in `_release` |
| retrieval with citations + answerability | `ResearchBrief.relevant_policies`, `open_questions`; gate `auto_send.citation` |
| approval queue with edit | `api/app.py`, `Pipeline.approve(body=...)` records `edited` |
| guardrails as typed classifiers with tripwires | `GroundingVerdict` + regex tripwires in `policy.yaml` |
| audit log of gate decisions | `events` table with `policy_version` |
| eval harness with labeled emails | `evals/` |

## What none of them had, and this adds
Refund-amount caps, always-human action classes, legal/privacy keyword tripwires, and
confidence thresholds as first-class, versioned configuration; idempotent ingestion with
worker claiming; a transactional outbox; PII redaction in the agent itself rather than a
separate guardrail library.

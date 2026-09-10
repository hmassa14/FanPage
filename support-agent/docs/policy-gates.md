# Policy gates

Gates run after the model has done everything it is going to do. They are deterministic,
versioned, and fail closed. `data/policy.yaml` is the source of truth; `pipeline/gates.py`
is the interpreter.

## Precedence
1. **reject** — `reject.categories` (spam). Nothing is sent, nothing is drafted.
2. **escalate** — any of: `escalate.categories`, a keyword regex hit on the redacted
   subject+body, `escalate.sentiments`, triage `requires_human_reason`, urgency `critical`,
   or the draft proposing `escalate_to_human`. The reply is never sent by the system; a
   person owns the ticket.
3. **needs_approval** — any `block` reason (below). The draft waits in the approval queue.
4. **auto_send** — only when nothing above fired.

## Block rules (each produces a `GateReason` you can see in the UI)
| Rule | Fires when |
|---|---|
| `auto_send.category` | triage category not in `auto_send.allowed_categories` |
| `auto_send.triage_confidence` | below `min_triage_confidence` |
| `auto_send.draft_confidence` | below `min_draft_confidence` |
| `auto_send.urgency` | above `max_urgency` |
| `auto_send.citation` | `require_kb_citation` and the draft cites no KB doc |
| `auto_send.open_questions` | brief has open questions but the draft still promises actions |
| `auto_send.length` | reply longer than `max_reply_words` |
| `judge.missing` | judge did not run (fail closed) |
| `judge.grounding` | score below `min_grounding_score` or `grounded=false` |
| `judge.tone` / `judge.policy_conflict` | judge flagged tone or a policy contradiction |
| `content.placeholder_leak` | reply contains `[[..._N]]` |
| `content.banned_phrase` | reply contains a phrase from `content.banned_phrases` |
| `content.language` | non-English reply and `require_language_match` |
| `actions.always_require_approval` | action type listed there (cancellations, address changes) |
| `actions.not_allowlisted` | action type not in `actions.auto_approve` |
| `actions.amount_missing` / `actions.amount_cap` | capped action with no amount / over cap |

## Adding a rule
1. Add the config under the right section of `policy.yaml` and bump `version`.
2. Add the check in `evaluate()` producing a `GateReason` with a stable `rule` name.
3. Add a test in `tests/test_gates.py` for both the firing and the non-firing case.
4. If the rule changes what auto-sends, add or relabel a case in `evals/dataset.jsonl`.

## Why regexes for tripwires
The LLM triage already classifies legal/privacy intent. The regex list exists so that the
system's behavior on "I will sue you" does not depend on a model call succeeding or on a
prompt tweak. Keep word boundaries (`\bsu(e|ed|ing)\b`, not `sue`, or "suede boots" escalates).

## What this does not do yet
* No per-customer rate limiting (one customer emailing 50 times an hour).
* No cross-check that a refund amount ≤ order total; the brief carries the order total as
  text. Promote it to a typed field on `ResearchBrief` and check it here.
* No "decline" review: a reply that says "no" (final sale) auto-sends if grounded. Many teams
  want a human on every refusal; add a `declines_request: bool` to `DraftReply` and block on it.

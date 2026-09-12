# Triage Agent Quickstart

A support-ticket triage agent built on the Claude API, in the shape of a
production agent rather than a demo: strict schemas at every JSON boundary, a
bounded tool-use loop that classifies its own failures, and an eval harness
that is checked before it is trusted. It was developed with Claude Code, file
by file, with a test after each file. This page walks the same path, so you
can build it from an empty directory and understand every line.

**Features**

- Two-tool function-calling loop (`lookup_customer`, `search_incidents`) on the official `anthropic` Python SDK, written by hand and, for comparison, with the SDK's `tool_runner`
- `strict: true` tool inputs and an `output_config.format` decision, both generated from Pydantic models with no hand-written JSON Schema
- Governance controls a production loop needs: bounded turns, refusal and truncation handling, tool errors returned as values, served-model tracking, full transcripts, a `needs_human` escalation flag
- Eval harness over 12 hand-labelled tickets reporting per-class precision/recall/F1, tool pass-rate, pass rate, and pass^k across repetitions
- Offline fakes (scripted, oracle, null) so 19 tests and the harness self-checks run with no API key

**What you'll learn**

1. Why the contract (schemas) comes before the loop, and how one Pydantic setting satisfies both the strict-tools and structured-output schema rules
2. The four-move tool-use loop and the three ways it breaks
3. Which controls turn a loop into something you can run unattended, and where each one lives in the code
4. How to design a small eval set that can actually detect a regression, and how to prove the harness works before spending money on it
5. When to let the SDK drive the loop and when to own it

## Prerequisites

- Python 3.10 or newer
- A Claude API key from [console.anthropic.com](https://console.anthropic.com) (only for the live run; everything else is offline)
- `pip`, and optionally a virtual environment

## Getting Started

1. Create the project and install dependencies:

   ```bash
   mkdir triage-agent && cd triage-agent
   python3 -m venv .venv && source .venv/bin/activate
   pip install "anthropic>=1.5,<2" "pydantic>=2.7,<3" pytest
   ```

2. Add the files in the order they appear under **How it's built** below (or clone the finished directory).

3. Run the offline tests:

   ```bash
   python -m pytest -q          # 19 passed
   ```

4. Prove the harness before paying for it:

   ```bash
   python eval.py --client oracle    # every metric reads 1.000
   python eval.py --client null      # accuracy 0.250 = majority baseline, tool pass-rate 0
   ```

5. Run it live:

   ```bash
   export ANTHROPIC_API_KEY=your_key
   python eval.py --reps 3 --workers 4
   echo '{"customer_id":"cust_004","subject":"Not renewing","body":"Leadership asked me to get quotes from two alternatives."}' | python agent.py
   ```

The model defaults to `claude-opus-5`. Override with `TRIAGE_MODEL=...` or `--model`.

## Project Structure

```
triage-agent/
├── requirements.txt
├── schemas.py         Pydantic models: Ticket, tool inputs and outputs, TriageDecision
├── tools.py           registry: strict tool definitions, handlers, execute()
├── agent.py           the manual loop (the file to rewrite from memory)
├── agent_runner.py    the same agent via client.beta.messages.tool_runner
├── fake_client.py     ScriptedClient, OracleClient, NullClient (no network)
├── cases.jsonl        12 hand-labelled tickets with required_tools
├── scoring.py         precision/recall/F1, tool pass-rate, pass^k, pass@k
├── eval.py            runner: results.jsonl, errors.jsonl, report.json
└── tests/
    ├── conftest.py
    ├── test_tools.py    registry is strict-compatible; errors are values
    ├── test_loop.py     the three ways the loop breaks, plus failure classes
    └── test_scoring.py  oracle = 1.0, null = baseline, planted failures, pass^k
```

## How It Works

One picture. The agent is a loop around a single API call. Pydantic sits on
every arrow. The harness runs the loop many times and counts.

```
        ┌──────────── eval.py: run N times, count ─────────────┐
        │                                                      │
        │   ticket ──► agent.py loop ──► TriageDecision        │
        │                 │    ▲                               │
        │        tool_use │    │ tool_result                   │
        │                 ▼    │                               │
        │              tools.py: strict inputs, handlers       │
        │                                                      │
        │   schemas.py: Pydantic models on every arrow above   │
        └──────────────────────────────────────────────────────┘
```

Per ticket: the loop sends the ticket with the tool definitions and the
decision schema; Claude calls `lookup_customer` (and `search_incidents` if
something is broken); the loop executes the calls, returns the results, and
Claude answers with a JSON `TriageDecision` that the loop validates.

## How It's Built

Each part below adds one file, says why it is shaped the way it is, and ends
with a check you run before moving on. The order matters: every file has
something to test against because the previous one exists.

### Part 0: The workflow

The workflow is the first best practice, because it is what made the rest
cheap.

- **Ask the installed SDK, not your memory.** The API drifted in 2025 and
  2026 (`output_format` became `output_config.format`, `budget_tokens` was
  removed, forced `tool_choice` is rejected on Claude Fable 5.1). Before
  writing a call, introspect it:

  ```bash
  python - <<'PY'
  import inspect, anthropic
  c = anthropic.Anthropic(api_key="x")
  print([p for p in inspect.signature(c.messages.create).parameters if p in ("output_config","output_format","tools","tool_choice","thinking")])
  print([p for p in inspect.signature(c.beta.messages.tool_runner).parameters][:12])
  PY
  ```

  That is how it was established that `create()` takes `output_config` (not
  `output_format`) and that `tool_runner()` accepts `system` and
  `output_config`.

- **Build in dependency order:** contract, tools, loop, fakes, tests, cases,
  scoring, runner. Each step is testable against the one before it.
- **Fake before live.** The loop touches one method on the client. Fake that
  method and the whole system runs offline, which means the tests run in CI
  and the harness can be proven before it costs anything.
- **Pin behaviour in tests, not in comments.** Every "never do X" in this
  guide is an assertion somewhere in `tests/`.
- **Commit after every green test run.** Small diffs make the transcript of
  how the agent was built readable later.

Record the pins first:

```
anthropic>=1.5,<2
pydantic>=2.7,<3
pytest>=8
```

### Part 1: `schemas.py`, the contract before the loop

**What we add.** Every object that crosses a JSON boundary, as a Pydantic
model: the ticket in, the two tool inputs, the two tool outputs, and the
decision out.

**Why it's shaped this way.** Start from what the agent *returns*. A
`TriageDecision` with a `Literal` category (Pydantic emits an `enum`), a
`Literal` priority, a `needs_human` bool, a one-sentence summary, and an
`evidence` list. The evidence list exists so a reviewer can see whether the
decision used the tool results or just re-read the ticket. Then work backwards:
which tools produce that evidence, what do they take, what do they return.

The single most important line is on the shared base class:

```python
class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")
```

`extra="forbid"` is what makes `model_json_schema()` emit
`additionalProperties: false`. Fields without defaults are all listed under
`required`. Those two properties are exactly what both `strict: true` tools
and `output_config.format` demand, so one base class produces API-ready
schemas for every boundary with no hand-edited JSON anywhere in the repo.

> **Tip:** Put a `description` on every field that Claude fills in. For tool
> inputs it becomes the per-parameter description Claude reads when deciding
> what to pass; for the decision it steers what goes in `summary` and
> `evidence`. It is the cheapest prompt engineering available.

```python
"""Pydantic models that define every JSON boundary in the agent.

Three boundaries, one library:

1. Tool inputs   - what Claude is allowed to pass to our functions.
                   Sent to the API as `input_schema` with `strict: true`.
2. Tool outputs  - what our functions return (serialised into `tool_result`).
3. The decision  - the final structured answer, sent as `output_config.format`
                   and re-validated on our side with `model_validate_json`.

`extra="forbid"` is what makes `model_json_schema()` emit
`additionalProperties: false`, and fields without defaults are all listed
under `required` - which is exactly the subset of JSON Schema that both
strict tools and structured outputs demand.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Category = Literal["billing", "bug", "feature_request", "account_access", "churn_risk"]
Priority = Literal["P0", "P1", "P2", "P3"]

CATEGORIES: tuple[str, ...] = ("billing", "bug", "feature_request", "account_access", "churn_risk")
PRIORITIES: tuple[str, ...] = ("P0", "P1", "P2", "P3")


class StrictModel(BaseModel):
    """Base for anything whose schema crosses the API boundary."""

    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------- #
# Input to the agent
# --------------------------------------------------------------------------- #


class Ticket(StrictModel):
    customer_id: str
    subject: str
    body: str

    def render(self) -> str:
        return f"customer_id: {self.customer_id}\nsubject: {self.subject}\n\n{self.body}"


# --------------------------------------------------------------------------- #
# Tool input schemas (what Claude sends us)
# --------------------------------------------------------------------------- #


class LookupCustomerInput(StrictModel):
    customer_id: str = Field(description="The customer_id exactly as it appears on the ticket.")


class SearchIncidentsInput(StrictModel):
    query: str = Field(
        description="Two to five keywords describing the product area or symptom, e.g. 'export csv timeout'."
    )


# --------------------------------------------------------------------------- #
# Tool output schemas (what we send back)
# --------------------------------------------------------------------------- #


class CustomerRecord(StrictModel):
    customer_id: str
    company: str
    plan: Literal["free", "team", "enterprise"]
    mrr_usd: int
    seats: int
    tenure_months: int
    open_tickets: int
    renewal_in_days: int


class Incident(StrictModel):
    incident_id: str
    title: str
    status: Literal["investigating", "identified", "monitoring"]
    affected_area: str
    opened_hours_ago: int


class IncidentSearchResult(StrictModel):
    query: str
    matches: list[Incident]


# --------------------------------------------------------------------------- #
# The decision (structured output)
# --------------------------------------------------------------------------- #


class TriageDecision(StrictModel):
    category: Category = Field(description="Exactly one of the five triage categories.")
    priority: Priority = Field(description="P0 is a live outage for a paying customer; P3 is a wishlist item.")
    needs_human: bool = Field(description="True when a person must reply before any automated response goes out.")
    summary: str = Field(description="One sentence a support lead can read in three seconds.")
    evidence: list[str] = Field(
        description="Facts that drove the decision. Cite tool results (plan, MRR, incident ids) rather than restating the ticket."
    )


DECISION_SCHEMA: dict = TriageDecision.model_json_schema()
```

**Test it.** Print the schema the API will receive and confirm the two
properties:

```bash
python -c "import json, schemas; print(json.dumps(schemas.DECISION_SCHEMA, indent=1))"
python -c "import schemas; s=schemas.LookupCustomerInput.model_json_schema(); print(s['additionalProperties'], s['required'])"
```

### Part 2: `tools.py`, tools as typed, fail-soft functions

**What we add.** A `ToolSpec` pairing an input model with a handler, a
`REGISTRY` keyed by name, `TOOL_DEFINITIONS` derived from it (each carrying
`strict: True`), and one `execute(name, raw_input)` entry point. Backing data
is two in-memory tables.

**Why two tools.** One that every ticket needs (`lookup_customer`) and one
only some tickets need (`search_incidents`). That asymmetry is what makes
"did it call the right tools" a real metric in Part 7 instead of a constant.

**Three rules for `execute`, and each is a production rule:**

- **It never raises.** Unknown tool, invalid input, handler failure: each
  becomes a `ToolOutcome(is_error=True)`. The loop turns that into a
  `tool_result` with `is_error: true`, which is how Claude gets to recover
  (retry with the right id, or decide without the data). A raised exception
  ends the run with no decision; an error result lets the model finish.
- **It re-validates the input** with `model_validate` even though
  `strict: true` means the API already guaranteed the shape. The registry is
  also called from tests and from the tool-runner variant, where nothing
  guaranteed anything.
- **Results go back as JSON text** (`json.dumps(result.model_dump())`).
  `tool_result.content` is a string, and typed outputs mean the fake tables
  cannot drift from what the handler promises.

> **Note:** The loop never touches the backing data. Swapping the in-memory
> tables for a real customer database means changing two handler bodies and
> nothing else.

```python
"""Tool registry: definitions the API sees, and handlers the loop executes.

Each entry pairs
  - a Pydantic input model  -> becomes `input_schema`, sent with `strict: true`
  - a handler                -> takes the validated model, returns a Pydantic
                               result (or raises ToolError)

`strict: true` means the API guarantees `tool_use.input` validates against
the schema *before* it reaches us. We still run `model_validate` in
`execute()`: the registry is also called from tests and the tool runner, and
a validation error here is a bug report, not a crash.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

from pydantic import BaseModel, ValidationError

from schemas import (
    CustomerRecord,
    Incident,
    IncidentSearchResult,
    LookupCustomerInput,
    SearchIncidentsInput,
)


class ToolError(Exception):
    """Raised by a handler for a *recoverable* failure Claude should hear about."""


# --------------------------------------------------------------------------- #
# Fake backing data. Swap for real clients; the loop never touches these.
# --------------------------------------------------------------------------- #

CUSTOMERS: dict[str, CustomerRecord] = {
    r.customer_id: r
    for r in [
        CustomerRecord(customer_id="cust_001", company="Northwind Logistics", plan="enterprise", mrr_usd=18000, seats=240, tenure_months=31, open_tickets=1, renewal_in_days=22),
        CustomerRecord(customer_id="cust_002", company="Bluefin Analytics", plan="team", mrr_usd=900, seats=12, tenure_months=8, open_tickets=0, renewal_in_days=140),
        CustomerRecord(customer_id="cust_003", company="Pilar Studio", plan="free", mrr_usd=0, seats=2, tenure_months=2, open_tickets=0, renewal_in_days=0),
        CustomerRecord(customer_id="cust_004", company="Harbor Health", plan="enterprise", mrr_usd=42000, seats=900, tenure_months=47, open_tickets=3, renewal_in_days=9),
        CustomerRecord(customer_id="cust_005", company="Quill & Co", plan="team", mrr_usd=450, seats=6, tenure_months=14, open_tickets=0, renewal_in_days=300),
        CustomerRecord(customer_id="cust_006", company="Tessellate Games", plan="team", mrr_usd=1200, seats=18, tenure_months=5, open_tickets=2, renewal_in_days=45),
        CustomerRecord(customer_id="cust_007", company="Meridian Legal", plan="enterprise", mrr_usd=26000, seats=410, tenure_months=19, open_tickets=0, renewal_in_days=200),
        CustomerRecord(customer_id="cust_008", company="Solo dev (K. Ito)", plan="free", mrr_usd=0, seats=1, tenure_months=11, open_tickets=0, renewal_in_days=0),
    ]
}

INCIDENTS: list[Incident] = [
    Incident(incident_id="INC-2291", title="CSV export jobs timing out for large workspaces", status="identified", affected_area="export csv download report", opened_hours_ago=3),
    Incident(incident_id="INC-2294", title="SSO login failures for Okta-backed tenants", status="investigating", affected_area="sso okta login saml authentication", opened_hours_ago=1),
    Incident(incident_id="INC-2277", title="Webhook delivery delays (up to 20 min)", status="monitoring", affected_area="webhook delivery integration api events", opened_hours_ago=30),
]


# --------------------------------------------------------------------------- #
# Handlers
# --------------------------------------------------------------------------- #


def lookup_customer(args: LookupCustomerInput) -> CustomerRecord:
    record = CUSTOMERS.get(args.customer_id)
    if record is None:
        raise ToolError(f"No customer with id {args.customer_id!r}. Treat the ticket as an unknown free-tier user.")
    return record


def search_incidents(args: SearchIncidentsInput) -> IncidentSearchResult:
    terms = {t.lower().strip(",.") for t in args.query.split() if len(t) > 2}
    matches = [
        inc
        for inc in INCIDENTS
        if terms & set(inc.affected_area.split()) or terms & set(inc.title.lower().split())
    ]
    return IncidentSearchResult(query=args.query, matches=matches)


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_model: type[BaseModel]
    handler: Callable[[Any], BaseModel]

    def definition(self) -> dict:
        """The dict that goes in the request's `tools` list."""
        return {
            "name": self.name,
            "description": self.description,
            "strict": True,
            "input_schema": self.input_model.model_json_schema(),
        }


REGISTRY: dict[str, ToolSpec] = {
    spec.name: spec
    for spec in [
        ToolSpec(
            name="lookup_customer",
            description=(
                "Fetch the account record for the customer who filed the ticket: plan, MRR, seats, "
                "tenure, open tickets, and days until renewal. Call this first for every ticket."
            ),
            input_model=LookupCustomerInput,
            handler=lookup_customer,
        ),
        ToolSpec(
            name="search_incidents",
            description=(
                "Search currently open platform incidents by keyword. Call this whenever a ticket "
                "describes something broken, slow, or failing, so a known outage is not triaged as a new bug."
            ),
            input_model=SearchIncidentsInput,
            handler=search_incidents,
        ),
    ]
}

TOOL_DEFINITIONS: list[dict] = [spec.definition() for spec in REGISTRY.values()]


@dataclass(frozen=True)
class ToolOutcome:
    content: str
    is_error: bool


def execute(name: str, raw_input: dict) -> ToolOutcome:
    """Run one tool call. Never raises: every failure becomes an `is_error` result.

    The loop turns this straight into a `tool_result` block. Returning errors
    as content (rather than raising) is what lets Claude recover - retry with
    a fixed input, or proceed without the data.
    """
    spec = REGISTRY.get(name)
    if spec is None:
        return ToolOutcome(f"Unknown tool {name!r}. Available: {sorted(REGISTRY)}", is_error=True)
    try:
        args = spec.input_model.model_validate(raw_input)
    except ValidationError as exc:
        return ToolOutcome(f"Invalid input for {name}: {exc.errors(include_url=False)}", is_error=True)
    try:
        result = spec.handler(args)
    except ToolError as exc:
        return ToolOutcome(str(exc), is_error=True)
    return ToolOutcome(json.dumps(result.model_dump()), is_error=False)
```

**Test it.** The second and fourth must print `is_error=True`, not a traceback:

```bash
python -c "from tools import execute; print(execute('lookup_customer', {'customer_id':'cust_001'}))"
python -c "from tools import execute; print(execute('lookup_customer', {'customer_id':'nope'}))"
python -c "from tools import execute; print(execute('search_incidents', {'query':'csv export timeout'}))"
python -c "from tools import execute; print(execute('delete_customer', {}))"
```

### Part 3: `agent.py`, the loop

**What we add.** The file you will be asked to write from memory. Here it is
in the order it was written, top to bottom.

**3a. The constant request.** Everything identical on every call goes in
`_request_params()`: model, `max_tokens`, `system`, `tools`, `output_config`.
Two reasons. The loop body stays short. And prompt caching is a prefix match
over `tools → system → messages`, so keeping those byte-identical across
calls is what lets the cache hit.

`output_config` carries two things: `effort: "low"` (classification is the
documented case where low effort is the sweet spot) and `format`, the
decision schema from Part 1. Note where `strict: true` is *not*: it sits on
each tool definition, not here. These are separate features that compose in
one request. `strict` constrains what Claude passes to *your* functions;
`format` constrains what Claude *says*.

**3b. The system prompt.** Five category definitions, four priority rules, a
three-step process that names the tools. Forced `tool_choice` returns a 400
on Claude Fable 5.1, so "always call lookup_customer first" lives in the
prompt and `tool_choice` stays at its default.

> **Note:** The one ambiguity found while writing cases (an SSO failure that
> is also an open incident: `bug` or `account_access`?) was resolved by adding
> one sentence to the prompt, not by softening the label. If two careful
> readers could disagree on a label, fix the spec, not the grader.

**3c. The loop skeleton.** `for turn in range(1, max_turns + 1)`, never
`while True`. One `messages.create` per turn, wrapped so `APIStatusError` and
`APIConnectionError` become `AgentError` with a `kind`. Then branch on
`stop_reason`, in this order:

| `stop_reason` | What the loop does |
|---|---|
| `refusal` | raise `AgentError("refusal")`. Check this before reading content. |
| `max_tokens` | raise `AgentError("truncated")`. A clipped answer is not a wrong answer. |
| `tool_use` | the three moves below, then `continue` |
| `pause_turn` | append the assistant content and `continue` (server tools only; none here, but know the shape) |
| `end_turn` | parse the text block, return |

**3d. The three moves on `tool_use`.** The sentence to memorise: *keep the
assistant turn verbatim, send one user message with one `tool_result` per
`tool_use`, match the ids.*

```python
messages.append({"role": "assistant", "content": response.content})   # verbatim: the whole list
results = []
for block in response.content:
    if block.type != "tool_use":
        continue
    outcome = execute(block.name, block.input)
    results.append({"type": "tool_result", "tool_use_id": block.id,   # id matched
                    "content": outcome.content, "is_error": outcome.is_error})
messages.append({"role": "user", "content": results})                 # ONE user message
```

Why each move breaks: rebuild the assistant turn from only the `tool_use`
blocks and you have dropped the text Claude wrote alongside them. Claude may
emit several `tool_use` blocks in one turn (parallel calls); split their
results across two user messages, or omit one, and the next request is a 400.
Mismatch an id and it is a 400.

**3e. The final parse.** On `end_turn`, join the text blocks and run
`TriageDecision.model_validate_json(text)`. The `format` constraint means
this should always succeed. Validate anyway; if it fails, that is
`AgentError("invalid_output")` with the raw text in the detail. Trust, then
verify.

**3f. Bookkeeping for the harness.** `TriageResult` carries the decision,
every tool call (name, input, error flag, content), the full `messages`
transcript, the served model from `response.model`, the turn count, token
totals from `response.usage`, and latency. None of this is needed to *run*
the agent. All of it is needed to *debug an eval*.

```python
"""The manual tool-use loop. This is the file to delete and rewrite cold.

The loop is four moves, and the API is strict about all four:

  1. Send the transcript. Claude answers with `stop_reason` and `content`.
  2. If `stop_reason == "tool_use"`: append the assistant turn VERBATIM
     (the whole `response.content` list, not just the tool_use blocks).
  3. Execute every `tool_use` block, then append ONE user message whose
     content is a `tool_result` per `tool_use`, each carrying the matching
     `tool_use_id`. Split them across messages or drop one and the next
     request is a 400.
  4. If `stop_reason == "end_turn"`: the text block is JSON shaped by
     `output_config.format`. Validate it with Pydantic anyway.

Everything else is bookkeeping so the harness can tell a model failure from
a plumbing failure.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any

import anthropic
from pydantic import ValidationError

from schemas import DECISION_SCHEMA, Ticket, TriageDecision
from tools import TOOL_DEFINITIONS, execute

MODEL = os.environ.get("TRIAGE_MODEL", "claude-opus-5")
MAX_TOKENS = 4096
MAX_TURNS = 6  # round-trips, not tool calls; parallel calls share a turn

SYSTEM_PROMPT = """You triage inbound support tickets for a B2B SaaS product.

Categories (pick exactly one):
- billing: charges, invoices, refunds, plan changes, failed payments.
- bug: something that should work does not, and it is not a known open incident being handled already. A ticket that matches an open incident is STILL a bug - mark it P0/P1 and cite the incident id.
- feature_request: asking for capability that does not exist.
- account_access: login, SSO, password, permissions, locked accounts. An SSO or login failure is account_access even when it matches an open incident; cite the incident and raise priority instead of recategorising.
- churn_risk: the customer signals they may leave (cancel, downgrade, "evaluating alternatives", "not renewing"). This wins over every other category when the signal is explicit, regardless of what the underlying complaint is.

Priority:
- P0: enterprise customer blocked or in an active incident, or churn risk from any paying customer with renewal inside 30 days.
- P1: paying customer materially impaired, or churn risk with renewal beyond 30 days.
- P2: paying customer inconvenienced, or any free-tier bug/billing/access issue.
- P3: feature requests and free-tier wishlist items.

Process:
1. Always call lookup_customer with the customer_id from the ticket before deciding. Plan and renewal window change the priority.
2. If the ticket describes something broken, slow, or failing, call search_incidents with a few keywords from the symptom.
3. Then respond with the triage decision. Put tool-derived facts (plan, MRR, renewal window, incident ids) in `evidence`."""


class AgentError(Exception):
    """A run that produced no scorable decision. `kind` is the failure class."""

    def __init__(self, kind: str, detail: str = ""):
        super().__init__(f"{kind}: {detail}" if detail else kind)
        self.kind = kind
        self.detail = detail


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict
    is_error: bool
    content: str


@dataclass
class TriageResult:
    decision: TriageDecision
    tool_calls: list[ToolCall]
    messages: list[dict]  # full transcript, for the harness
    model: str
    turns: int
    input_tokens: int = 0
    output_tokens: int = 0
    latency_s: float = 0.0
    raw_text: str = ""
    extras: dict[str, Any] = field(default_factory=dict)


def _request_params() -> dict:
    """Everything that is the same on every call. Keeping this stable is what
    lets prompt caching hit: tools -> system -> messages is the cache prefix."""
    return {
        "model": MODEL,
        "max_tokens": MAX_TOKENS,
        "system": SYSTEM_PROMPT,
        "tools": TOOL_DEFINITIONS,
        "output_config": {
            "effort": "low",  # classification: low effort is the documented sweet spot
            "format": {"type": "json_schema", "schema": DECISION_SCHEMA},
        },
    }


def triage(ticket: Ticket, client: Any | None = None, max_turns: int = MAX_TURNS) -> TriageResult:
    """Run the loop once for one ticket. Raises AgentError on any non-scorable outcome."""
    client = client or anthropic.Anthropic()
    messages: list[dict] = [{"role": "user", "content": ticket.render()}]
    tool_calls: list[ToolCall] = []
    in_tok = out_tok = 0
    served_model = MODEL
    started = time.perf_counter()

    for turn in range(1, max_turns + 1):
        try:
            response = client.messages.create(**_request_params(), messages=messages)
        except anthropic.APIStatusError as exc:  # 4xx/5xx after the SDK's own retries
            raise AgentError("api_error", f"{exc.status_code}: {exc.message}") from exc
        except anthropic.APIConnectionError as exc:
            raise AgentError("connection_error", str(exc)) from exc

        in_tok += response.usage.input_tokens
        out_tok += response.usage.output_tokens
        served_model = response.model

        if response.stop_reason == "refusal":
            detail = getattr(response, "stop_details", None)
            raise AgentError("refusal", getattr(detail, "category", "") or "")
        if response.stop_reason == "max_tokens":
            raise AgentError("truncated", f"hit max_tokens={MAX_TOKENS} on turn {turn}")

        if response.stop_reason == "tool_use":
            # (2) the assistant turn goes back exactly as received
            messages.append({"role": "assistant", "content": response.content})

            # (3) one tool_result per tool_use, ids matched, all in ONE user message
            results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                outcome = execute(block.name, block.input)
                tool_calls.append(ToolCall(block.id, block.name, dict(block.input), outcome.is_error, outcome.content))
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": outcome.content,
                        "is_error": outcome.is_error,
                    }
                )
            messages.append({"role": "user", "content": results})
            continue

        if response.stop_reason == "pause_turn":
            # only server tools produce this; we have none, but the shape is:
            # append the partial assistant turn and re-send so Claude resumes.
            messages.append({"role": "assistant", "content": response.content})
            continue

        # (4) end_turn: the text block is schema-shaped JSON. Trust, then verify.
        text = "".join(b.text for b in response.content if b.type == "text")
        try:
            decision = TriageDecision.model_validate_json(text)
        except ValidationError as exc:
            raise AgentError("invalid_output", f"{exc.error_count()} validation errors; raw={text[:200]!r}") from exc

        messages.append({"role": "assistant", "content": response.content})
        return TriageResult(
            decision=decision,
            tool_calls=tool_calls,
            messages=messages,
            model=served_model,
            turns=turn,
            input_tokens=in_tok,
            output_tokens=out_tok,
            latency_s=time.perf_counter() - started,
            raw_text=text,
        )

    raise AgentError("max_turns", f"no decision after {max_turns} turns")


if __name__ == "__main__":
    import json
    import sys

    ticket = Ticket.model_validate_json(sys.stdin.read())
    result = triage(ticket)
    print(json.dumps(result.decision.model_dump(), indent=2))
    print(f"\n# {len(result.tool_calls)} tool call(s), {result.turns} turn(s), "
          f"{result.input_tokens} in / {result.output_tokens} out, served by {result.model}", file=sys.stderr)
```

You cannot run this without a key yet. That is the cue for Part 4.

### Part 4: `fake_client.py` and `tests/test_loop.py`, prove the loop offline

**What we add.** Three fakes and six tests.

**Why it works.** The loop only ever calls `client.messages.create(**kwargs)`
and reads `.content`, `.stop_reason`, `.model`, `.usage` from the result. So
a fake is a `messages` attribute whose `create` returns `SimpleNamespace`
objects. Three of them:

- **`ScriptedClient`** returns a fixed list of responses in order. It tests
  the transcript handling.
- **`OracleClient`** knows the labelled cases and answers each perfectly,
  calling its required tools first. It takes a `plant` dict so a test can
  inject a known failure. Used in Part 8.
- **`NullClient`** always returns the same decision with no tool calls. Used
  in Part 8.

> **Note:** A bug worth remembering from building this. The fake recorded
> `kwargs` as-is, but `kwargs["messages"]` is the loop's *live* list, which
> the loop keeps appending to after the call returns. The test saw four
> messages where three were sent. Fakes have to copy what they record:
> `list(kwargs["messages"])`.

```python
"""Offline stand-ins for `anthropic.Anthropic()`.

They expose the one method the loop uses, `client.messages.create(**kwargs)`,
and return objects with the attributes the loop reads: `content`,
`stop_reason`, `model`, `usage`. Nothing here talks to the network.

  ScriptedClient - returns a fixed list of responses in order. For testing the
                   loop's transcript handling.
  OracleClient   - knows the labelled cases and answers them perfectly, calling
                   the required tools first. Run the harness against it and
                   every metric must read 1.0; that proves the plumbing before
                   spending money. `plant` lets a test inject known failures.
  NullClient     - always answers the majority class with no tool calls. The
                   harness must score this badly; if it doesn't, the grader is
                   too lenient.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Callable

from schemas import Ticket

FAKE_MODEL = "fake-model"


def text_block(text: str) -> SimpleNamespace:
    return SimpleNamespace(type="text", text=text)


def tool_use_block(id: str, name: str, input: dict) -> SimpleNamespace:
    return SimpleNamespace(type="tool_use", id=id, name=name, input=input)


def response(content: list, stop_reason: str, model: str = FAKE_MODEL) -> SimpleNamespace:
    return SimpleNamespace(
        content=content,
        stop_reason=stop_reason,
        model=model,
        usage=SimpleNamespace(input_tokens=100, output_tokens=20),
        stop_details=None,
    )


def final_json(decision: dict) -> SimpleNamespace:
    return response([text_block(json.dumps(decision))], "end_turn")


class _Messages:
    def __init__(self, fn: Callable[..., SimpleNamespace]):
        self._fn = fn
        self.calls: list[dict] = []

    def create(self, **kwargs) -> SimpleNamespace:
        # snapshot: the loop mutates its `messages` list after this call returns
        self.calls.append({**kwargs, "messages": list(kwargs["messages"])})
        return self._fn(**kwargs)


class ScriptedClient:
    def __init__(self, responses: list[SimpleNamespace]):
        self._queue = list(responses)
        self.messages = _Messages(self._next)

    def _next(self, **kwargs) -> SimpleNamespace:
        if not self._queue:
            raise AssertionError("ScriptedClient ran out of responses")
        return self._queue.pop(0)


@dataclass
class Plant:
    """A deliberate failure for one case. Any field left None keeps oracle behaviour."""

    category: str | None = None
    priority: str | None = None
    skip_tools: bool = False


@dataclass
class OracleClient:
    cases: list[dict]
    plant: dict[str, Plant] = field(default_factory=dict)

    def __post_init__(self):
        self._by_ticket = {Ticket.model_validate(c["ticket"]).render(): c for c in self.cases}
        self.messages = _Messages(self._answer)

    def _answer(self, messages: list[dict], **_: Any) -> SimpleNamespace:
        case = self._by_ticket[messages[0]["content"]]
        plant = self.plant.get(case["id"], Plant())
        first_turn = len(messages) == 1

        if first_turn and not plant.skip_tools:
            ticket = case["ticket"]
            blocks = []
            for i, name in enumerate(case["required_tools"]):
                args = (
                    {"customer_id": ticket["customer_id"]}
                    if name == "lookup_customer"
                    else {"query": ticket["subject"]}
                )
                blocks.append(tool_use_block(f"toolu_{case['id']}_{i}", name, args))
            return response(blocks, "tool_use")

        expected = case["expected"]
        return final_json(
            {
                "category": plant.category or expected["category"],
                "priority": plant.priority or expected["priority"],
                "needs_human": expected["priority"] in ("P0", "P1"),
                "summary": f"oracle answer for {case['id']}",
                "evidence": ["oracle"],
            }
        )


class NullClient:
    def __init__(self, category: str = "billing", priority: str = "P2"):
        self._decision = {
            "category": category,
            "priority": priority,
            "needs_human": False,
            "summary": "constant answer",
            "evidence": [],
        }
        self.messages = _Messages(lambda **_: final_json(self._decision))
```

The tests. The first one is the sentence from 3d turned into assertions.
`first.content` is checked by identity (`is`), not equality, because
"verbatim" means the same object went back.

```python
"""The three ways the manual loop breaks, each pinned by a test."""

import json

import pytest

from agent import AgentError, triage
from fake_client import ScriptedClient, final_json, response, text_block, tool_use_block
from schemas import Ticket

TICKET = Ticket(customer_id="cust_001", subject="CSV export never finishes", body="exports time out")
GOOD = {
    "category": "bug",
    "priority": "P0",
    "needs_human": True,
    "summary": "Enterprise export outage",
    "evidence": ["plan=enterprise", "INC-2291"],
}


def test_parallel_tool_calls_produce_one_user_message_with_matching_ids():
    first = response(
        [
            text_block("Checking the account and incidents."),
            tool_use_block("toolu_A", "lookup_customer", {"customer_id": "cust_001"}),
            tool_use_block("toolu_B", "search_incidents", {"query": "csv export timeout"}),
        ],
        "tool_use",
    )
    client = ScriptedClient([first, final_json(GOOD)])

    result = triage(TICKET, client=client)

    assert result.decision.category == "bug"
    # transcript: user, assistant (verbatim), user (tool results), assistant (final)
    assert [m["role"] for m in result.messages] == ["user", "assistant", "user", "assistant"]
    # 1. the assistant turn is the response content object itself, text block included
    assert result.messages[1]["content"] is first.content
    # 2. ONE user message carrying one tool_result per tool_use ...
    results = result.messages[2]["content"]
    assert [r["type"] for r in results] == ["tool_result", "tool_result"]
    # 3. ... with ids matched, in order
    assert [r["tool_use_id"] for r in results] == ["toolu_A", "toolu_B"]
    assert all(r["is_error"] is False for r in results)
    assert json.loads(results[0]["content"])["plan"] == "enterprise"
    # the second request carried the full transcript
    assert len(client.messages.calls[1]["messages"]) == 3


def test_tool_error_is_returned_as_is_error_result_not_raised():
    first = response([tool_use_block("toolu_X", "lookup_customer", {"customer_id": "cust_404"})], "tool_use")
    client = ScriptedClient([first, final_json(GOOD)])
    result = triage(TICKET, client=client)
    tr = result.messages[2]["content"][0]
    assert tr["is_error"] is True and tr["tool_use_id"] == "toolu_X"
    assert result.tool_calls[0].is_error


def test_request_shape_has_strict_tools_and_output_format():
    client = ScriptedClient([final_json(GOOD)])
    triage(TICKET, client=client)
    req = client.messages.calls[0]
    assert all(t["strict"] is True for t in req["tools"])
    assert req["output_config"]["format"]["type"] == "json_schema"
    assert req["output_config"]["format"]["schema"]["additionalProperties"] is False
    assert req["messages"][0] == {"role": "user", "content": TICKET.render()}


def test_invalid_json_output_is_a_classified_failure():
    bad = dict(GOOD, category="urgent")  # not in the enum
    client = ScriptedClient([final_json(bad)])
    with pytest.raises(AgentError) as exc:
        triage(TICKET, client=client)
    assert exc.value.kind == "invalid_output"


def test_refusal_and_truncation_are_classified():
    with pytest.raises(AgentError) as exc:
        triage(TICKET, client=ScriptedClient([response([], "refusal")]))
    assert exc.value.kind == "refusal"
    with pytest.raises(AgentError) as exc:
        triage(TICKET, client=ScriptedClient([response([text_block("{")], "max_tokens")]))
    assert exc.value.kind == "truncated"


def test_max_turns_guard():
    loop_forever = response([tool_use_block("t", "lookup_customer", {"customer_id": "cust_001"})], "tool_use")
    client = ScriptedClient([loop_forever] * 10)
    with pytest.raises(AgentError) as exc:
        triage(TICKET, client=client, max_turns=3)
    assert exc.value.kind == "max_turns"
    assert len(client.messages.calls) == 3
```

And `tests/conftest.py` so the tests can import the package from anywhere:

```python
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
```

**Test it.**

```bash
python -m pytest tests/test_loop.py -q     # 6 passed
```

### Part 5: Governance, the controls that make the loop safe to run unattended

Nothing new is added in this part. It names the controls already in the code,
says what each protects against, and points at the test that pins it. This
is the checklist to carry to the next agent.

| Control | Protects against | Where | Pinned by |
|---|---|---|---|
| Bounded loop (`max_turns`) | A model that keeps calling tools forever, and the bill that comes with it | `agent.py` `for turn in range(...)` | `test_max_turns_guard` |
| Strict tool inputs | Malformed arguments reaching your functions | `tools.py` `strict: True` + `extra="forbid"` | `test_every_definition_is_strict_compatible` |
| Tool errors as values | One bad lookup ending the run with no decision | `tools.py` `execute()` never raises | `test_tool_error_is_returned_as_is_error_result_not_raised` |
| Validated output | Downstream code trusting text that only looks like JSON | `agent.py` `model_validate_json` | `test_invalid_json_output_is_a_classified_failure` |
| Failure classes | Plumbing failures counted as wrong answers | `AgentError.kind`: refusal, truncated, invalid_output, api_error, connection_error, max_turns | `test_refusal_and_truncation_are_classified` |
| Refusal checked first | Reading `content` off a refused response | `agent.py` `stop_reason == "refusal"` branch | same |
| Served-model tracking | A silent fallback or reroute scoring as the model you asked for | `TriageResult.model` from `response.model` | reported per row in `results.jsonl` |
| Token accounting from `usage` | Cost estimates from string length | `TriageResult.input_tokens/output_tokens` | per row in `results.jsonl` |
| Full transcript kept | Debugging a surprising score by re-running it | `TriageResult.messages` | `transcript` field per row |
| Human escalation flag | Automation replying where a person must | `TriageDecision.needs_human` | part of the decision schema |
| Stable request prefix | Cache misses from volatile content ahead of the messages | `_request_params()` | `test_request_shape_has_strict_tools_and_output_format` |

> **Production note:** Two controls are deliberately left out here and worth
> knowing. Server-side refusal fallbacks (`fallbacks` on the beta client)
> re-run a refused request on another model inside the same call; a triage
> classifier will essentially never be refused, and the parameter needs the
> beta client, so the loop stays on the stable one. And a wall-clock ceiling
> per case belongs in the runner, not the loop, if you ever add streaming.

### Part 6: `cases.jsonl`, an eval set that can detect a regression

**What we add.** Twelve tickets with labels and `required_tools`.

**Why it's shaped this way.** Small, but built like a real set:

- **Twelve cases, 3/3/2/2/2 across five classes.** Enough that one mislabel
  moves a per-class number by a visible amount (one churn miss is recall
  0.5), few enough to read every one.
- **Every case declares `required_tools`.** All twelve require
  `lookup_customer`; the four describing something failing also require
  `search_incidents`. That is what makes tool pass-rate a metric.
- **Three cases match seeded incidents** (`g1`, `g2`, `a2`), so "did it check
  incidents and cite the id" is observable in `evidence`.
- **Decoys in both directions.** `b1` is a billing complaint that says the
  customer is happy (must *not* be churn). `c2` is a billing complaint with an
  explicit downgrade threat (must be churn, not billing). A set that tests
  only one direction rewards always-saying-churn.
- **Labels are hand-written**, and each row says so in `source`. Labels that
  came from a model would make the eval measure imitation of that model.
- **Priorities follow the rules in the prompt** so they are derivable, not
  vibes: enterprise blocked → P0, paying customer impaired → P1, free tier
  anything → P2, feature requests → P3.

> **Tip:** Write the ambiguous case first. `c2` (billing complaint, downgrade
> threat) forced the "churn wins when the signal is explicit" sentence into
> the prompt. The decoy that exposes a fuzzy spec is worth more than five
> easy cases.

```json
{"id": "b1", "tags": ["billing", "churn-decoy-negative"], "ticket": {"customer_id": "cust_002", "subject": "Charged twice for September", "body": "Hi, our card was charged $900 on Sept 1 and again on Sept 3 for the same Team plan invoice (INV-88412). Can you refund the duplicate? Otherwise everything is working fine, we're happy with the product."}, "expected": {"category": "billing", "priority": "P2"}, "required_tools": ["lookup_customer"], "source": "hand-written"}
{"id": "b2", "tags": ["billing"], "ticket": {"customer_id": "cust_005", "subject": "Invoice needs our VAT number", "body": "Our accountant is rejecting your invoices because they don't show our VAT ID (GB123456789). Can you add it to the billing profile and reissue the last three invoices?"}, "expected": {"category": "billing", "priority": "P2"}, "required_tools": ["lookup_customer"], "source": "hand-written"}
{"id": "b3", "tags": ["billing", "free-tier"], "ticket": {"customer_id": "cust_003", "subject": "Did you charge me?", "body": "I'm on the free plan but I see a $0.00 'authorization hold' from you on my bank statement. Is that going to turn into a real charge? I never entered a card on purpose."}, "expected": {"category": "billing", "priority": "P2"}, "required_tools": ["lookup_customer"], "source": "hand-written"}
{"id": "g1", "tags": ["bug", "known-incident"], "ticket": {"customer_id": "cust_001", "subject": "CSV export never finishes", "body": "Since this morning every CSV export of our shipments report spins for ~5 minutes then fails with 'export timed out'. We have 240 people who rely on this for the daily dispatch. Nothing changed on our side."}, "expected": {"category": "bug", "priority": "P0"}, "required_tools": ["lookup_customer", "search_incidents"], "source": "hand-written"}
{"id": "g2", "tags": ["bug", "known-incident"], "ticket": {"customer_id": "cust_006", "subject": "Webhooks arriving 15 minutes late", "body": "Our build pipeline listens for your webhook events and they've been showing up 10-20 minutes after the action since yesterday. It's making our deploy notifications useless. Is something up with webhook delivery?"}, "expected": {"category": "bug", "priority": "P1"}, "required_tools": ["lookup_customer", "search_incidents"], "source": "hand-written"}
{"id": "g3", "tags": ["bug", "free-tier"], "ticket": {"customer_id": "cust_008", "subject": "Dark mode resets on reload", "body": "Every time I refresh the page the theme flips back to light mode even though I've set dark mode in settings. Chrome 129, macOS. Minor but annoying."}, "expected": {"category": "bug", "priority": "P2"}, "required_tools": ["lookup_customer", "search_incidents"], "source": "hand-written"}
{"id": "f1", "tags": ["feature_request"], "ticket": {"customer_id": "cust_001", "subject": "Slack integration for alerts", "body": "Would love to get alert notifications posted straight into a Slack channel instead of email. Right now we bounce them through Zapier which works but is clunky. Is this on the roadmap?"}, "expected": {"category": "feature_request", "priority": "P3"}, "required_tools": ["lookup_customer"], "source": "hand-written"}
{"id": "f2", "tags": ["feature_request", "free-tier"], "ticket": {"customer_id": "cust_003", "subject": "Markdown tables?", "body": "Could you add support for Markdown tables in the notes field? Pipes just render as plain text right now."}, "expected": {"category": "feature_request", "priority": "P3"}, "required_tools": ["lookup_customer"], "source": "hand-written"}
{"id": "a1", "tags": ["account_access"], "ticket": {"customer_id": "cust_007", "subject": "Admin account locked", "body": "I'm the workspace owner and I've been locked out after too many password attempts (I was on a new laptop and mistyped). The unlock email never arrives. Our whole legal team can't add new matters until I'm back in."}, "expected": {"category": "account_access", "priority": "P0"}, "required_tools": ["lookup_customer"], "source": "hand-written"}
{"id": "a2", "tags": ["account_access", "known-incident"], "ticket": {"customer_id": "cust_002", "subject": "Okta SSO: 'SAML assertion invalid'", "body": "Starting about an hour ago everyone signing in through Okta gets 'SAML assertion invalid'. Nobody on our side touched the IdP config. About half the team is stuck at the login page."}, "expected": {"category": "account_access", "priority": "P1"}, "required_tools": ["lookup_customer", "search_incidents"], "source": "hand-written"}
{"id": "c1", "tags": ["churn_risk", "renewal-soon"], "ticket": {"customer_id": "cust_004", "subject": "Not renewing", "body": "We have renewal on the calendar for this month and honestly the export outage today was the last straw after three open tickets. Leadership has asked me to get quotes from two alternatives. Unless someone senior can get on a call this week I expect we will not renew."}, "expected": {"category": "churn_risk", "priority": "P0"}, "required_tools": ["lookup_customer"], "source": "hand-written"}
{"id": "c2", "tags": ["churn_risk", "billing-decoy"], "ticket": {"customer_id": "cust_006", "subject": "Considering downgrading to free", "body": "We were charged for 18 seats but only 11 people actually use the product. I've asked twice about a credit and heard nothing. If this isn't sorted out before our next invoice we'll downgrade to the free tier and move the team to something else."}, "expected": {"category": "churn_risk", "priority": "P1"}, "required_tools": ["lookup_customer"], "source": "hand-written"}
```

### Part 7: `scoring.py`, the math with no I/O

**What we add.** Pure functions over a list of result rows. Keeping I/O and
the API out of this file is what makes the numbers unit-testable.

**Per-class precision, recall, F1.** For every scored row compare expected
and predicted category. Equal: `tp[expected] += 1`. Different:
`fn[expected] += 1` and `fp[predicted] += 1`. That is the whole algorithm.
P = tp/(tp+fp), R = tp/(tp+fn), F1 is their harmonic mean. A class with no
predictions has undefined precision; report `None`, do not fake a zero.
Macro-F1 averages F1 over classes so the two-item classes count as much as
the three-item ones.

**Majority-class baseline.** Most common expected label over total. Accuracy
means nothing without this floor beside it.

**Tool pass-rate.** An attempt passes tools when every name in
`required_tools` appears among calls that did *not* error, and no call was to
an unknown tool.

**Pass.** category AND priority AND tools. Strict on purpose.

**pass^k.** For one case with `n` reps of which `c` passed, the probability
that `k` attempts drawn without replacement all pass is `C(c,k) / C(n,k)`.
Average over cases. `pass@k` (at least one passes) is
`1 − C(n−c,k) / C(n,k)`. Python's `math.comb` does both. With `n = 3, c = 2`:
pass^1 = 2/3, pass^2 = 1/3, pass^3 = 0. Part 8 tests exactly those values.

> **Note:** pass^k is the reliability number. pass@k tells you whether the
> agent *can* do it; pass^k tells you whether it *will*, every time, which is
> what an unattended system needs.

```python
"""Pure scoring functions. No I/O, no API - so the math is unit-testable.

Input is a list of result rows (one per (case, rep) that produced a decision)
plus the labelled cases. Rows that errored never reach here; the harness
reports them separately so plumbing failures don't masquerade as wrong answers.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from math import comb

from schemas import CATEGORIES


@dataclass
class ClassScores:
    tp: int = 0
    fp: int = 0
    fn: int = 0

    @property
    def precision(self) -> float | None:
        return self.tp / (self.tp + self.fp) if (self.tp + self.fp) else None

    @property
    def recall(self) -> float | None:
        return self.tp / (self.tp + self.fn) if (self.tp + self.fn) else None

    @property
    def f1(self) -> float | None:
        p, r = self.precision, self.recall
        if p is None or r is None or (p + r) == 0:
            return None
        return 2 * p * r / (p + r)


@dataclass
class Report:
    n_cases: int
    n_attempts: int
    n_errors: int
    accuracy: float
    majority_baseline: float
    priority_exact: float
    tool_pass_rate: float
    pass_rate: float
    per_class: dict[str, ClassScores]
    macro_f1: float
    pass_pow_k: dict[int, float]  # k -> pass^k
    pass_at_k: dict[int, float]  # k -> pass@k
    per_case_passes: dict[str, tuple[int, int]] = field(default_factory=dict)  # case -> (passes, reps)
    failures: list[dict] = field(default_factory=list)


def judge(case: dict, predicted: dict, tool_calls: list[dict]) -> dict:
    """Per-attempt verdicts. `predicted` is the validated decision as a dict."""
    called_ok = {tc["name"] for tc in tool_calls if not tc["is_error"]}
    unknown = [tc for tc in tool_calls if tc["is_error"] and tc["content"].startswith("Unknown tool")]
    tools_ok = set(case["required_tools"]) <= called_ok and not unknown
    category_ok = predicted["category"] == case["expected"]["category"]
    priority_ok = predicted["priority"] == case["expected"]["priority"]
    return {
        "category_ok": category_ok,
        "priority_ok": priority_ok,
        "tools_ok": tools_ok,
        "passed": category_ok and priority_ok and tools_ok,
    }


def pass_pow_k(passes: int, reps: int, k: int) -> float:
    """P(all k sampled attempts pass), sampling k of `reps` without replacement."""
    if k > reps:
        raise ValueError(f"k={k} exceeds reps={reps}")
    return comb(passes, k) / comb(reps, k)


def pass_at_k(passes: int, reps: int, k: int) -> float:
    """P(at least one of k sampled attempts passes)."""
    if k > reps:
        raise ValueError(f"k={k} exceeds reps={reps}")
    return 1.0 - comb(reps - passes, k) / comb(reps, k)


def score(cases: list[dict], rows: list[dict], n_errors: int = 0) -> Report:
    by_id = {c["id"]: c for c in cases}
    per_class = {c: ClassScores() for c in CATEGORIES}
    per_case: dict[str, list[bool]] = defaultdict(list)
    failures = []
    n_cat = n_pri = n_tools = n_pass = 0

    for row in rows:
        case = by_id[row["case_id"]]
        exp, pred = case["expected"]["category"], row["predicted"]["category"]
        if exp == pred:
            per_class[exp].tp += 1
        else:
            per_class[exp].fn += 1
            per_class[pred].fp += 1
        n_cat += row["category_ok"]
        n_pri += row["priority_ok"]
        n_tools += row["tools_ok"]
        n_pass += row["passed"]
        per_case[row["case_id"]].append(row["passed"])
        if not row["passed"]:
            failures.append(
                {
                    "case_id": row["case_id"],
                    "rep": row["rep"],
                    "expected": case["expected"],
                    "predicted": {"category": pred, "priority": row["predicted"]["priority"]},
                    "tools_ok": row["tools_ok"],
                    "tool_calls": [tc["name"] for tc in row["tool_calls"]],
                }
            )

    n = len(rows)
    majority = Counter(c["expected"]["category"] for c in cases).most_common(1)[0][1] / len(cases)
    f1s = [s.f1 for s in per_class.values() if s.f1 is not None or (s.tp + s.fn) > 0]
    macro_f1 = sum((f or 0.0) for f in f1s) / len(f1s) if f1s else 0.0

    reps = min((len(v) for v in per_case.values()), default=0)
    pow_k: dict[int, float] = {}
    at_k: dict[int, float] = {}
    for k in range(1, reps + 1):
        pow_k[k] = sum(pass_pow_k(sum(v), len(v), k) for v in per_case.values()) / len(per_case)
        at_k[k] = sum(pass_at_k(sum(v), len(v), k) for v in per_case.values()) / len(per_case)

    return Report(
        n_cases=len(cases),
        n_attempts=n,
        n_errors=n_errors,
        accuracy=n_cat / n if n else 0.0,
        majority_baseline=majority,
        priority_exact=n_pri / n if n else 0.0,
        tool_pass_rate=n_tools / n if n else 0.0,
        pass_rate=n_pass / n if n else 0.0,
        per_class=per_class,
        macro_f1=macro_f1,
        pass_pow_k=pow_k,
        pass_at_k=at_k,
        per_case_passes={cid: (sum(v), len(v)) for cid, v in per_case.items()},
        failures=failures,
    )


def _fmt(x: float | None) -> str:
    return "  -  " if x is None else f"{x:5.2f}"


def render(report: Report) -> str:
    lines = [
        f"cases={report.n_cases}  attempts={report.n_attempts}  errors(not scored)={report.n_errors}",
        "",
        f"category accuracy   {report.accuracy:.3f}   (majority-class baseline {report.majority_baseline:.3f})",
        f"macro F1            {report.macro_f1:.3f}",
        f"priority exact      {report.priority_exact:.3f}",
        f"tool pass-rate      {report.tool_pass_rate:.3f}   (required tools called, valid input, no unknown tools)",
        f"pass rate           {report.pass_rate:.3f}   (category AND priority AND tools)",
        "",
        f"{'class':<16}{'P':>7}{'R':>7}{'F1':>7}{'tp':>5}{'fp':>5}{'fn':>5}",
    ]
    for name, s in report.per_class.items():
        lines.append(f"{name:<16}{_fmt(s.precision):>7}{_fmt(s.recall):>7}{_fmt(s.f1):>7}{s.tp:>5}{s.fp:>5}{s.fn:>5}")
    if report.pass_pow_k:
        lines.append("")
        lines.append("k     pass^k   pass@k")
        for k in report.pass_pow_k:
            lines.append(f"{k:<6}{report.pass_pow_k[k]:.3f}    {report.pass_at_k[k]:.3f}")
    if report.failures:
        lines.append("")
        lines.append("failures:")
        for f in report.failures:
            lines.append(
                f"  {f['case_id']} rep{f['rep']}: expected {f['expected']['category']}/{f['expected']['priority']}, "
                f"got {f['predicted']['category']}/{f['predicted']['priority']}, tools_ok={f['tools_ok']} {f['tool_calls']}"
            )
    return "\n".join(lines)
```

### Part 8: `eval.py`, and proving the harness before paying for it

**What we add.** One job per `(case, rep)`, a thread pool, and a hard split
between two output files.

- `results.jsonl`: one row per attempt that produced a decision. Expected and
  predicted labels, every tool call, the per-attempt verdicts from `judge()`,
  `usage` tokens from the API, served model, latency, full transcript.
- `errors.jsonl`: one row per attempt that did *not* produce a decision, with
  `kind` from `AgentError`. Reported as a count, never scored as wrong. A
  runner that errors on every input must not look like a model that
  carefully got everything wrong.

`--rescore path/results.jsonl` re-runs `score()` on saved rows, so the metric
can change without spending money. `_serialise` exists because SDK content
blocks are Pydantic objects and the fakes are `SimpleNamespace`; both need to
become plain JSON for the transcript.

```python
"""Eval harness: run the agent over labelled cases, score it, keep the receipts.

    python eval.py                       # live API, 1 rep each
    python eval.py --reps 3 --workers 4  # pass^k needs reps
    python eval.py --client oracle       # offline: proves the harness scores a perfect run as 1.0
    python eval.py --client null         # offline: proves a constant answer scores badly
    python eval.py --rescore runs/<ts>/results.jsonl   # recompute metrics, no API calls

Every run writes to runs/<timestamp>/:
    results.jsonl  one row per attempt that produced a decision (with transcript)
    errors.jsonl   one row per attempt that did not (refusal, truncation, invalid JSON, API error)
    report.json    the numbers
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from pathlib import Path
from typing import Any

import agent
from agent import AgentError, triage
from fake_client import NullClient, OracleClient
from schemas import Ticket
from scoring import judge, render, score

HERE = Path(__file__).parent


def load_cases(path: Path) -> list[dict]:
    cases = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    ids = [c["id"] for c in cases]
    assert len(ids) == len(set(ids)), "duplicate case ids"
    return cases


def _serialise(obj: Any) -> Any:
    """Transcripts hold SDK content blocks; turn them into plain JSON."""
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    if hasattr(obj, "__dict__") and not isinstance(obj, dict):
        return {k: _serialise(v) for k, v in vars(obj).items()}
    if isinstance(obj, dict):
        return {k: _serialise(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_serialise(v) for v in obj]
    return obj


def run_attempt(case: dict, rep: int, client: Any) -> dict:
    """Returns either {"ok": True, "row": ...} or {"ok": False, "error": ...}."""
    ticket = Ticket.model_validate(case["ticket"])
    try:
        result = triage(ticket, client=client)
    except AgentError as exc:
        return {"ok": False, "error": {"case_id": case["id"], "rep": rep, "kind": exc.kind, "detail": exc.detail}}
    except Exception as exc:  # anything else is a harness bug; still don't score it as wrong
        return {"ok": False, "error": {"case_id": case["id"], "rep": rep, "kind": "harness_error", "detail": repr(exc)}}

    tool_calls = [asdict(tc) for tc in result.tool_calls]
    predicted = result.decision.model_dump()
    row = {
        "case_id": case["id"],
        "rep": rep,
        "expected": case["expected"],
        "predicted": predicted,
        "tool_calls": tool_calls,
        **judge(case, predicted, tool_calls),
        "model": result.model,
        "turns": result.turns,
        "input_tokens": result.input_tokens,
        "output_tokens": result.output_tokens,
        "latency_s": round(result.latency_s, 3),
        "transcript": _serialise(result.messages),
    }
    return {"ok": True, "row": row}


def run(cases: list[dict], reps: int, workers: int, client: Any) -> tuple[list[dict], list[dict]]:
    rows: list[dict] = []
    errors: list[dict] = []
    jobs = [(case, rep) for case in cases for rep in range(reps)]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(run_attempt, case, rep, client): (case["id"], rep) for case, rep in jobs}
        for fut in as_completed(futures):
            outcome = fut.result()
            (rows if outcome["ok"] else errors).append(outcome.get("row") or outcome["error"])
            cid, rep = futures[fut]
            mark = "ok " if outcome["ok"] else "ERR"
            print(f"  [{mark}] {cid} rep{rep}", file=sys.stderr)
    rows.sort(key=lambda r: (r["case_id"], r["rep"]))
    errors.sort(key=lambda r: (r["case_id"], r["rep"]))
    return rows, errors


def make_client(kind: str, cases: list[dict]) -> Any:
    if kind == "api":
        import anthropic

        return anthropic.Anthropic()
    if kind == "oracle":
        return OracleClient(cases)
    if kind == "null":
        return NullClient()
    raise ValueError(kind)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cases", type=Path, default=HERE / "cases.jsonl")
    ap.add_argument("--reps", type=int, default=1)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--client", choices=["api", "oracle", "null"], default="api")
    ap.add_argument("--model", default=None, help="override TRIAGE_MODEL for this run")
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--rescore", type=Path, default=None, help="score an existing results.jsonl instead of running")
    args = ap.parse_args(argv)

    cases = load_cases(args.cases)

    if args.rescore:
        rows = [json.loads(l) for l in args.rescore.read_text().splitlines() if l.strip()]
        err_path = args.rescore.with_name("errors.jsonl")
        n_err = sum(1 for l in err_path.read_text().splitlines() if l.strip()) if err_path.exists() else 0
        print(render(score(cases, rows, n_err)))
        return 0

    if args.model:
        agent.MODEL = args.model
    client = make_client(args.client, cases)
    out = args.out or HERE / "runs" / time.strftime("%Y%m%d-%H%M%S")
    out.mkdir(parents=True, exist_ok=True)

    print(f"running {len(cases)} cases x {args.reps} reps with client={args.client} model={agent.MODEL}", file=sys.stderr)
    rows, errors = run(cases, args.reps, args.workers, client)

    (out / "results.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    (out / "errors.jsonl").write_text("".join(json.dumps(e) + "\n" for e in errors))
    report = score(cases, rows, len(errors))
    (out / "report.json").write_text(json.dumps(_serialise(report), indent=2, default=str))

    print(render(report))
    if errors:
        print("\nerrors (see errors.jsonl):")
        for e in errors:
            print(f"  {e['case_id']} rep{e['rep']}: {e['kind']} {e['detail'][:120]}")
    print(f"\nwrote {out}/", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

**Test it, twice, before the first live run.** Two offline runs catch most
eval bugs:

```bash
python eval.py --client oracle   # every number must be 1.000
python eval.py --client null     # accuracy must equal the majority baseline (0.250), tool pass-rate 0
```

If the oracle does not score 1.0, the harness or grader is broken. If the
null baseline does not score badly, the grader is too lenient. Then pin the
math with planted failures. Do the arithmetic by hand once so the assertions
mean something:

- Relabel `c2` (churn_risk) as billing. churn_risk: 1 tp, 1 fn → recall
  0.50. billing: 3 tp, 1 fp → precision 0.75, recall still 1.0.
- Make `g3` skip its tools. Right answer, but `tools_ok` false → tool
  pass-rate 11/12, pass rate 10/12.

The three-rep test uses a client that fails `c2` every time and `g3` once,
and checks pass^k against the closed-form numbers from Part 7.

```python
"""Scoring math, checked against runs with known planted failures."""

from pathlib import Path

import pytest

import eval as harness
from fake_client import NullClient, OracleClient, Plant
from scoring import pass_at_k, pass_pow_k, score

CASES = harness.load_cases(Path(__file__).parent.parent / "cases.jsonl")


def _run(client, reps=1):
    rows, errors = harness.run(CASES, reps=reps, workers=2, client=client)
    return score(CASES, rows, len(errors)), errors


def test_case_set_shape():
    assert len(CASES) == 12
    from collections import Counter

    counts = Counter(c["expected"]["category"] for c in CASES)
    assert counts == {"billing": 3, "bug": 3, "feature_request": 2, "account_access": 2, "churn_risk": 2}


def test_oracle_scores_perfect():
    report, errors = _run(OracleClient(CASES))
    assert errors == []
    assert report.accuracy == 1.0 and report.tool_pass_rate == 1.0 and report.pass_rate == 1.0
    assert all(s.f1 == 1.0 for s in report.per_class.values())
    assert report.pass_pow_k == {1: 1.0}


def test_null_baseline_scores_badly():
    report, _ = _run(NullClient("billing", "P2"))
    assert report.accuracy == pytest.approx(3 / 12) == report.majority_baseline
    assert report.tool_pass_rate == 0.0  # never called lookup_customer
    assert report.per_class["billing"].precision == pytest.approx(3 / 12)
    assert report.per_class["churn_risk"].recall == 0.0


def test_two_planted_failures():
    plant = {
        "c2": Plant(category="billing"),  # churn_risk decoy mislabelled as billing
        "g3": Plant(skip_tools=True),  # right answer, but never looked anything up
    }
    report, errors = _run(OracleClient(CASES, plant=plant))
    assert errors == []
    assert report.per_class["churn_risk"].recall == pytest.approx(0.5)
    assert report.per_class["billing"].precision == pytest.approx(0.75)
    assert report.per_class["billing"].recall == 1.0
    assert report.accuracy == pytest.approx(11 / 12)
    assert report.tool_pass_rate == pytest.approx(11 / 12)
    assert report.pass_rate == pytest.approx(10 / 12)
    assert {f["case_id"] for f in report.failures} == {"c2", "g3"}


def test_pass_pow_k_with_reps():
    # c2 fails every rep, g3 fails 1 of 3 (planted via a flaky client)
    class Flaky(OracleClient):
        def __init__(self, cases):
            super().__init__(cases, plant={"c2": Plant(category="billing")})
            self._seen = 0

        def _answer(self, messages, **kw):
            if messages[0]["content"].startswith("customer_id: cust_008") and len(messages) == 1:
                self._seen += 1
                if self._seen == 1:
                    self.plant["g3"] = Plant(skip_tools=True)
                else:
                    self.plant.pop("g3", None)
            return super()._answer(messages, **kw)

    report, _ = _run(Flaky(CASES), reps=3)
    assert report.per_case_passes["c2"] == (0, 3)
    assert report.per_case_passes["g3"] == (2, 3)
    # 10 cases always pass, c2 never, g3 with prob 2/3 (k=1) or 1/3 (k=2) or 0 (k=3)
    assert report.pass_pow_k[1] == pytest.approx((10 + 0 + 2 / 3) / 12)
    assert report.pass_pow_k[2] == pytest.approx((10 + 0 + 1 / 3) / 12)
    assert report.pass_pow_k[3] == pytest.approx(10 / 12)
    assert report.pass_at_k[3] == pytest.approx(11 / 12)


def test_estimators_edge_cases():
    assert pass_pow_k(3, 3, 3) == 1.0 and pass_pow_k(2, 3, 3) == 0.0
    assert pass_at_k(1, 3, 3) == 1.0 and pass_at_k(0, 3, 2) == 0.0
    assert pass_pow_k(2, 4, 2) == pytest.approx(1 / 6)
    with pytest.raises(ValueError):
        pass_pow_k(1, 1, 2)
```

`tests/test_tools.py` pins the registry: every definition is strict-compatible,
errors are values not exceptions, keyword search hits the right incident.

```python
import json

from tools import REGISTRY, TOOL_DEFINITIONS, execute


def test_every_definition_is_strict_compatible():
    for d in TOOL_DEFINITIONS:
        schema = d["input_schema"]
        assert d["strict"] is True
        assert schema["additionalProperties"] is False, d["name"]
        # strict mode requires every property to be listed in `required`
        assert set(schema["required"]) == set(schema["properties"]), d["name"]
        assert d["description"]


def test_registry_and_definitions_agree():
    assert [d["name"] for d in TOOL_DEFINITIONS] == list(REGISTRY)


def test_lookup_customer_round_trips_json():
    out = execute("lookup_customer", {"customer_id": "cust_001"})
    assert not out.is_error
    rec = json.loads(out.content)
    assert rec["plan"] == "enterprise" and rec["renewal_in_days"] == 22


def test_unknown_customer_is_recoverable_error():
    out = execute("lookup_customer", {"customer_id": "cust_999"})
    assert out.is_error and "cust_999" in out.content


def test_invalid_input_is_error_not_exception():
    out = execute("lookup_customer", {"customer_id": 5, "extra": "x"})
    assert out.is_error and "Invalid input" in out.content


def test_unknown_tool_is_error():
    out = execute("delete_customer", {})
    assert out.is_error and out.content.startswith("Unknown tool")


def test_search_incidents_matches_keywords():
    hit = json.loads(execute("search_incidents", {"query": "csv export timing out"}).content)
    assert [m["incident_id"] for m in hit["matches"]] == ["INC-2291"]
    miss = json.loads(execute("search_incidents", {"query": "dark mode theme"}).content)
    assert miss["matches"] == []
```

```bash
python -m pytest -q     # 19 passed
```

### Part 9: `agent_runner.py`, when the SDK drives the loop

**What we add.** The same agent, the other way. `@beta_tool` reads the
function signature and the Google-style docstring and generates the tool
definition (the `Args:` section becomes per-parameter descriptions).
`client.beta.messages.tool_runner` takes the decorated functions plus the
same `system` and `output_config`; iterating it yields one message per
assistant turn. The whole `for turn in ...` block from Part 3 is gone.

The handlers call the same `execute()`, so error behaviour is identical. The
one thing the runner will not do for you is hand back the transcript; the
`history` list mirrors it by appending each message and
`runner.generate_tool_call_response()` (cached, so the tools still run once).

**When to own the loop instead:** the runner is beta; you need the transcript
for the harness; per-turn control (approval gates, retries, error classes,
turn budgets) is easier in twelve lines you own; and the Python runner does
not auto-resume `pause_turn`.

```python
"""Same agent, but the SDK drives the loop: `client.beta.messages.tool_runner`.

`@beta_tool` reads the function signature and docstring and generates the
tool definition; the runner calls the API, executes whichever tool Claude
asked for, appends the results, and repeats until `end_turn`. The whole of
agent.py's `for turn in ...` block becomes `runner.until_done()`.

Why you would still drop to the manual loop (agent.py):
  - it's beta (`client.beta.*`), and the request shapes it can build are the
    ones it knows about;
  - you don't get the transcript back unless you mirror it yourself (see
    `history` below), and the eval harness needs the transcript;
  - per-turn control (a human approval gate, retry policy, error routing,
    counting turns for a budget) is easier to reason about when the loop is
    twelve lines you own;
  - the runner does not auto-resume `pause_turn` in the Python SDK.

Usage: echo '{"customer_id": "cust_001", "subject": "...", "body": "..."}' | python agent_runner.py
"""

from __future__ import annotations

import json
import sys

import anthropic
from anthropic import beta_tool

from agent import MAX_TOKENS, MODEL, SYSTEM_PROMPT
from schemas import DECISION_SCHEMA, LookupCustomerInput, SearchIncidentsInput, Ticket, TriageDecision
from tools import execute


# The decorator builds the schema from the signature + docstring. The handlers
# are the same registry handlers; `execute` keeps error handling identical.


@beta_tool
def lookup_customer(customer_id: str) -> str:
    """Fetch the account record for the customer who filed the ticket. Call this first for every ticket.

    Args:
        customer_id: The customer_id exactly as it appears on the ticket.
    """
    return execute("lookup_customer", LookupCustomerInput(customer_id=customer_id).model_dump()).content


@beta_tool
def search_incidents(query: str) -> str:
    """Search currently open platform incidents by keyword. Call this whenever a ticket describes something broken, slow, or failing.

    Args:
        query: Two to five keywords describing the product area or symptom.
    """
    return execute("search_incidents", SearchIncidentsInput(query=query).model_dump()).content


def triage_with_runner(ticket: Ticket, client: anthropic.Anthropic | None = None) -> tuple[TriageDecision, list[dict]]:
    client = client or anthropic.Anthropic()
    history: list[dict] = [{"role": "user", "content": ticket.render()}]

    runner = client.beta.messages.tool_runner(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=SYSTEM_PROMPT,
        tools=[lookup_customer, search_incidents],
        output_config={"effort": "low", "format": {"type": "json_schema", "schema": DECISION_SCHEMA}},
        messages=history,
    )

    final = None
    for message in runner:  # one iteration per assistant turn
        final = message
        history.append({"role": "assistant", "content": message.content})
        tool_response = runner.generate_tool_call_response()  # cached: tools still run once
        if tool_response is not None:
            history.append(tool_response)

    if final is None or final.stop_reason != "end_turn":
        raise RuntimeError(f"runner ended with stop_reason={getattr(final, 'stop_reason', None)!r}")
    text = "".join(b.text for b in final.content if b.type == "text")
    return TriageDecision.model_validate_json(text), history


if __name__ == "__main__":
    decision, _ = triage_with_runner(Ticket.model_validate_json(sys.stdin.read()))
    print(json.dumps(decision.model_dump(), indent=2))
```

### Part 10: The live run

Check the request shapes against the real SDK first, with no network. Point
the client at an unroutable address with retries off; a bad parameter or
schema raises before any connection attempt, so if the only exception is
`APIConnectionError`, the request serialised fine:

```bash
python - <<'PY'
import anthropic, agent
from agent_runner import lookup_customer, search_incidents
c = anthropic.Anthropic(api_key="sk-fake", base_url="http://127.0.0.1:9", max_retries=0)
try:
    c.messages.create(**agent._request_params(), messages=[{"role": "user", "content": "x"}])
except anthropic.APIConnectionError:
    print("manual loop request shape OK")
try:
    c.beta.messages.tool_runner(model=agent.MODEL, max_tokens=100, system="s",
        tools=[lookup_customer, search_incidents], messages=[{"role": "user", "content": "x"}]).until_done()
except anthropic.APIConnectionError:
    print("tool_runner request shape OK")
PY
```

Then:

```bash
export ANTHROPIC_API_KEY=your_key
python eval.py --reps 3 --workers 4
```

Read `runs/<timestamp>/results.jsonl` for every failure before touching the
prompt. The transcript is in the row. Most surprising scores are eval bugs,
not facts about the model. When a change to the prompt is warranted, change
one thing, re-run with the same reps, compare pass^k, keep or revert.

## Best Practices Reference

| Practice | Why | Where in this repo |
|---|---|---|
| Contract first, loop second | The loop exists to produce one object; define the object | `schemas.py` before `agent.py` |
| One Pydantic base for every boundary | `extra="forbid"` + no defaults is the strict/structured-output schema subset | `StrictModel` |
| Two tools with different necessity | Makes tool usage a measurable behaviour | `tools.py`, `required_tools` |
| Tool errors are results | The model can recover from a value, not from an exception | `execute()` |
| Verbatim assistant turn, one results message, matched ids | The three 400s in a tool-use loop | `agent.py`, `test_loop.py` |
| Bounded loop, classified failures | Unattended runs need a ceiling and a reason | `max_turns`, `AgentError.kind` |
| Validate the structured output anyway | Downstream code should never trust unvalidated text | `model_validate_json` |
| Stable request prefix | Prompt caching is a prefix match | `_request_params()` |
| Fake the one method the loop uses | Offline tests, offline harness checks | `fake_client.py` |
| Errors in a sidecar, never a zero | Plumbing must not score as model failure | `errors.jsonl` |
| Oracle and null runs before the first paid run | Proves the harness measures what you think | `--client oracle`, `--client null` |
| Baseline beside accuracy | A number needs a floor | `majority_baseline` |
| Both-directions decoys, hand-written labels | Otherwise the eval rewards a bias | `b1`, `c2`, `source` |
| Reps and pass^k | Reliability, not just capability | `--reps`, `pass_pow_k` |
| Transcript and usage per row | Debug a score without re-running; cost from real tokens | `results.jsonl` |
| Introspect the SDK before writing a call | The API drifts faster than memory | Part 0 |

## The Drill

```bash
rm agent.py
python -m pytest tests/test_loop.py -q      # ImportError across the board
# rewrite agent.py from the four-move description in Part 3
python -m pytest -q                         # 19 passed
```

Things to be able to say without looking:

1. The `stop_reason` values you branch on and what each means for the transcript.
2. Why `response.content` goes back whole, why tool results share one user message, and what field ties a result to its call.
3. Where `strict: true` goes versus where `output_config.format` goes, and what each constrains.
4. What `extra="forbid"` does to a Pydantic schema and why the API cares.
5. Why an `is_error` tool result beats raising.
6. What `tool_runner` does for you, and three reasons you would still write the loop.
7. How tp/fp/fn are counted from one (expected, predicted) pair, and the formula for pass^k.
8. Why errors get their own file instead of a zero.

## Next Steps

Extensions that keep the shape:

- Replace the in-memory tables in `tools.py` with a real datastore. Nothing else changes.
- Add a `cache_control` breakpoint on the system prompt and confirm `usage.cache_read_input_tokens` climbs across the eval run.
- Route the eval through the Message Batches API for a 50% cost reduction when latency does not matter.
- Add a wall-clock ceiling per case in `run_attempt` and a `timeout` failure class.
- Grow the case set from production tickets, label by hand, keep `source` honest, and re-run the oracle and null checks every time the grader changes.
- Hill-climb the prompt against pass^k with a held-out split, one change per round.

Post seeds, each one an argument this repo can back with running code:

1. *The loop is the agent.* Everything people call "agent architecture" is four moves around one API call, and the three ways it breaks fit in a sentence.
2. *Strict is not structured.* `strict: true` and `output_config.format` are two features that look alike, constrain different things, and compose. One Pydantic setting satisfies both.
3. *Errors are results.* Why a production tool returns `is_error` instead of raising, and what that does to recovery.
4. *Prove the harness before you trust it.* The oracle run, the null run, and the planted failure: three offline checks that catch most eval bugs.
5. *pass@k versus pass^k.* Capability versus reliability, and why unattended systems need the second number.
6. *Let the SDK drive, until it can't.* `tool_runner` versus the manual loop, and the exact points where you drop down.

## Appendix: API Details Verified While Building

- `client.messages.create()` accepts `output_config` (with `format` and `effort`). The top-level `output_format` parameter is deprecated; `client.messages.parse(output_format=PydanticModel)` still takes a type and merges it into `output_config` for you.
- `strict: true` sits on the tool definition alongside `name`, `description`, `input_schema`, not on `tool_choice`. The schema needs `additionalProperties: false` and every property in `required`.
- `client.beta.messages.tool_runner()` accepts `system`, `output_config`, `max_iterations`, and a `tools` list of `@beta_tool` functions. Iterating the runner yields one `BetaMessage` per assistant turn; `runner.generate_tool_call_response()` returns the user message the runner is about to send.
- On Claude Fable 5.1, forced `tool_choice` (`any`, `tool`) returns a 400; `auto` plus a prompt instruction, with `strict: true` for argument shape, is the replacement. The `thinking` parameter is omitted (adaptive is the default on Claude Opus 5 and always-on on Claude Fable 5.1); `output_config.effort` controls depth.
- `response.stop_details` is populated only when `stop_reason == "refusal"`; guard before reading.
- The `anthropic` 1.x SDK is built on `httpx2`; `anthropic.APIConnectionError` and `anthropic.APIStatusError` are the two classes the loop catches, most specific first.

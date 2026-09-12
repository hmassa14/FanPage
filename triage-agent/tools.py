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

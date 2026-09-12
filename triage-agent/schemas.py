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

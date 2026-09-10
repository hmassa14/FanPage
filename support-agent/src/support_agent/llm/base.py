"""Provider interface. The pipeline only ever talks to this.

Two implementations ship: `AnthropicProvider` (real) and `FakeProvider` (deterministic,
offline). Tests and CI run on the fake; the eval harness can run on either.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, Protocol, TypeVar

from ..knowledge.tools import ResearchTools
from ..models import (
    DraftReply,
    GroundingVerdict,
    RedactedEmail,
    ResearchBrief,
    StageUsage,
    TriageResult,
)

T = TypeVar("T")


@dataclass
class StageOutput(Generic[T]):
    result: T
    usage: StageUsage


class RefusalError(RuntimeError):
    """The model declined the request (stop_reason == 'refusal') and no fallback rescued it."""


class ModelOutputError(RuntimeError):
    """The model returned output that failed schema validation after retries."""


class LLMProvider(Protocol):
    name: str

    def triage(self, email: RedactedEmail) -> StageOutput[TriageResult]: ...

    def research(
        self, email: RedactedEmail, triage: TriageResult, tools: ResearchTools
    ) -> StageOutput[ResearchBrief]: ...

    def draft(
        self, email: RedactedEmail, triage: TriageResult, brief: ResearchBrief
    ) -> StageOutput[DraftReply]: ...

    def judge(
        self, email: RedactedEmail, brief: ResearchBrief, draft: DraftReply
    ) -> StageOutput[GroundingVerdict]: ...

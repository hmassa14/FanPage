"""Claude-backed provider.

Notable production choices:
  * `client.beta.messages.parse` + Pydantic `output_format` for every stage: the API
    constrains output to the schema, the SDK validates it, and we retry once on a
    validation failure (rare, but "rare" times "thousands of emails" is "daily").
  * Adaptive thinking on, per-stage `effort` from settings (low for triage, high for
    research/draft, medium for judge). Effort is the cost lever; measure before changing.
  * `fallbacks="default"`: policy refusals are re-run server-side on Anthropic's
    recommended model instead of failing the ticket. Toggle with SA_ANTHROPIC_FALLBACKS.
  * Stable system prompt cached with `cache_control`; everything volatile is in the user turn.
  * Research is a manual tool loop (read-only tools, bounded rounds), then one
    `tool_choice: none` call that produces the typed brief.
"""

from __future__ import annotations

import logging
import time
from typing import Any, TypeVar, cast

import anthropic
import pydantic
from anthropic.types.beta import BetaMessage, BetaMessageParam, BetaToolParam

from ..config import Settings, StageModelConfig
from ..knowledge.tools import TOOL_DEFINITIONS, ResearchTools
from ..logging_setup import log
from ..models import (
    DraftReply,
    GroundingVerdict,
    RedactedEmail,
    ResearchBrief,
    StageUsage,
    TriageResult,
)
from . import prompts, render
from .base import ModelOutputError, RefusalError, StageOutput

logger = logging.getLogger(__name__)
M = TypeVar("M", bound=pydantic.BaseModel)

FALLBACK_BETA = "server-side-fallback-2026-07-01"


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, settings: Settings, client: anthropic.Anthropic | None = None) -> None:
        self.s = settings
        # Zero-arg client: resolves ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN / `ant auth login`.
        self.client = client or anthropic.Anthropic()

    # ---- request plumbing --------------------------------------------------- #
    def _request_kwargs(self, cfg: StageModelConfig, system: str) -> dict[str, Any]:
        kw: dict[str, Any] = {
            "model": cfg.model,
            "max_tokens": cfg.max_tokens,
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": cfg.effort},
            "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        }
        if self.s.anthropic_fallbacks:
            kw["betas"] = [FALLBACK_BETA]
            kw["fallbacks"] = "default"
        return kw

    def _usage(self, stage: str, cfg: StageModelConfig, resp: BetaMessage, t0: float) -> StageUsage:
        u = resp.usage
        cache_read = u.cache_read_input_tokens or 0
        cache_write = u.cache_creation_input_tokens or 0
        in_price, out_price = self.s.price(resp.model or cfg.model)
        cost = (
            u.input_tokens * in_price
            + cache_read * in_price * 0.1
            + cache_write * in_price * 1.25
            + u.output_tokens * out_price
        ) / 1_000_000
        return StageUsage(
            stage=stage,
            model=resp.model or cfg.model,
            input_tokens=u.input_tokens,
            output_tokens=u.output_tokens,
            cache_read_tokens=cache_read,
            cache_write_tokens=cache_write,
            latency_ms=int((time.perf_counter() - t0) * 1000),
            cost_usd=round(cost, 6),
        )

    @staticmethod
    def _check_refusal(resp: BetaMessage, stage: str) -> None:
        if resp.stop_reason == "refusal":
            details = getattr(resp, "stop_details", None)
            raise RefusalError(f"{stage}: model refused ({details})")
        if resp.stop_reason == "max_tokens":
            raise ModelOutputError(f"{stage}: hit max_tokens before finishing; raise max_tokens")

    def _parse_stage(
        self,
        stage: str,
        cfg: StageModelConfig,
        system: str,
        messages: list[dict[str, Any]],
        output_format: type[M],
        **extra: Any,
    ) -> tuple[M, StageUsage]:
        last_err: Exception | None = None
        for attempt in range(2):
            t0 = time.perf_counter()
            try:
                resp = self.client.beta.messages.parse(
                    messages=cast(list[BetaMessageParam], messages),
                    output_format=output_format,
                    **self._request_kwargs(cfg, system),
                    **extra,
                )
            except pydantic.ValidationError as exc:
                # The SDK validated the JSON against the schema and it did not fit. Retry once.
                last_err = exc
                log(
                    logger,
                    logging.WARNING,
                    "schema validation failed; retrying",
                    stage=stage,
                    attempt=attempt,
                    error=str(exc)[:500],
                )
                continue
            self._check_refusal(resp, stage)
            usage = self._usage(stage, cfg, resp, t0)
            for block in resp.content:
                parsed = getattr(block, "parsed_output", None)
                if parsed is not None:
                    return parsed, usage
            last_err = ModelOutputError(f"{stage}: response contained no JSON text block")
        raise ModelOutputError(f"{stage}: could not obtain valid output: {last_err}")

    # ---- stages --------------------------------------------------------------- #
    def triage(self, email: RedactedEmail) -> StageOutput[TriageResult]:
        result, usage = self._parse_stage(
            "triage",
            self.s.triage,
            prompts.triage_system(self.s),
            [{"role": "user", "content": render.triage_user(email)}],
            TriageResult,
        )
        return StageOutput(result, usage)

    def research(
        self, email: RedactedEmail, triage: TriageResult, tools: ResearchTools
    ) -> StageOutput[ResearchBrief]:
        cfg, system = self.s.research, prompts.research_system(self.s)
        messages: list[dict[str, Any]] = [{"role": "user", "content": render.research_user(email, triage)}]
        totals = StageUsage(stage="research", model=cfg.model)

        for _round in range(self.s.research_max_tool_rounds):
            t0 = time.perf_counter()
            resp = self.client.beta.messages.create(
                messages=cast(list[BetaMessageParam], messages),
                tools=cast(list[BetaToolParam], TOOL_DEFINITIONS),
                **self._request_kwargs(cfg, system),
            )
            self._check_refusal(resp, "research")
            _accumulate(totals, self._usage("research", cfg, resp, t0))
            messages.append({"role": "assistant", "content": resp.content})
            tool_uses = [b for b in resp.content if b.type == "tool_use"]
            if not tool_uses:
                break
            results = []
            for tu in tool_uses:
                out, ok = tools.dispatch(tu.name, dict(tu.input))  # type: ignore[arg-type]
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tu.id,
                        "content": out,
                        "is_error": not ok,
                    }
                )
            messages.append({"role": "user", "content": results})  # all results in ONE turn

        messages.append({"role": "user", "content": render.research_finalize()})
        brief, usage = self._parse_stage(
            "research",
            cfg,
            system,
            messages,
            ResearchBrief,
            tools=cast(list[BetaToolParam], TOOL_DEFINITIONS),
            tool_choice={"type": "none"},
        )
        _accumulate(totals, usage)
        brief.tools_used = tools.names_used()
        return StageOutput(brief, totals)

    def draft(
        self, email: RedactedEmail, triage: TriageResult, brief: ResearchBrief
    ) -> StageOutput[DraftReply]:
        result, usage = self._parse_stage(
            "draft",
            self.s.draft,
            prompts.draft_system(self.s),
            [{"role": "user", "content": render.draft_user(email, triage, brief)}],
            DraftReply,
        )
        return StageOutput(result, usage)

    def judge(
        self, email: RedactedEmail, brief: ResearchBrief, draft: DraftReply
    ) -> StageOutput[GroundingVerdict]:
        result, usage = self._parse_stage(
            "judge",
            self.s.judge,
            prompts.judge_system(self.s),
            [{"role": "user", "content": render.judge_user(email, brief, draft)}],
            GroundingVerdict,
        )
        return StageOutput(result, usage)


def _accumulate(total: StageUsage, part: StageUsage) -> None:
    total.model = part.model
    total.input_tokens += part.input_tokens
    total.output_tokens += part.output_tokens
    total.cache_read_tokens += part.cache_read_tokens
    total.cache_write_tokens += part.cache_write_tokens
    total.latency_ms += part.latency_ms
    total.cost_usd = round(total.cost_usd + part.cost_usd, 6)

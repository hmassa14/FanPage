"""Policy gates: deterministic rules evaluated after the model has done its work.

Principles:
  * Rules are data (data/policy.yaml), versioned, and the version is stamped on the decision.
  * Gates fail closed. A missing judge verdict, an unknown action type, or an unparseable
    field all route to a human; nothing goes out because a check could not run.
  * Order of precedence: reject > escalate > needs_approval > auto_send.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from ..models import (
    ActionType,
    DraftReply,
    GateDecision,
    GateReason,
    GateResult,
    GroundingVerdict,
    RedactedEmail,
    ResearchBrief,
    Sentiment,
    TriageCategory,
    TriageResult,
    Urgency,
)
from ..redaction import contains_placeholder

_URGENCY_RANK = {Urgency.low: 0, Urgency.normal: 1, Urgency.high: 2, Urgency.critical: 3}


@dataclass
class Policy:
    version: str
    raw: dict[str, Any]
    escalate_patterns: list[re.Pattern[str]] = field(default_factory=list)
    injection_patterns: list[re.Pattern[str]] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path) -> Policy:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        return cls.from_dict(raw)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Policy:
        esc = [re.compile(p, re.IGNORECASE) for p in raw.get("escalate", {}).get("keywords", [])]
        inj = [re.compile(p, re.IGNORECASE) for p in (raw.get("content") or {}).get("injection_patterns", [])]
        return cls(version=str(raw["version"]), raw=raw, escalate_patterns=esc, injection_patterns=inj)

    def section(self, name: str) -> dict[str, Any]:
        return self.raw.get(name) or {}


def evaluate(
    policy: Policy,
    email: RedactedEmail,
    triage: TriageResult,
    brief: ResearchBrief | None,
    draft: DraftReply | None,
    judge: GroundingVerdict | None,
) -> GateResult:
    reasons: list[GateReason] = []
    auto, esc, rej = (
        policy.section("auto_send"),
        policy.section("escalate"),
        policy.section("reject"),
    )
    actions_cfg, content = policy.section("actions"), policy.section("content")

    # ---- 1. reject ----------------------------------------------------------- #
    if triage.category.value in rej.get("categories", []):
        reasons.append(
            GateReason(
                rule="reject.category",
                severity="reject",
                detail=f"category={triage.category.value}",
            )
        )
        return GateResult(decision=GateDecision.reject, reasons=reasons, policy_version=policy.version)

    # ---- 2. escalate (deterministic tripwires) -------------------------------- #
    if triage.category.value in esc.get("categories", []):
        reasons.append(
            GateReason(
                rule="escalate.category",
                severity="escalate",
                detail=f"category={triage.category.value}",
            )
        )
    haystack = f"{email.subject}\n{email.body_text}"
    for pat in policy.escalate_patterns:
        m = pat.search(haystack)
        if m:
            reasons.append(
                GateReason(
                    rule="escalate.keyword",
                    severity="escalate",
                    detail=f"matched {pat.pattern!r}: '{m.group(0)}'",
                )
            )
            break
    if triage.sentiment.value in esc.get("sentiments", []):
        reasons.append(
            GateReason(
                rule="escalate.sentiment",
                severity="escalate",
                detail=f"sentiment={triage.sentiment.value}",
            )
        )
    if triage.requires_human_reason:
        reasons.append(
            GateReason(rule="escalate.model_flag", severity="escalate", detail=triage.requires_human_reason)
        )
    if triage.urgency == Urgency.critical:
        reasons.append(GateReason(rule="escalate.urgency", severity="escalate", detail="urgency=critical"))
    if draft and any(a.type == ActionType.escalate_to_human for a in draft.proposed_actions):
        reasons.append(
            GateReason(
                rule="escalate.proposed_action",
                severity="escalate",
                detail="draft proposed escalate_to_human",
            )
        )
    if any(r.severity == "escalate" for r in reasons):
        return GateResult(decision=GateDecision.escalate, reasons=reasons, policy_version=policy.version)

    # ---- 3. needs_approval (anything below blocks auto-send) ------------------ #
    def block(rule: str, detail: str) -> None:
        reasons.append(GateReason(rule=rule, severity="block", detail=detail))

    if draft is None or brief is None:
        block("pipeline.incomplete", "no draft/brief produced")
        return GateResult(
            decision=GateDecision.needs_approval, reasons=reasons, policy_version=policy.version
        )

    if triage.category.value not in auto.get("allowed_categories", []):
        block("auto_send.category", f"{triage.category.value} is not auto-sendable")
    if triage.confidence < float(auto.get("min_triage_confidence", 1.0)):
        block(
            "auto_send.triage_confidence",
            f"{triage.confidence:.2f} < {auto.get('min_triage_confidence')}",
        )
    if draft.confidence < float(auto.get("min_draft_confidence", 1.0)):
        block(
            "auto_send.draft_confidence",
            f"{draft.confidence:.2f} < {auto.get('min_draft_confidence')}",
        )
    max_urg = auto.get("max_urgency", "high")
    if _URGENCY_RANK[triage.urgency] > _URGENCY_RANK[Urgency(max_urg)]:
        block("auto_send.urgency", f"urgency={triage.urgency.value} > {max_urg}")
    if auto.get("require_kb_citation") and not draft.cited_doc_ids:
        block("auto_send.citation", "reply cites no knowledge-base document")
    if auto.get("review_declines") and draft.declines_request:
        block("auto_send.decline_review", "reply declines the customer's request; a person reviews refusals")
    if brief.open_questions and draft.proposed_actions:
        block("auto_send.open_questions", "brief has open questions but draft promises actions")
    words = len(draft.body.split())
    if words > int(auto.get("max_reply_words", 10_000)):
        block("auto_send.length", f"{words} words > {auto.get('max_reply_words')}")

    # judge: fail closed
    if judge is None:
        block("judge.missing", "grounding judge did not run")
    else:
        if judge.score < float(auto.get("min_grounding_score", 1.0)) or not judge.grounded:
            block(
                "judge.grounding",
                f"score={judge.score:.2f} grounded={judge.grounded} unsupported={judge.unsupported_claims}",
            )
        if not judge.tone_ok:
            block("judge.tone", "; ".join(judge.issues) or "tone flagged")
        if judge.policy_conflicts:
            block("judge.policy_conflict", "; ".join(judge.policy_conflicts))

    # content rules
    if content.get("block_if_contains_placeholder", True) and contains_placeholder(draft.body):
        block("content.placeholder_leak", "reply contains a redaction placeholder")
    for pat in policy.injection_patterns:
        m = pat.search(haystack)
        if m:
            block("content.injection_attempt", f"email matched {pat.pattern!r}: '{m.group(0)}'")
            break
    lowered = draft.body.lower()
    for phrase in content.get("banned_phrases", []):
        if phrase.lower() in lowered:
            block("content.banned_phrase", f"contains '{phrase}'")
    if content.get("require_language_match") and triage.language != "en":
        block("content.language", f"non-English ({triage.language}) replies require review")

    # actions
    auto_ok: dict[str, Any] = actions_cfg.get("auto_approve", {}) or {}
    always_human = set(actions_cfg.get("always_require_approval", []) or [])
    for a in draft.proposed_actions:
        if a.type == ActionType.none:
            continue
        if a.type.value in always_human:
            block("actions.always_require_approval", f"{a.type.value} order={a.order_id}")
            continue
        rule = auto_ok.get(a.type.value)
        if rule is None:
            block("actions.not_allowlisted", f"{a.type.value} is not auto-approvable")
            continue
        if (
            a.type == ActionType.issue_refund
            and actions_cfg.get("refund_must_not_exceed_order_total")
            and a.amount_usd is not None
            and brief.order_total_usd is not None
            and a.amount_usd > brief.order_total_usd + 0.005
        ):
            block(
                "actions.exceeds_order_total",
                f"refund ${a.amount_usd:.2f} > order total ${brief.order_total_usd:.2f}",
            )
            continue
        cap = rule.get("max_amount_usd") if isinstance(rule, dict) else None
        if cap is not None:
            if a.amount_usd is None:
                block("actions.amount_missing", f"{a.type.value} has no amount")
            elif a.amount_usd > float(cap):
                block(
                    "actions.amount_cap",
                    f"{a.type.value} ${a.amount_usd:.2f} > cap ${float(cap):.2f}",
                )
            else:
                reasons.append(
                    GateReason(
                        rule="actions.auto_approved",
                        severity="info",
                        detail=f"{a.type.value} ${a.amount_usd:.2f} <= cap ${float(cap):.2f}",
                    )
                )
        else:
            reasons.append(GateReason(rule="actions.auto_approved", severity="info", detail=a.type.value))

    if any(r.severity == "block" for r in reasons):
        return GateResult(
            decision=GateDecision.needs_approval, reasons=reasons, policy_version=policy.version
        )
    reasons.append(GateReason(rule="auto_send", severity="info", detail="all checks passed"))
    return GateResult(decision=GateDecision.auto_send, reasons=reasons, policy_version=policy.version)


# Referenced for type completeness in tests; keeps the import graph explicit.
_ = (Sentiment, TriageCategory)

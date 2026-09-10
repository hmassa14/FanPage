"""Deterministic offline provider.

It is NOT a mock that returns canned strings: it runs the real retrieval and CRM tools
and applies simple heuristics, so the demo, the tests, and the eval harness exercise the
same pipeline and the same gates the real model goes through. Use it for CI and for
local development without an API key.
"""

from __future__ import annotations

import json
import re

from ..config import Settings
from ..knowledge.tools import ResearchTools
from ..models import (
    ActionType,
    DraftReply,
    ExtractedEntities,
    GroundingVerdict,
    KBCitation,
    ProposedAction,
    RedactedEmail,
    ResearchBrief,
    ResearchFinding,
    Sentiment,
    StageUsage,
    TriageCategory,
    TriageResult,
    Urgency,
)
from .base import StageOutput

_ORDER_RE = re.compile(r"\bNW-\d{4,6}\b", re.IGNORECASE)
_MONEY_RE = re.compile(r"\$\s?(\d+(?:\.\d{2})?)")

_CATEGORY_RULES: list[tuple[TriageCategory, list[str]]] = [
    (
        TriageCategory.spam_or_irrelevant,
        ["seo", "backlinks", "unsubscribe", "partnership opportunity", "guest post"],
    ),
    (
        TriageCategory.data_privacy_request,
        ["delete my data", "delete my account", "gdpr", "ccpa", "export my data"],
    ),
    (
        TriageCategory.legal_or_regulatory,
        ["lawyer", "attorney", "lawsuit", "sue ", "chargeback", "legal action", "bbb", "press"],
    ),
    (
        TriageCategory.account_access,
        ["password", "log in", "login", "locked out", "can't sign in", "cannot sign in"],
    ),
    (TriageCategory.return_or_refund, ["return", "refund", "money back", "send it back"]),
    (
        TriageCategory.shipping_issue,
        ["damaged", "arrived broken", "missing package", "never arrived", "wrong item", "lost"],
    ),
    (
        TriageCategory.order_status,
        [
            "where is my order",
            "tracking",
            "when will",
            "status of my order",
            "hasn't shipped",
            "has not shipped",
            "cancel",
            "stornieren",
            "bestellung",
        ],
    ),
    (
        TriageCategory.billing,
        ["charged twice", "double charged", "credit card", "invoice", "billing"],
    ),
    (
        TriageCategory.product_question,
        [
            "size",
            "sizing",
            "fit",
            "wash",
            "waterproof",
            "warranty",
            "how do i",
            "temperature rating",
        ],
    ),
    (
        TriageCategory.complaint,
        ["disappointed", "unacceptable", "terrible", "worst", "furious", "ridiculous"],
    ),
]


def _usage(stage: str) -> StageUsage:
    return StageUsage(stage=stage, model="fake", latency_ms=1)


class FakeProvider:
    name = "fake"

    def __init__(self, settings: Settings) -> None:
        self.s = settings

    def triage(self, email: RedactedEmail) -> StageOutput[TriageResult]:
        text = f"{email.subject}\n{email.body_text}".lower()
        category, confidence = TriageCategory.other, 0.55
        for cat, kws in _CATEGORY_RULES:
            if any(k in text for k in kws):
                category, confidence = cat, 0.92
                break
        # "cancel" is order-change territory; keep it under order_status for the demo.
        sentiment = Sentiment.neutral
        if any(w in text for w in ("furious", "unacceptable", "worst", "ridiculous", "outraged")):
            sentiment = Sentiment.angry
        elif any(w in text for w in ("disappointed", "frustrat", "annoyed", "third time")):
            sentiment = Sentiment.frustrated
        elif any(w in text for w in ("thank", "love", "great")):
            sentiment = Sentiment.positive
        urgency = Urgency.normal
        if any(w in text for w in ("urgent", "asap", "tomorrow", "this weekend", "trip on")):
            urgency = Urgency.high
        if any(w in text for w in ("injur", "hospital", "caught fire", "exploded")):
            urgency = Urgency.critical
        language = "de" if re.search(r"\b(hallo|bitte|danke|bestellung|stornieren)\b", text) else "en"
        requires_human = None
        if any(w in text for w in ("my child", "my son", "my daughter", "injur", "hospital")):
            requires_human = "Mentions a minor or an injury."
        entities = ExtractedEntities(
            order_ids=sorted({m.upper() for m in _ORDER_RE.findall(text)}),
            amounts_usd=[float(m) for m in _MONEY_RE.findall(text)],
        )
        first_line = email.body_text.strip().splitlines()[0][:160] if email.body_text.strip() else ""
        result = TriageResult(
            category=category,
            urgency=urgency,
            sentiment=sentiment,
            language=language,
            summary=f"Customer wrote about '{email.subject}'. {first_line}",
            customer_ask=first_line or email.subject,
            entities=entities,
            requires_human_reason=requires_human,
            confidence=confidence,
        )
        return StageOutput(result, _usage("triage"))

    def research(
        self, email: RedactedEmail, triage: TriageResult, tools: ResearchTools
    ) -> StageOutput[ResearchBrief]:
        customer = json.loads(tools.dispatch("get_customer", {})[0])
        customer_context = (
            f"{customer.get('name')} ({customer.get('tier')} tier, customer since "
            f"{customer.get('customer_since')}, {customer.get('lifetime_orders')} orders). "
            f"Notes: {customer.get('notes') or 'none'}."
            if customer.get("found")
            else "No CRM record for this email address."
        )
        orders = []
        for oid in triage.entities.order_ids:
            o = json.loads(tools.dispatch("get_order", {"order_id": oid})[0])
            if o.get("found"):
                orders.append(o)
        if not orders and triage.category in (
            TriageCategory.order_status,
            TriageCategory.return_or_refund,
            TriageCategory.shipping_issue,
            TriageCategory.billing,
        ):
            listed = json.loads(tools.dispatch("list_customer_orders", {})[0])["orders"]
            if len(listed) == 1:
                o = json.loads(tools.dispatch("get_order", {"order_id": listed[0]["order_id"]})[0])
                if o.get("found"):
                    orders.append(o)

        findings: list[ResearchFinding] = []
        open_questions: list[str] = []
        if orders:
            o = orders[0]
            items = ", ".join(f"{i['name']} (${i['price_usd']:.2f})" for i in o["items"])
            order_context = (
                f"Order {o['order_id']} placed {o['placed_at']}, status {o['status']}, total "
                f"${o['total_usd']:.2f}. Items: {items}. Shipped {o.get('shipped_at') or 'not yet'}, "
                f"delivered {o.get('delivered_at') or 'not yet'}"
                + (f" ({o['days_since_delivery']} days ago)" if "days_since_delivery" in o else "")
                + f". Carrier {o.get('carrier') or 'n/a'}, tracking {o.get('tracking') or 'n/a'}."
            )
            findings.append(
                ResearchFinding(
                    claim=f"Order {o['order_id']} status is {o['status']}.",
                    source="get_order",
                    supporting_text=order_context,
                )
            )
            if any(i.get("final_sale") for i in o["items"]):
                findings.append(
                    ResearchFinding(
                        claim="The item is marked Final Sale.",
                        source="get_order",
                        supporting_text=items,
                    )
                )
        else:
            order_context = "No order found for the ids mentioned; customer has " + (
                f"{customer.get('lifetime_orders')} orders on file."
                if customer.get("found")
                else "no account."
            )
            if triage.category in (
                TriageCategory.return_or_refund,
                TriageCategory.shipping_issue,
                TriageCategory.order_status,
            ):
                open_questions.append("Which order number is this about?")

        query = {
            TriageCategory.return_or_refund: "return window refund original payment method",
            TriageCategory.shipping_issue: "damaged in transit late missing package replacement",
            TriageCategory.order_status: "processing time delivery estimates tracking cancelling an order",
            TriageCategory.account_access: "password reset locked account",
            TriageCategory.product_question: "sizing care wash warranty",
            TriageCategory.billing: "refund processing bank",
        }.get(triage.category, triage.summary)
        lowered = f"{email.subject} {email.body_text}".lower()
        if "cancel" in lowered or "stornieren" in lowered:
            query = "cancelling an order before it ships"
        if triage.category == TriageCategory.product_question:
            query = lowered
        hits = tools.kb.search(query, top_k=3)
        citations = [
            KBCitation(doc_id=c.doc_id, section=c.section, quote=" ".join(c.text.split()[:45]))
            for c, _ in hits
        ]
        tools.calls.append(
            type(tools.calls[0])(
                "search_knowledge_base", {"query": query}, json.dumps({"n": len(hits)}), True
            )
        )
        for c in citations:
            findings.append(
                ResearchFinding(
                    claim=f"Policy '{c.doc_id}' / {c.section} applies.",
                    source=c.doc_id,
                    supporting_text=c.quote,
                )
            )

        resolution = self._resolution(triage, orders, email)
        brief = ResearchBrief(
            customer_context=customer_context,
            order_context=order_context,
            relevant_policies=citations,
            findings=findings,
            open_questions=open_questions,
            recommended_resolution=resolution,
            tools_used=tools.names_used(),
        )
        return StageOutput(brief, _usage("research"))

    @staticmethod
    def _resolution(triage: TriageResult, orders: list[dict], email: RedactedEmail) -> str:
        text = f"{email.subject} {email.body_text}".lower()
        o = orders[0] if orders else None
        if triage.category == TriageCategory.return_or_refund and o:
            if any(i.get("final_sale") for i in o["items"]):
                return "Item is Final Sale and not returnable; explain politely, no refund."
            days = o.get("days_since_delivery")
            if days is None:
                return "Order not yet delivered; returns start after delivery."
            if days <= 30:
                return (
                    f"Within 30 days: full refund of ${o['total_usd']:.2f} to original payment "
                    "after return via prepaid label."
                )
            if days <= 60:
                return f"31-60 days after delivery: store credit of ${o['total_usd']:.2f} only."
            return "More than 60 days since delivery; not eligible for return."
        if triage.category == TriageCategory.shipping_issue and o:
            if "damaged" in text or "broken" in text:
                return (
                    f"Damaged in transit: offer replacement of {o['items'][0]['name']} "
                    f"(${o['total_usd']:.2f}) or full refund; ask for photos within 7 days."
                )
            return "Open a carrier trace if no movement for 5 business days."
        if triage.category == TriageCategory.order_status and o:
            if "cancel" in text or "stornieren" in text:
                return (
                    "Order has not shipped; cancellation is allowed."
                    if o["status"] == "processing"
                    else "Order already shipped; cannot cancel, offer return after delivery."
                )
            return f"Share status '{o['status']}' and tracking {o.get('tracking') or 'pending'}."
        if triage.category == TriageCategory.account_access:
            return "Send a password reset link; explain 30-minute validity."
        if triage.category == TriageCategory.product_question:
            return "Answer from the product-care / sizing article, quoting the relevant section."
        if triage.category == TriageCategory.billing:
            return "Confirm the charges with the payments team before promising anything; no action yet."
        if triage.category == TriageCategory.complaint:
            return "Apologize, ask for the order number, and hand to a human for follow-up."
        return "Acknowledge and route to a human; no automated resolution available."

    def draft(
        self, email: RedactedEmail, triage: TriageResult, brief: ResearchBrief
    ) -> StageOutput[DraftReply]:
        name = (email.from_name or "there").split(" ")[0]
        res = brief.recommended_resolution
        actions: list[ProposedAction] = []
        amount = None
        m = re.search(r"\$(\d+(?:\.\d{2})?)", res)
        if m:
            amount = float(m.group(1))
        oid = triage.entities.order_ids[0] if triage.entities.order_ids else None
        if res.startswith("Within 30 days") and amount:
            actions.append(
                ProposedAction(
                    type=ActionType.issue_refund,
                    order_id=oid,
                    amount_usd=amount,
                    rationale="returns-policy / Return window",
                )
            )
        elif res.startswith("Damaged in transit") and amount:
            actions.append(
                ProposedAction(
                    type=ActionType.replace_item,
                    order_id=oid,
                    amount_usd=amount,
                    rationale="shipping-policy / Damaged in transit",
                )
            )
        elif res.startswith("Order has not shipped"):
            actions.append(
                ProposedAction(
                    type=ActionType.cancel_order,
                    order_id=oid,
                    rationale="order-changes / Cancelling an order",
                )
            )
        elif res.startswith("Send a password reset"):
            actions.append(
                ProposedAction(
                    type=ActionType.reset_password_link,
                    rationale="account-and-login / Password reset",
                )
            )
        elif res.startswith("Acknowledge and route"):
            actions.append(
                ProposedAction(type=ActionType.escalate_to_human, rationale="No automated resolution.")
            )

        if triage.language == "de":
            greeting, closing = f"Hallo {name},", "Viele Grüße,"
        else:
            greeting, closing = f"Hi {name},", "Best regards,"
        lines = [
            greeting,
            "",
            f'Thanks for reaching out about "{email.subject.strip()}". '
            "I'm sorry for the trouble and I want to get this sorted quickly.",
            "",
        ]
        if brief.open_questions:
            lines += ["To help, could you send me: " + "; ".join(brief.open_questions), ""]
        else:
            lines += [res, ""]
            if brief.relevant_policies:
                lines += [f"For reference: {brief.relevant_policies[0].quote}", ""]
        lines += [
            "If anything here doesn't match what you're seeing, just reply to this email.",
            "",
            closing,
            self.s.agent_signature,
        ]
        body = "\n".join(lines)
        confidence = 0.9 if not brief.open_questions else 0.6
        if res.startswith(("Acknowledge", "Confirm the charges", "Apologize")):
            confidence = 0.4
        draft = DraftReply(
            subject=f"Re: {email.subject}",
            body=body,
            proposed_actions=actions,
            cited_doc_ids=sorted({c.doc_id for c in brief.relevant_policies}),
            confidence=confidence,
            notes_for_reviewer="Generated by the offline provider; heuristics only.",
        )
        return StageOutput(draft, _usage("draft"))

    def judge(
        self, email: RedactedEmail, brief: ResearchBrief, draft: DraftReply
    ) -> StageOutput[GroundingVerdict]:
        body = draft.body.lower()
        issues = []
        tone_ok = not any(p in body for p in ("our fault", "we are liable", "guarantee"))
        if not tone_ok:
            issues.append("Tone: contains a liability or guarantee phrase.")
        unsupported = []
        if "within 3 business days" in body and "3 business days" not in json.dumps(
            brief.model_dump(mode="json")
        ):
            unsupported.append("within 3 business days")
        score = 1.0 if not unsupported else 0.5
        verdict = GroundingVerdict(
            grounded=score == 1.0,
            score=score,
            unsupported_claims=unsupported,
            tone_ok=tone_ok,
            policy_conflicts=[],
            issues=issues,
        )
        return StageOutput(verdict, _usage("judge"))

"""User-turn builders shared by providers. Volatile content lives here, not in system prompts."""

from __future__ import annotations

import json

from ..models import DraftReply, RedactedEmail, ResearchBrief, TriageResult


def email_block(email: RedactedEmail) -> str:
    name = email.from_name or "(unknown)"
    return (
        "<customer_email>\n"
        f"From: {name} <{email.from_address}>\n"
        f"Received: {email.received_at.isoformat()}\n"
        f"Subject: {email.subject}\n\n"
        f"{email.body_text}\n"
        "</customer_email>"
    )


def triage_user(email: RedactedEmail) -> str:
    return email_block(email) + "\n\nTriage this email."


def research_user(email: RedactedEmail, triage: TriageResult) -> str:
    return (
        email_block(email)
        + "\n\n<triage>\n"
        + json.dumps(triage.model_dump(mode="json"), indent=2)
        + "\n</triage>\n\nInvestigate using the tools, then stop."
    )


def research_finalize() -> str:
    return "Now write the research brief as JSON, based only on what the tools returned."


def draft_user(email: RedactedEmail, triage: TriageResult, brief: ResearchBrief) -> str:
    return (
        email_block(email)
        + "\n\n<triage>\n"
        + json.dumps(triage.model_dump(mode="json"), indent=2)
        + "\n</triage>\n\n<research_brief>\n"
        + json.dumps(brief.model_dump(mode="json"), indent=2)
        + "\n</research_brief>\n\nWrite the reply as JSON."
    )


def judge_user(email: RedactedEmail, brief: ResearchBrief, draft: DraftReply) -> str:
    return (
        email_block(email)
        + "\n\n<research_brief>\n"
        + json.dumps(brief.model_dump(mode="json"), indent=2)
        + "\n</research_brief>\n\n<draft_reply>\n"
        + json.dumps(draft.model_dump(mode="json"), indent=2)
        + "\n</draft_reply>\n\nReview the draft and respond as JSON."
    )

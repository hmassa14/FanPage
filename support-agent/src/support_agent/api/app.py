"""Approval queue API + minimal UI, health, and Prometheus-style metrics.

Auth is a single shared admin token (bearer for JSON, HTTP Basic for the browser UI).
Replace with your SSO in production; the dependency is the only thing to swap.
"""

import hmac
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.security import (
    HTTPAuthorizationCredentials,
    HTTPBasic,
    HTTPBasicCredentials,
    HTTPBearer,
)
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from ..ingest.parse import parse_json
from ..models import TicketStatus
from ..pipeline.orchestrator import Pipeline

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
_bearer = HTTPBearer(auto_error=False)
_basic = HTTPBasic(auto_error=False)


class ApprovePayload(BaseModel):
    reviewer: str
    body: str | None = None
    subject: str | None = None
    note: str | None = None


class DecisionPayload(BaseModel):
    reviewer: str
    note: str | None = None


def create_app(pipeline: Pipeline) -> FastAPI:
    app = FastAPI(title="support-agent approval queue", version="0.1.0")
    token = pipeline.s.admin_token

    def require_auth(
        bearer: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
        basic: Annotated[HTTPBasicCredentials | None, Depends(_basic)],
    ) -> str:
        if bearer and hmac.compare_digest(bearer.credentials, token):
            return "api-token"
        if basic and hmac.compare_digest(basic.password, token):
            return basic.username or "reviewer"
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "unauthorized",
            headers={"WWW-Authenticate": "Basic realm=support-agent"},
        )

    Auth = Annotated[str, Depends(require_auth)]

    # ---- ops ----------------------------------------------------------------- #
    @app.get("/health")
    def health() -> dict[str, Any]:
        return {"ok": True, "provider": pipeline.provider.name, "policy": pipeline.policy.version}

    @app.get("/metrics", response_class=PlainTextResponse)
    def metrics() -> str:
        counts = pipeline.store.counts_by_status()
        usage = pipeline.store.usage_totals()
        lines = ["# TYPE support_agent_tickets gauge"]
        for st in TicketStatus:
            lines.append(f'support_agent_tickets{{status="{st.value}"}} {counts.get(st.value, 0)}')
        lines += [
            "# TYPE support_agent_llm_calls_total counter",
            f"support_agent_llm_calls_total {usage['llm_calls']}",
            "# TYPE support_agent_llm_tokens_total counter",
            f'support_agent_llm_tokens_total{{kind="input"}} {usage["input_tokens"]}',
            f'support_agent_llm_tokens_total{{kind="output"}} {usage["output_tokens"]}',
            f'support_agent_llm_tokens_total{{kind="cache_read"}} {usage["cache_read_tokens"]}',
            "# TYPE support_agent_llm_cost_usd_total counter",
            f"support_agent_llm_cost_usd_total {usage['cost_usd']}",
        ]
        return "\n".join(lines) + "\n"

    # ---- JSON API ------------------------------------------------------------- #
    @app.post("/api/ingest", status_code=202)
    def ingest(payload: dict[str, Any], _: Auth) -> dict[str, Any]:
        t = pipeline.process_email(parse_json(payload))
        return {
            "ticket_id": t.id,
            "status": t.status.value,
            "decision": t.gate.decision.value if t.gate else None,
        }

    @app.get("/api/tickets")
    def list_tickets(_: Auth, status_filter: str | None = None) -> list[dict[str, Any]]:
        st = TicketStatus(status_filter) if status_filter else None
        return [_brief(t) for t in pipeline.store.list_tickets(st)]

    @app.get("/api/tickets/{ticket_id}")
    def get_ticket(ticket_id: str, _: Auth) -> dict[str, Any]:
        t = pipeline.store.get(ticket_id)
        if not t:
            raise HTTPException(404, "no such ticket")
        return {**t.model_dump(mode="json"), "events": pipeline.store.events(ticket_id)}

    @app.post("/api/tickets/{ticket_id}/approve")
    def api_approve(ticket_id: str, p: ApprovePayload, _: Auth) -> dict[str, Any]:
        return _brief(_guard(lambda: pipeline.approve(ticket_id, p.reviewer, p.body, p.subject, p.note)))

    @app.post("/api/tickets/{ticket_id}/reject")
    def api_reject(ticket_id: str, p: DecisionPayload, _: Auth) -> dict[str, Any]:
        return _brief(_guard(lambda: pipeline.reject(ticket_id, p.reviewer, p.note)))

    @app.post("/api/tickets/{ticket_id}/escalate")
    def api_escalate(ticket_id: str, p: DecisionPayload, _: Auth) -> dict[str, Any]:
        return _brief(_guard(lambda: pipeline.escalate(ticket_id, p.reviewer, p.note)))

    # ---- HTML UI ------------------------------------------------------------- #
    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request, user: Auth) -> HTMLResponse:
        tickets = pipeline.store.list_tickets(limit=200)
        pending = [t for t in tickets if t.status == TicketStatus.awaiting_approval]
        return TEMPLATES.TemplateResponse(
            request,
            "dashboard.html",
            {
                "tickets": tickets,
                "pending": pending,
                "counts": pipeline.store.counts_by_status(),
                "usage": pipeline.store.usage_totals(),
                "user": user,
                "policy": pipeline.policy.version,
                "provider": pipeline.provider.name,
            },
        )

    @app.get("/tickets/{ticket_id}", response_class=HTMLResponse)
    def ticket_page(request: Request, ticket_id: str, user: Auth) -> HTMLResponse:
        t = pipeline.store.get(ticket_id)
        if not t:
            raise HTTPException(404, "no such ticket")
        return TEMPLATES.TemplateResponse(
            request,
            "ticket.html",
            {
                "t": t,
                "events": pipeline.store.events(ticket_id),
                "user": user,
                "outbox": pipeline.store.outbox_for(ticket_id),
            },
        )

    @app.post("/tickets/{ticket_id}/decide")
    def ui_decide(
        ticket_id: str,
        user: Auth,
        action: Annotated[str, Form()],
        subject: Annotated[str, Form()] = "",
        body: Annotated[str, Form()] = "",
        note: Annotated[str, Form()] = "",
    ) -> RedirectResponse:
        if action == "approve":
            _guard(lambda: pipeline.approve(ticket_id, user, body or None, subject or None, note or None))
        elif action == "reject":
            _guard(lambda: pipeline.reject(ticket_id, user, note or None))
        elif action == "escalate":
            _guard(lambda: pipeline.escalate(ticket_id, user, note or None))
        else:
            raise HTTPException(400, "unknown action")
        return RedirectResponse(f"/tickets/{ticket_id}", status_code=303)

    return app


def _guard(fn):
    try:
        return fn()
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


def _brief(t) -> dict[str, Any]:
    return {
        "ticket_id": t.id,
        "status": t.status.value,
        "from": t.email.from_address,
        "subject": t.email.subject,
        "category": t.triage.category.value if t.triage else None,
        "decision": t.gate.decision.value if t.gate else None,
        "cost_usd": t.total_cost_usd,
        "created_at": t.created_at.isoformat(),
    }

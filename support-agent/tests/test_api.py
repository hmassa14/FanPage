import json

import pytest
from fastapi.testclient import TestClient

from support_agent.api.app import create_app

from .conftest import SAMPLES

H = {"Authorization": "Bearer test-token"}


@pytest.fixture
def client(pipeline):
    return TestClient(create_app(pipeline))


def test_health_and_metrics_are_public(client):
    assert client.get("/health").json()["ok"] is True
    assert "support_agent_tickets" in client.get("/metrics").text


def test_api_requires_token(client):
    assert client.get("/api/tickets").status_code == 401
    assert client.get("/").status_code == 401


def test_ingest_review_approve_flow(client):
    payload = json.loads(next(SAMPLES.glob("02*.json")).read_text())
    r = client.post("/api/ingest", json=payload, headers=H)
    assert r.status_code == 202 and r.json()["decision"] == "needs_approval"
    tid = r.json()["ticket_id"]
    assert client.get("/", auth=("haley", "test-token")).status_code == 200
    page = client.get(f"/tickets/{tid}", auth=("haley", "test-token"))
    assert page.status_code == 200 and "Approve" in page.text
    r = client.post(
        f"/tickets/{tid}/decide",
        data={"action": "approve", "note": "fine"},
        auth=("haley", "test-token"),
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert client.get(f"/api/tickets/{tid}", headers=H).json()["status"] == "sent"
    # a second decision on a closed ticket is a conflict, not a silent no-op
    r = client.post(f"/api/tickets/{tid}/reject", json={"reviewer": "x"}, headers=H)
    assert r.status_code == 409


def test_unknown_ticket_404(client):
    assert client.get("/api/tickets/nope", headers=H).status_code == 404

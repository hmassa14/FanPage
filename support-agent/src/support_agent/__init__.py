"""support_agent: a production-shaped customer-support email agent.

Pipeline: ingest -> redact -> triage -> research -> draft -> judge -> policy gates
-> (auto-send | human approval | escalate | reject) -> outbox -> send.
"""

__version__ = "0.1.0"

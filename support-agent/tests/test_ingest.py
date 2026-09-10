from support_agent.ingest.parse import parse_eml

RAW = b"""From: Maya Chen <maya.chen@example.com>
To: support@northwind-outfitters.example
Subject: Return request
Message-ID: <abc@example.com>
Date: Wed, 10 Sep 2026 14:02:00 +0000
Content-Type: text/plain; charset="utf-8"

Hi, I want to return NW-10042.
"""


def test_parse_eml_plain():
    e = parse_eml(RAW)
    assert e.from_address == "maya.chen@example.com" and e.from_name == "Maya Chen"
    assert e.message_id == "<abc@example.com>" and "NW-10042" in e.body_text
    assert e.received_at.year == 2026
    assert e.idempotency_key() == parse_eml(RAW).idempotency_key()

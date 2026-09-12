import json

from tools import REGISTRY, TOOL_DEFINITIONS, execute


def test_every_definition_is_strict_compatible():
    for d in TOOL_DEFINITIONS:
        schema = d["input_schema"]
        assert d["strict"] is True
        assert schema["additionalProperties"] is False, d["name"]
        # strict mode requires every property to be listed in `required`
        assert set(schema["required"]) == set(schema["properties"]), d["name"]
        assert d["description"]


def test_registry_and_definitions_agree():
    assert [d["name"] for d in TOOL_DEFINITIONS] == list(REGISTRY)


def test_lookup_customer_round_trips_json():
    out = execute("lookup_customer", {"customer_id": "cust_001"})
    assert not out.is_error
    rec = json.loads(out.content)
    assert rec["plan"] == "enterprise" and rec["renewal_in_days"] == 22


def test_unknown_customer_is_recoverable_error():
    out = execute("lookup_customer", {"customer_id": "cust_999"})
    assert out.is_error and "cust_999" in out.content


def test_invalid_input_is_error_not_exception():
    out = execute("lookup_customer", {"customer_id": 5, "extra": "x"})
    assert out.is_error and "Invalid input" in out.content


def test_unknown_tool_is_error():
    out = execute("delete_customer", {})
    assert out.is_error and out.content.startswith("Unknown tool")


def test_search_incidents_matches_keywords():
    hit = json.loads(execute("search_incidents", {"query": "csv export timing out"}).content)
    assert [m["incident_id"] for m in hit["matches"]] == ["INC-2291"]
    miss = json.loads(execute("search_incidents", {"query": "dark mode theme"}).content)
    assert miss["matches"] == []

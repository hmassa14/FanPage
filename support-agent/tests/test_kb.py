from support_agent.config import PROJECT_ROOT
from support_agent.knowledge.kb import KnowledgeBase


def test_bm25_finds_the_right_section():
    kb = KnowledgeBase(PROJECT_ROOT / "data" / "kb")
    top, _ = kb.search("return window after delivery refund")[0]
    assert top.doc_id == "returns-policy" and top.section == "Return window"
    top, _ = kb.search("password reset link valid")[0]
    assert top.doc_id == "account-and-login"


def test_doc_ids_and_get_doc():
    kb = KnowledgeBase(PROJECT_ROOT / "data" / "kb")
    assert "escalation-sop" in kb.doc_ids()
    assert len(kb.get_doc("shipping-policy")) >= 5

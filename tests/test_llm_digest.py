import os, tempfile
from autocompactor import llm_digest


def test_openai_url_normalizes():
    assert llm_digest._openai_url("http://h/v1") == "http://h/v1/chat/completions"
    assert llm_digest._openai_url("http://h/v1/chat/completions") == "http://h/v1/chat/completions"
    assert llm_digest._openai_url("http://h") == "http://h/v1/chat/completions"


def test_llm_digest_disabled_returns_empty(monkeypatch):
    # No provider configured + unreadable path -> never raises, returns "".
    monkeypatch.delenv("AUTOCOMPACTOR_LLM_CMD", raising=False)
    assert llm_digest.llm_digest("/nonexistent/path.jsonl") == ""


def test_pi_bridge_imports_llm_digest_from_new_module():
    import autocompactor.pi_bridge as pb
    assert pb.llm_digest.__module__ == "autocompactor.llm_digest"

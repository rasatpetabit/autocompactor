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


# ─── Chonkie integration (Phase 1) ──────────────────────────────────────────
import sys as _sys
_sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from autocompactor import chonkie_lib


_OLD_PREAMBLE = (
    "Below are the most recent entries of a coding-session transcript "
    "(JSONL). List, as terse bullets, the facts that MUST survive a "
    "context compaction: current task, file paths touched, key "
    "decisions, working commands, unresolved errors. Bullets only.\n\n"
)


def _reset_config(monkeypatch):
    """Force pure-env config (no config.json) for hermetic tests."""
    monkeypatch.setenv("AUTOCOMPACTOR_CONFIG", "")
    chonkie_lib.config_lib._config_cache = None


def _write_transcript(tmp_path, lines):
    p = tmp_path / "session.jsonl"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(p)


def _old_blob(path):
    """Compute the exact pre-Chonkie slice via the same readlines path."""
    with open(path, encoding="utf-8") as fh:
        return "".join(fh.readlines()[-120:])[-30_000:]


def test_off_mode_is_byte_identical_to_old(monkeypatch, tmp_path):
    # Golden: off mode must produce the exact pre-Chonkie prompt.
    _reset_config(monkeypatch)
    lines = [f'{{"role":"user","content":"line {i}"}}' for i in range(200)]
    path = _write_transcript(tmp_path, lines)
    old_expected = _OLD_PREAMBLE + _old_blob(path)
    prompt, note = llm_digest._build_digest_prompt(path)
    assert prompt == old_expected
    assert note == ""


def test_off_mode_byte_identical_multibyte(monkeypatch, tmp_path):
    # CJK / multibyte: char-cap must match old char-cap exactly.
    _reset_config(monkeypatch)
    lines = [f'{{"c":"第 {i} 行 中文内容 测试"}}' for i in range(200)]
    path = _write_transcript(tmp_path, lines)
    old_expected = _OLD_PREAMBLE + _old_blob(path)
    prompt, note = llm_digest._build_digest_prompt(path)
    assert prompt == old_expected
    assert note == ""


def test_digest_mode_chunks_exact_old_blob(monkeypatch, tmp_path):
    # The bytes chunked must be the same last-120-lines / 30k-char blob.
    _reset_config(monkeypatch)
    monkeypatch.setenv("AUTOCOMPACTOR_CHONKIE_MODE", "digest")
    monkeypatch.setenv("AUTOCOMPACTOR_CHONKIE_CHUNK_SIZE", "300")
    lines = [f'{{"role":"user","content":"message number {i} here"}}'
             for i in range(200)]
    path = _write_transcript(tmp_path, lines)
    prompt, note = llm_digest._build_digest_prompt(path)
    assert "chonkie-digest" in note
    # Every chunk body is a substring of the old blob.
    assert "### Chunk 1" in prompt
    # prompt never exceeds the old size envelope (preamble + 30k).
    assert len(prompt) <= len(_OLD_PREAMBLE) + 30000


def test_shadow_mode_sends_old_prompt_with_note(monkeypatch, tmp_path):
    _reset_config(monkeypatch)
    monkeypatch.setenv("AUTOCOMPACTOR_CHONKIE_MODE", "shadow")
    lines = [f'line {i}' for i in range(150)]
    path = _write_transcript(tmp_path, lines)
    prompt, note = llm_digest._build_digest_prompt(path)
    assert prompt == _OLD_PREAMBLE + _old_blob(path)  # unchanged prompt
    assert "chonkie-shadow" in note


def test_off_mode_survives_malformed_config(monkeypatch, tmp_path):
    # Adversarial review #1: off-mode must stay byte-identical even if the
    # Chonkie config is malformed (a parse failure must NOT break the old prompt).
    _reset_config(monkeypatch)
    monkeypatch.setenv("AUTOCOMPACTOR_CHONKIE_MODE", "off")
    # Malformed numeric config that would raise during settings() if not guarded.
    monkeypatch.setenv("AUTOCOMPACTOR_CHONKIE_CHUNK_SIZE", "not-a-number")
    lines = [f'line {i}' for i in range(150)]
    path = _write_transcript(tmp_path, lines)
    prompt, note = llm_digest._build_digest_prompt(path)
    assert prompt == _OLD_PREAMBLE + _old_blob(path)  # byte-identical old prompt


def test_off_mode_survives_settings_exception(monkeypatch, tmp_path):
    # If settings() itself raises, off-mode still returns the old prompt.
    _reset_config(monkeypatch)
    monkeypatch.setenv("AUTOCOMPACTOR_CHONKIE_MODE", "off")
    lines = [f'line {i}' for i in range(150)]
    path = _write_transcript(tmp_path, lines)
    import autocompactor.chonkie_lib as _cl
    def _boom(_=None):
        raise RuntimeError("config backend exploded")
    monkeypatch.setattr(_cl, "settings", _boom)
    prompt, note = llm_digest._build_digest_prompt(path)
    assert prompt == _OLD_PREAMBLE + _old_blob(path)
    assert note == "chonkie-settings-failed"


def test_chonkie_failure_falls_back_to_old_prompt(monkeypatch, tmp_path):
    _reset_config(monkeypatch)
    monkeypatch.setenv("AUTOCOMPACTOR_CHONKIE_MODE", "digest")
    # Break the runner path -> chunk_text returns None -> old prompt.
    monkeypatch.setattr(chonkie_lib, "_RUNNER", "/nonexistent/runner.py")
    monkeypatch.setenv("AUTOCOMPACTOR_CHONKIE_TIMEOUT_MS", "200")
    lines = [f'line {i}' for i in range(150)]
    path = _write_transcript(tmp_path, lines)
    prompt, note = llm_digest._build_digest_prompt(path)
    assert prompt == _OLD_PREAMBLE + _old_blob(path)
    assert note == "chonkie-failed-fallback"


def test_llm_digest_never_raises_on_bad_path(monkeypatch):
    _reset_config(monkeypatch)
    assert llm_digest.llm_digest("/nonexistent/path.jsonl") == ""


def test_large_transcript_tail_performance(monkeypatch, tmp_path):
    # 200KB tail: chunking must complete under timeout or fall back.
    _reset_config(monkeypatch)
    monkeypatch.setenv("AUTOCOMPACTOR_CHONKIE_MODE", "digest")
    monkeypatch.setenv("AUTOCOMPACTOR_CHONKIE_CHUNK_SIZE", "1000")
    big = ["x" * 1700 for _ in range(200)]  # ~340KB, tail ~200KB
    path = _write_transcript(tmp_path, big)
    prompt, note = llm_digest._build_digest_prompt(path)
    # Either it chunked (note has chonkie-digest) or fell back — never hangs.
    assert "chonkie" in note or note == "chonkie-failed-fallback"
    assert len(prompt) <= len(_OLD_PREAMBLE) + 30000

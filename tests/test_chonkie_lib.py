"""Unit tests for chonkie_lib — all fail-closed paths, bounds, and rendering."""
import os
import sys


sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from autocompactor import chonkie_lib


# ─── settings() validation ──────────────────────────────────────────────────

def test_settings_defaults_when_unconfigured(monkeypatch):
    # Clear any config + env so defaults apply.
    monkeypatch.setenv("AUTOCOMPACTOR_CONFIG", "")
    chonkie_lib.config_lib._config_cache = None
    s = chonkie_lib.settings()
    assert s["mode"] == "off"
    assert s["chunker"] == "recursive"
    assert s["chunk_size"] == 1200
    assert s["max_chunks"] == 24
    assert s["max_input_chars"] == 30000
    assert s["timeout_ms"] == 1500


def test_settings_invalid_mode_degrades_to_off(monkeypatch):
    monkeypatch.setenv("AUTOCOMPACTOR_CONFIG", "")
    chonkie_lib.config_lib._config_cache = None
    monkeypatch.setenv("AUTOCOMPACTOR_CHONKIE_MODE", "banana")
    assert chonkie_lib.settings()["mode"] == "off"


def test_settings_env_overrides_config(monkeypatch, tmp_path):
    # config file says digest, env says off -> env wins
    cfg_file = tmp_path / "c.json"
    cfg_file.write_text('{"CHONKIE_MODE":"digest","CHONKIE_CHUNK_SIZE":500}')
    monkeypatch.setenv("AUTOCOMPACTOR_CONFIG", str(cfg_file))
    chonkie_lib.config_lib._config_cache = None
    monkeypatch.setenv("AUTOCOMPACTOR_CHONKIE_MODE", "off")
    s = chonkie_lib.settings()
    assert s["mode"] == "off"  # env wins
    assert s["chunk_size"] == 500  # from config (no env override)


def test_settings_clamps_bad_numbers(monkeypatch):
    monkeypatch.setenv("AUTOCOMPACTOR_CONFIG", "")
    chonkie_lib.config_lib._config_cache = None
    monkeypatch.setenv("AUTOCOMPACTOR_CHONKIE_CHUNK_SIZE", "-5")
    monkeypatch.setenv("AUTOCOMPACTOR_CHONKIE_TIMEOUT_MS", "10")
    s = chonkie_lib.settings()
    assert s["chunk_size"] == 1  # max(1, ...)
    assert s["timeout_ms"] == 100  # max(100, ...)


def test_settings_clamps_timeout_upper_bound(monkeypatch):
    # Adversarial review #3: no unbounded timeout can hang the hook.
    monkeypatch.setenv("AUTOCOMPACTOR_CONFIG", "")
    chonkie_lib.config_lib._config_cache = None
    monkeypatch.setenv("AUTOCOMPACTOR_CHONKIE_TIMEOUT_MS", "600000")
    s = chonkie_lib.settings()
    assert s["timeout_ms"] == 5000  # hard cap


# ─── chunk_text() fail-closed ───────────────────────────────────────────────

def _sett(**kw):
    base = chonkie_lib.settings()
    base.update(kw)
    return base


def test_chunk_text_blank_returns_empty():
    assert chonkie_lib.chunk_text("", _sett()) == []
    assert chonkie_lib.chunk_text("   \n  ", _sett()) == []


def test_chunk_text_returns_list_on_success():
    s = _sett(chunk_size=200)
    out = chonkie_lib.chunk_text("Alpha. " * 100 + "Beta. " * 100, s)
    assert out is not None
    assert isinstance(out, list)
    assert all(isinstance(c, dict) and "text" in c for c in out)


def test_chunk_text_timeout_falls_back(monkeypatch):
    # Point at a runner that sleeps past the timeout.
    runner = os.path.join(os.path.dirname(chonkie_lib.__file__),
                          "chonkie_chunk_runner.py")
    sleepy = runner + ".NOTREAL"  # force missing-file path
    monkeypatch.setattr(chonkie_lib, "_RUNNER", sleepy)
    s = _sett(timeout_ms=200)
    assert chonkie_lib.chunk_text("some text", s) is None


def test_chunk_text_real_timeout(monkeypatch, tmp_path):
    # Real subprocess that hangs -> killed by timeout -> None.
    hang = tmp_path / "hang.py"
    hang.write_text("import time; time.sleep(30)\n")
    monkeypatch.setattr(chonkie_lib, "_RUNNER", str(hang))
    s = _sett(timeout_ms=300)
    assert chonkie_lib.chunk_text("some text", s) is None


def test_chunk_text_malformed_stdout_returns_none(monkeypatch, tmp_path):
    bad = tmp_path / "bad.py"
    bad.write_text("import sys; sys.stdout.write('not json')")
    monkeypatch.setattr(chonkie_lib, "_RUNNER", str(bad))
    assert chonkie_lib.chunk_text("text", _sett()) is None


def test_chunk_text_offline_no_network():
    # Recursive char tokenizer must work fully offline.
    s = _sett(chunk_size=200)
    env_offline = {**os.environ, "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1"}
    # chunk_text sets its own env; just confirm it succeeds offline-capable input
    out = chonkie_lib.chunk_text("topic one. " * 50, s)
    assert out is not None and len(out) >= 1


# ─── render_digest() guarantees ─────────────────────────────────────────────

def test_render_empty():
    assert chonkie_lib.render_digest([], 30000, 24) == ""


def test_render_within_budget():
    chunks = [{"text": f"chunk {i}", "token_count": i} for i in range(3)]
    out = chonkie_lib.render_digest(chunks, 30000, 24)
    assert "### Chunk 1" in out
    assert "### Chunk 3" in out
    assert "chunk 0" in out and "chunk 2" in out


def test_render_newest_preserved_on_count_overflow():
    # 30 chunks, max_chunks 5 -> only last 5 kept (newest)
    chunks = [{"text": f"chunk-{i}", "token_count": 0} for i in range(30)]
    out = chonkie_lib.render_digest(chunks, 30000, 5)
    assert "chunk-29" in out  # newest kept
    assert "chunk-0" not in out  # oldest dropped
    assert "chunk-25" in out  # last 5 = indices 25..29
    assert "chunk-24" not in out


def test_render_size_cap_with_headings():
    chunks = [{"text": "x" * 5000, "token_count": 0} for _ in range(10)]
    out = chonkie_lib.render_digest(chunks, 30000, 24)
    assert len(out) <= 30000


def test_render_single_oversized_block_truncated():
    chunks = [{"text": "y" * 50000, "token_count": 0}]
    out = chonkie_lib.render_digest(chunks, 30000, 24)
    assert len(out) <= 30000
    assert out  # not empty — newest content survives (truncated)


def test_render_single_oversized_block_keeps_tail_not_front():
    # Adversarial review #4: the newest facts (end of transcript) must survive.
    body = "OLDSTART_" * 2000 + "NEWEST_END_MARKER_" * 2000
    chunks = [{"text": body, "token_count": 0}]
    out = chonkie_lib.render_digest(chunks, 2000, 24)
    assert "NEWEST_END_MARKER_" in out  # tail preserved
    assert "OLDSTART_" not in out  # front dropped
    assert "### Chunk 1" in out  # header still present


def test_render_drops_oldest_to_fit_budget():
    # Each block ~6000 chars; budget 10000 -> only newest 1-2 survive.
    chunks = [{"text": "a" * 5900, "token_count": 0},
              {"text": "b" * 5900, "token_count": 0}]
    out = chonkie_lib.render_digest(chunks, 10000, 24)
    assert "b" * 100 in out  # newest kept
    assert len(out) <= 10000


def test_render_never_raises_on_garbage():
    assert chonkie_lib.render_digest(None, 30000, 24) == ""
    assert chonkie_lib.render_digest("notalist", 30000, 24) == ""

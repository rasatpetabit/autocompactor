#!/usr/bin/env python3
"""chonkie_lib.py — optional Chonkie chunking for the llm_digest transcript tail.

FAIL-CLOSED by construction. Every public function returns a safe sentinel
(None, "", []) on ANY failure (import error, subprocess timeout, malformed
input) so llm_digest falls back to the exact pre-Chonkie prompt. Chonkie never
runs in-process: chunking is isolated in a subprocess (chonkie_chunk_runner.py)
so a stuck import or C-call cannot hang the compaction hook.

Config (standard autocompactor precedence: AUTOCOMPACTOR_* env >
config.local.json > config.json > default):
  CHONKIE_MODE         off|shadow|digest   (default off)
  CHONKIE_CHUNKER      recursive           (default recursive; no-network)
  CHONKIE_CHUNK_SIZE   1200                (characters; recursive char tokenizer)
  CHONKIE_MAX_CHUNKS   24
  CHONKIE_MAX_INPUT_CHARS 30000
  CHONKIE_TIMEOUT_MS   1500
"""
import json
import os
import subprocess
import sys

from autocompactor import config_lib

_RUNNER = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "chonkie_chunk_runner.py")


def settings():
    """Read + validate Chonkie config. Invalid values degrade to safe defaults."""
    cfg = config_lib.cfg
    mode = str(cfg.str("CHONKIE_MODE", default="off") or "off").strip().lower()
    if mode not in ("off", "shadow", "digest"):
        mode = "off"  # invalid -> safe default (never enables silently)
    chunker = str(cfg.str("CHONKIE_CHUNKER", default="recursive")
                  or "recursive").strip().lower() or "recursive"
    return {
        "mode": mode,
        "chunker": chunker,
        "chunk_size": max(1, int(cfg.float("CHONKIE_CHUNK_SIZE", default=1200))),
        "max_chunks": max(1, int(cfg.float("CHONKIE_MAX_CHUNKS", default=24))),
        "max_input_chars": max(1, int(cfg.float("CHONKIE_MAX_INPUT_CHARS",
                                                 default=30000))),
        "timeout_ms": min(max(100, int(cfg.float("CHONKIE_TIMEOUT_MS", default=1500))), 5000),
    }


def chunk_text(text, _settings=None):
    """Chunk `text` via the isolated subprocess runner.

    Returns a list of {"text","token_count"} dicts, or None on ANY failure
    (import error, timeout, malformed stdout). Empty/blank input -> [].
    Never raises.
    """
    try:
        if _settings is None:
            _settings = settings()
        if not text or not text.strip():
            return []
        if not os.path.isfile(_RUNNER):
            return None
        env = dict(os.environ)
        # Unconditional: a parent env with these set to "0" must NOT put the
        # compaction hook back online. (Adversarial review #5.)
        for k in ("HF_HUB_DISABLE_PROGRESS_BARS", "HF_HUB_DISABLE_TELEMETRY",
                  "HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE"):
            env[k] = "1"
        env["TRANSFORMERS_VERBOSITY"] = "error"
        env["TOKENIZERS_PARALLELISM"] = "false"
        proc = subprocess.run(
            [sys.executable, _RUNNER,
             "--chunker", _settings["chunker"],
             "--chunk-size", str(_settings["chunk_size"])],
            input=text,
            capture_output=True,
            text=True,
            encoding="utf-8",  # locale-independent (review #6): CJK-safe
            timeout=_settings["timeout_ms"] / 1000.0,
            env=env,
        )
        if proc.returncode != 0:
            return None
        chunks = json.loads(proc.stdout)
        if not isinstance(chunks, list):
            return None
        # Validate shape.
        for c in chunks:
            if not isinstance(c, dict) or "text" not in c:
                return None
        return chunks
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError, ValueError):
        return None
    except Exception:
        return None


def render_digest(chunks, max_chars, max_chunks):
    """Render chunks as '### Chunk N (~T tokens)' markdown sections.

    Guarantees (adversarial review):
      * Newest-preserved: when chunks exceed max_chunks, oldest are dropped.
      * Size cap: output <= max_chars (headings count). If a single block
        exceeds the cap it is truncated so the freshest content always lands.
      * Never raises.
    `chunks` is chronological (oldest first).
    """
    try:
        if not isinstance(chunks, list) or not chunks:
            return ""
        # 1. Newest-preserved truncation of the chunk count.
        sel = chunks[-max_chunks:] if len(chunks) > max_chunks else list(chunks)
        # 2. Render blocks in chronological order with sequential numbering.
        blocks = []
        for idx, c in enumerate(sel):
            body = c.get("text", "") if isinstance(c, dict) else str(c)
            tokens = c.get("token_count", 0) if isinstance(c, dict) else 0
            blocks.append(f"### Chunk {idx + 1} (~{int(tokens)} tokens)\n\n{body}")
        # 3. Fit under max_chars by dropping OLDEST blocks (front) first.
        #    Always keep at least the newest block.
        while len(blocks) > 1 and len("\n\n".join(blocks)) > max_chars:
            blocks.pop(0)
        rendered = "\n\n".join(blocks)
        # 4. Single oversized block: keep its HEADER + the TAIL of the body, so
        #    the most recent transcript content (end of chunk) survives the cap,
        #    not the beginning. (Adversarial review #4.)
        if len(rendered) > max_chars:
            header, sep, body = rendered.partition("\n\n")
            if len(header) + 2 < max_chars:
                keep = max_chars - len(header) - 2
                rendered = header + sep + body[-keep:]
            else:
                rendered = rendered[-max_chars:]
        return rendered.strip()
    except Exception:
        return ""

#!/usr/bin/env python3
"""chonkie_chunk_runner.py — isolated subprocess runner for Chonkie chunking.

Reads text from stdin, writes a JSON array of {"text","token_count"} to stdout,
nothing else. The parent (chonkie_lib.chunk_text) spawns this so a stuck
import or C-call can be hard-killed via subprocess timeout — the compaction
hook can never hang on Chonkie.

Design constraints (from adversarial review):
  * Zero network: uses RecursiveChunker with the DEFAULT 'character' tokenizer
    (no gpt2/HF download). HF_HUB_OFFLINE set defensively.
  * stdout purity: every HuggingFace/tqdm/transformers channel is silenced at
    process start; only the JSON array is written to stdout.
  * Fail loud to the parent: any exception exits non-zero; the parent treats
    non-zero (and timeout, and malformed stdout) as "fall back to old prompt".
"""
import json
import os
import sys

# Silence every noisy upstream channel BEFORE any chonkie import.
for _k in ("HF_HUB_DISABLE_PROGRESS_BARS", "HF_HUB_DISABLE_TELEMETRY",
           "TRANSFORMERS_OFFLINE", "HF_HUB_OFFLINE"):
    os.environ.setdefault(_k, "1")
os.environ.setdefault("TRANSFORMERS_VERBOSITY", "error")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")


def _parse(argv):
    chunker = "recursive"
    chunk_size = 1200
    i = 0
    while i < len(argv):
        if argv[i] == "--chunker" and i + 1 < len(argv):
            chunker = argv[i + 1]
            i += 2
        elif argv[i] == "--chunk-size" and i + 1 < len(argv):
            try:
                chunk_size = int(argv[i + 1])
            except ValueError:
                pass
            i += 2
        else:
            i += 1
    if chunk_size < 1:
        chunk_size = 1200
    return chunker, chunk_size


def _make_chunker(name, chunk_size):
    """Build a chunker that needs NO network. Defaults to recursive (character
    tokenizer). Unknown names degrade to recursive rather than failing."""
    from chonkie import RecursiveChunker
    if name in ("token", "sentence"):
        # These tokenizers would need a download; degrade to recursive to keep
        # the no-network guarantee. (Phase 1 deliberately excludes them.)
        return RecursiveChunker(chunk_size=chunk_size)
    if name == "fast":
        try:
            from chonkie import FastChunker
            return FastChunker(chunk_size=chunk_size)
        except Exception:
            return RecursiveChunker(chunk_size=chunk_size)
    return RecursiveChunker(chunk_size=chunk_size)


def main(argv):
    chunker_name, chunk_size = _parse(argv)
    text = sys.stdin.read()
    if not text.strip():
        sys.stdout.write("[]")
        return 0
    chunker = _make_chunker(chunker_name, chunk_size)
    chunks = chunker(text)
    out = []
    for c in chunks:
        out.append({
            "text": getattr(c, "text", str(c)),
            "token_count": int(getattr(c, "token_count", 0) or 0),
        })
    sys.stdout.write(json.dumps(out))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except Exception:
        # Non-zero exit -> parent falls back to the old prompt (fail closed).
        sys.exit(1)

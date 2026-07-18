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


def _fallback_recursive_chunks(text, chunk_size):
    """Pure-Python character recursive split (no chonkie install required).

    Mirrors the no-network RecursiveChunker(character) intent: prefer paragraph
    / line / sentence boundaries, hard-cut at chunk_size. Returns list of
    {"text", "token_count"} where token_count ≈ character length (char tokenizer).
    """
    chunk_size = max(1, int(chunk_size))
    text = text or ""
    if not text:
        return []

    # Prefer splitting on strong boundaries first, then re-pack to chunk_size.
    parts = []
    for para in text.split("\n\n"):
        if not para:
            parts.append("\n\n")
            continue
        if len(para) <= chunk_size:
            parts.append(para)
            continue
        for line in para.split("\n"):
            if len(line) <= chunk_size:
                parts.append(line)
                continue
            # sentence-ish then hard cut
            buf = line
            while buf:
                if len(buf) <= chunk_size:
                    parts.append(buf)
                    break
                window = buf[:chunk_size]
                cut = max(window.rfind(". "), window.rfind("; "),
                          window.rfind(", "), window.rfind(" "))
                if cut < chunk_size // 4:
                    cut = chunk_size
                else:
                    cut = cut + 1  # keep delimiter with left piece
                parts.append(buf[:cut])
                buf = buf[cut:]

    # Pack parts into <= chunk_size chunks (join with single spaces/newlines
    # already present in parts).
    out = []
    cur = ""
    for p in parts:
        if not p:
            continue
        if not cur:
            cur = p
            continue
        sep = "\n\n" if (not cur.endswith("\n") and "\n" not in p[:1]) else ""
        # Prefer blank-line join when both sides look like paragraphs.
        if p.startswith("\n") or cur.endswith("\n"):
            candidate = cur + p
        else:
            candidate = cur + ("\n" if "\n" in cur or "\n" in p else " ") + p
        if len(candidate) <= chunk_size:
            cur = candidate
        else:
            out.append(cur)
            cur = p
    if cur:
        out.append(cur)

    # Final hard-cut any oversized piece (safety).
    final = []
    for piece in out:
        while len(piece) > chunk_size:
            final.append(piece[:chunk_size])
            piece = piece[chunk_size:]
        if piece:
            final.append(piece)
    return [{"text": c, "token_count": len(c)} for c in final if c]


class _ListChunker:
    """Callable matching chonkie chunker(text) -> iterable of chunk-like objs."""

    def __init__(self, chunks):
        self._chunks = chunks

    def __call__(self, text):
        # text already chunked at build time for fallback path
        return self._chunks


def _make_chunker(name, chunk_size, text=None):
    """Build a chunker that needs NO network. Defaults to recursive (character
    tokenizer). Unknown names degrade to recursive rather than failing.

    If the optional `chonkie` package is not installed, uses a pure-Python
    recursive character splitter so shadow/digest modes still work on hosts
    that cannot pip-install system-wide (PEP 668).
    """
    try:
        from chonkie import RecursiveChunker
    except Exception:
        # Fallback: precompute chunks for this text (caller passes text).
        chunks = _fallback_recursive_chunks(text or "", chunk_size)
        return _ListChunker(chunks)

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
    chunker = _make_chunker(chunker_name, chunk_size, text=text)
    chunks = chunker(text)
    out = []
    for c in chunks:
        if isinstance(c, dict) and "text" in c:
            out.append({
                "text": c["text"],
                "token_count": int(c.get("token_count", 0) or 0),
            })
        else:
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

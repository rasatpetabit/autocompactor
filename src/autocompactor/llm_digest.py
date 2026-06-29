#!/usr/bin/env python3
"""llm_digest.py — optional cheap-model "what must survive compaction" digest.

Harness-agnostic. Extracted from precompact_analyzer.py in the Pi-only pivot;
one live consumer (pi_bridge). Never raises into the caller — returns "".
"""
import json
import shlex
import subprocess
import urllib.request

from autocompactor import config_lib


def _env(name: str, default: str = "") -> str:
    if not name.startswith("AUTOCOMPACTOR_"):
        import os
        return os.environ.get(name, default)
    return config_lib.cfg.str(name[len("AUTOCOMPACTOR_"):], default=default)


def _llm_timeout() -> float:
    try:
        return float(_env("AUTOCOMPACTOR_LLM_TIMEOUT", "45"))
    except ValueError:
        return 45.0


def _openai_url(base: str) -> str:
    base = base.rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    if base.endswith("/v1"):
        return base + "/chat/completions"
    return base + "/v1/chat/completions"


def _llm_digest_openai(prompt: str, model: str, timeout: float) -> str:
    base = _env("AUTOCOMPACTOR_LLM_BASE_URL")
    if not base:
        return ""
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": "Return terse bullets only."},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0,
        "max_tokens": int(float(_env("AUTOCOMPACTOR_LLM_MAX_TOKENS", "512"))),
    }
    raw_extra = _env("AUTOCOMPACTOR_LLM_EXTRA_JSON")
    if raw_extra:
        try:
            extra = json.loads(raw_extra)
            if isinstance(extra, dict):
                payload.update(extra)
        except Exception:
            pass
    req = urllib.request.Request(
        _openai_url(base),
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + _env(
                "AUTOCOMPACTOR_LLM_API_KEY",
                _env("OPENAI_API_KEY", "EMPTY"),
            ),
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return ((data.get("choices") or [{}])[0]
            .get("message", {}).get("content", "").strip())


def _llm_digest_command(prompt: str, model: str, timeout: float) -> str:
    template = _env("AUTOCOMPACTOR_LLM_CMD")
    if not template:
        return ""
    uses_prompt_arg = "{prompt}" in template
    rendered = template.format(model=model, prompt=prompt)
    cmd = shlex.split(rendered)
    res = subprocess.run(
        cmd,
        input=None if uses_prompt_arg else prompt,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return res.stdout.strip() if res.returncode == 0 else ""


def _llm_digest_claude(prompt: str, model: str, timeout: float) -> str:
    res = subprocess.run(
        ["claude", "-p", "--model", model, prompt],
        capture_output=True, text=True, timeout=timeout,
    )
    return res.stdout.strip() if res.returncode == 0 else ""


# Preamble extracted as a module constant so _build_digest_prompt can produce
# byte-identical off-mode output and the Chonkie path reuses the same wording.
_PREAMBLE = (
    "Below are the most recent entries of a coding-session transcript "
    "(JSONL). List, as terse bullets, the facts that MUST survive a "
    "context compaction: current task, file paths touched, key "
    "decisions, working commands, unresolved errors. Bullets only.\n\n"
)

# Most recent Chonkie outcome for this process (telemetry/inspection).
# "" = off mode or no Chonkie run; otherwise a short status token.
_last_chonkie_note = ""


def _build_digest_prompt(transcript_path):
    """Build the digest prompt.

    Exact old behavior when Chonkie is off: _PREAMBLE + last-120-lines tail
    capped at 30k chars. In shadow/digest mode, chunk that EXACT same tail
    blob and either observe (shadow) or re-present it as bounded sections
    (digest). Chonkie failures fall back to the old prompt — the only
    exceptions that can escape are genuine file-read errors, which propagate
    to llm_digest's never-raise handler.

    Returns (prompt, note) where note is "" off-mode or a status token.
    """
    import os
    from autocompactor import chonkie_lib
    with open(os.path.expanduser(transcript_path), encoding="utf-8") as fh:
        tail_lines = fh.readlines()[-120:]
    blob = "".join(tail_lines)[-30_000:]  # EXACT pre-Chonkie slice
    # Off-mode is the default and MUST be byte-identical + safe. Resolve the
    # mode inside a fail-closed block so a malformed config (bad numeric value,
    # import error, broken config backend) degrades to the old prompt instead
    # of raising into llm_digest's outer handler (review #1).
    try:
        s = chonkie_lib.settings()
    except Exception:
        return _PREAMBLE + blob, "chonkie-settings-failed"
    if s["mode"] == "off":
        return _PREAMBLE + blob, ""
    # shadow or digest: compute chunks (fail-closed -> old prompt on any error)
    chunks = chonkie_lib.chunk_text(blob, s)
    if chunks is None:
        return _PREAMBLE + blob, "chonkie-failed-fallback"
    if not chunks:
        return _PREAMBLE + blob, "chonkie-empty"
    if s["mode"] == "shadow":
        # Observability only: send the unchanged old prompt.
        return _PREAMBLE + blob, f"chonkie-shadow chunks={len(chunks)}"
    # digest: present the SAME bytes as bounded sections, capped to the old size.
    rendered = chonkie_lib.render_digest(
        chunks, s["max_input_chars"], s["max_chunks"])
    if not rendered:
        return _PREAMBLE + blob, "chonkie-render-empty"
    return _PREAMBLE + rendered, f"chonkie-digest chunks={len(chunks)}"


def llm_digest(transcript_path: str) -> str:
    """Optional: ask a configured cheap model what must survive compaction."""
    global _last_chonkie_note
    try:
        prompt, note = _build_digest_prompt(transcript_path)
        _last_chonkie_note = note
        if note:
            try:
                from autocompactor import stats
                stats.log_event({"type": "chonkie_digest", "note": note})
            except Exception:
                pass
        model = _env("AUTOCOMPACTOR_LLM_MODEL", "haiku")
        timeout = _llm_timeout()
        provider = _env("AUTOCOMPACTOR_LLM_PROVIDER", "claude").lower()
        if _env("AUTOCOMPACTOR_LLM_CMD"):
            provider = "command"
        if provider in ("openai", "openai-compatible", "vllm"):
            return _llm_digest_openai(prompt, model, timeout)
        if provider == "command":
            return _llm_digest_command(prompt, model, timeout)
        return _llm_digest_claude(prompt, model, timeout)
    except Exception:
        return ""

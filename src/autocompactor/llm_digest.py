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


def llm_digest(transcript_path: str) -> str:
    """Optional: ask a configured cheap model what must survive compaction."""
    try:
        import os
        with open(os.path.expanduser(transcript_path), encoding="utf-8") as fh:
            tail_lines = fh.readlines()[-120:]
        prompt = (
            "Below are the most recent entries of a coding-session transcript "
            "(JSONL). List, as terse bullets, the facts that MUST survive a "
            "context compaction: current task, file paths touched, key "
            "decisions, working commands, unresolved errors. Bullets only.\n\n"
            + "".join(tail_lines)[-30_000:]
        )
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

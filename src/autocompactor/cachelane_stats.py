#!/usr/bin/env python3
"""
cachelane_stats.py — read-only CacheLane rollup for autocompactor evaluate.

Pi traffic goes through the LiteLLM CacheLane proxy (:7332), which records
turns under ``~/.cachelane-litellm`` (override via CACHELANE_HOME /
AUTOCOMPACTOR_CACHELANE_HOME / config CACHELANE_HOME). Claude Code uses
``~/.cachelane-claude``.

Session IDs do **not** match Pi session filenames (proxy UUID vs Pi
``YYYY-…_uuid``), so this module returns a **fleet** rollup for
observe-only telemetry and optional soft-path bias — never a hard gate.

``savings_ratio`` here is the cache-read hit ratio
(``cache_read / (input + cache_read)``). That matches what open-model /
openai-chat lanes actually realize; the CacheLane CLI's cost-unit
savings_ratio is Anthropic-tier pricing and is not recomputed here.

Never raises into the hook path; every failure returns None.
Content-free by design: ratios, counts, paths only — no transcript text.
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import asdict, dataclass
from typing import Optional


# Pi default home (see /srv/cachelane/docs/runbook-litellm.md).
DEFAULT_PI_HOME = os.path.expanduser("~/.cachelane-litellm")
DEFAULT_DB_NAME = "cachelane.db"


@dataclass(frozen=True)
class CachelaneRollup:
    """Content-free CacheLane fleet stats for one home directory."""

    home: str
    db_path: str
    turns: int
    cache_hit_ratio: float
    savings_ratio: float  # hit-based proxy; see module docstring
    pruned_blocks: int
    compression_tokens_saved: int
    sessions: int

    def as_event_fields(self) -> dict:
        """Flat dict for monitor_eval telemetry (prefixed)."""
        d = asdict(self)
        return {f"cachelane_{k}": v for k, v in d.items()}

    def observe_note(self) -> str:
        """One-line human note for reason strings (no content)."""
        return (
            f"cachelane {self.turns}t "
            f"hit={self.cache_hit_ratio:.0%} "
            f"pruned_blocks={self.pruned_blocks:,}"
        )


def resolve_home(explicit: str | None = None) -> str:
    """Resolve CacheLane home: arg > AUTOCOMPACTOR_CACHELANE_HOME >
    CACHELANE_HOME > default Pi litellm home."""
    for candidate in (
        explicit,
        os.environ.get("AUTOCOMPACTOR_CACHELANE_HOME"),
        os.environ.get("CACHELANE_HOME"),
    ):
        if candidate and str(candidate).strip():
            return os.path.expanduser(str(candidate).strip())
    return DEFAULT_PI_HOME


def db_path_for(home: str | None = None) -> str:
    return os.path.join(resolve_home(home), DEFAULT_DB_NAME)


def read_rollup(home: str | None = None) -> Optional[CachelaneRollup]:
    """Aggregate turns + compression from the local CacheLane SQLite DB.

    Returns None when the DB is missing, unreadable, or empty. Never raises.
    """
    try:
        home_path = resolve_home(home)
        path = os.path.join(home_path, DEFAULT_DB_NAME)
        if not os.path.isfile(path):
            return None
        # Read-only URI so a busy proxy writer cannot block evaluate.
        uri = f"file:{path}?mode=ro"
        con = sqlite3.connect(uri, uri=True, timeout=0.5)
        try:
            con.row_factory = sqlite3.Row
            row = con.execute(
                """
                SELECT
                  COUNT(*) AS turns,
                  COUNT(DISTINCT session_id) AS sessions,
                  COALESCE(SUM(input_tokens), 0) AS input_tokens,
                  COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens,
                  COALESCE(SUM(pruned_blocks_count), 0) AS pruned_blocks
                FROM turns
                """
            ).fetchone()
            if not row or int(row["turns"] or 0) <= 0:
                return None

            compression_saved = 0
            try:
                crow = con.execute(
                    "SELECT COALESCE(SUM(tokens_saved), 0) AS s "
                    "FROM compression_events"
                ).fetchone()
                compression_saved = int(crow["s"] or 0) if crow else 0
            except Exception:
                compression_saved = 0
        finally:
            con.close()

        input_tokens = int(row["input_tokens"] or 0)
        cache_read = int(row["cache_read_tokens"] or 0)
        denom = input_tokens + cache_read
        hit = (cache_read / denom) if denom > 0 else 0.0

        return CachelaneRollup(
            home=home_path,
            db_path=path,
            turns=int(row["turns"] or 0),
            cache_hit_ratio=round(hit, 4),
            savings_ratio=round(hit, 4),
            pruned_blocks=int(row["pruned_blocks"] or 0),
            compression_tokens_saved=compression_saved,
            sessions=int(row["sessions"] or 0),
        )
    except Exception:
        return None


def soft_bias_suppresses(
    rollup: Optional[CachelaneRollup],
    *,
    enabled: bool,
    min_savings_ratio: float,
    occupancy: float,
    hard: float,
) -> bool:
    """True when CacheLane soft-bias should suppress a SOFT-band recommend.

    Hard line is never suppressed (safety). Requires a live rollup with
    savings_ratio >= min_savings_ratio. Fail-open: missing rollup → False.
    """
    if not enabled or rollup is None:
        return False
    if occupancy >= hard:
        return False
    try:
        return float(rollup.savings_ratio) >= float(min_savings_ratio)
    except Exception:
        return False

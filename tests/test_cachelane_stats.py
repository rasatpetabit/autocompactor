"""Unit tests for cachelane_stats (observe-only rollup + soft bias)."""
import sqlite3
from pathlib import Path

from autocompactor import cachelane_stats


def _make_db(path: Path, *, turns=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path))
    con.executescript(
        """
        CREATE TABLE turns (
          id TEXT PRIMARY KEY,
          workspace_id TEXT,
          session_id TEXT,
          turn_number INTEGER,
          model TEXT,
          input_tokens INTEGER,
          output_tokens INTEGER,
          cache_creation_5m_tokens INTEGER DEFAULT 0,
          cache_creation_1h_tokens INTEGER DEFAULT 0,
          cache_read_tokens INTEGER DEFAULT 0,
          effective_cost_units REAL DEFAULT 0,
          prefix_breakpoint_hash TEXT,
          middle_breakpoint_hash TEXT,
          pruned_blocks_count INTEGER DEFAULT 0,
          keepalive_pings_since_last_turn INTEGER DEFAULT 0,
          created_at INTEGER,
          signals TEXT,
          request_mutated INTEGER DEFAULT 1,
          provider TEXT,
          cache_write_tokens INTEGER DEFAULT 0
        );
        CREATE TABLE compression_events (
          id TEXT PRIMARY KEY,
          turn_id TEXT,
          session_id TEXT,
          workspace_id TEXT,
          tool_use_id TEXT,
          content_type TEXT,
          original_tokens INTEGER,
          compressed_tokens INTEGER,
          tokens_saved INTEGER,
          created_at INTEGER
        );
        """
    )
    if turns:
        con.executemany(
            """
            INSERT INTO turns (
              id, workspace_id, session_id, turn_number, model,
              input_tokens, output_tokens, cache_read_tokens,
              pruned_blocks_count, created_at, provider
            ) VALUES (?, 'ws', ?, ?, 'grok-4.5', ?, 10, ?, ?, 1, 'openai-chat')
            """,
            turns,
        )
    con.commit()
    con.close()


def test_read_rollup_missing_db(tmp_path):
    assert cachelane_stats.read_rollup(str(tmp_path / "nope")) is None


def test_read_rollup_hit_ratio(tmp_path):
    db = tmp_path / "cachelane.db"
    # two turns: input 600 + cache_read 400 → hit 0.4
    _make_db(
        db,
        turns=[
            ("t1", "s1", 1, 300, 200, 3),
            ("t2", "s1", 2, 300, 200, 5),
        ],
    )
    rollup = cachelane_stats.read_rollup(str(tmp_path))
    assert rollup is not None
    assert rollup.turns == 2
    assert rollup.sessions == 1
    assert rollup.pruned_blocks == 8
    assert abs(rollup.cache_hit_ratio - 0.4) < 1e-3
    assert abs(rollup.savings_ratio - 0.4) < 1e-3
    fields = rollup.as_event_fields()
    assert fields["cachelane_turns"] == 2
    assert "cachelane" in rollup.observe_note()


def test_soft_bias_only_soft_band():
    rollup = cachelane_stats.CachelaneRollup(
        home="h", db_path="d", turns=10, cache_hit_ratio=0.5,
        savings_ratio=0.5, pruned_blocks=1, compression_tokens_saved=0,
        sessions=1,
    )
    # Soft band + high savings → suppress
    assert cachelane_stats.soft_bias_suppresses(
        rollup, enabled=True, min_savings_ratio=0.40,
        occupancy=0.45, hard=0.58,
    )
    # Hard band → never suppress
    assert not cachelane_stats.soft_bias_suppresses(
        rollup, enabled=True, min_savings_ratio=0.40,
        occupancy=0.60, hard=0.58,
    )
    # Disabled → never
    assert not cachelane_stats.soft_bias_suppresses(
        rollup, enabled=False, min_savings_ratio=0.40,
        occupancy=0.45, hard=0.58,
    )
    # Low savings → never
    low = cachelane_stats.CachelaneRollup(
        home="h", db_path="d", turns=10, cache_hit_ratio=0.1,
        savings_ratio=0.1, pruned_blocks=0, compression_tokens_saved=0,
        sessions=1,
    )
    assert not cachelane_stats.soft_bias_suppresses(
        low, enabled=True, min_savings_ratio=0.40,
        occupancy=0.45, hard=0.58,
    )


def test_soft_bias_fail_open_on_none():
    assert not cachelane_stats.soft_bias_suppresses(
        None, enabled=True, min_savings_ratio=0.40,
        occupancy=0.45, hard=0.58,
    )

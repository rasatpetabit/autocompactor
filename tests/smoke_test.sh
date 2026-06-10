#!/usr/bin/env bash
# tests/smoke_test.sh — verify autocompactor end-to-end against bundled
# fixtures, in an isolated HOME so nothing on the machine is touched.
# Run from the autocompactor directory:  bash tests/smoke_test.sh
set -euo pipefail
cd "$(dirname "$0")/.."

export HOME="$(mktemp -d)"
trap 'rm -rf "$HOME"' EXIT
# Isolate from live tuning: a user's settings.json env (injected into
# Claude Code child processes) must not change fixture expectations.
while IFS='=' read -r v _; do unset "$v"; done < <(env | grep '^AUTOCOMPACTOR_')
# The rich fixture sits at ~84k context; default guard values (POST_FLOOR
# 70k + MIN_SAVINGS 30k) would suppress every recommendation below ~100k.
# Pin guard values that keep the fixture eligible.
export AUTOCOMPACTOR_POST_FLOOR=50000 AUTOCOMPACTOR_MIN_SAVINGS=20000
FIX=tests/fixtures
fail() { echo "FAIL: $1"; exit 1; }

echo "1) monitor recommends at boundary (rich fixture, new-topic prompt)"
OUT=$(echo '{"session_id":"smoke","transcript_path":"'$FIX'/rich_transcript.jsonl","cwd":"/tmp","hook_event_name":"UserPromptSubmit","prompt":"now plan the website redesign"}' \
  | python3 context_monitor.py)
echo "$OUT" | grep -q "Good moment to compact" || fail "no recommendation"
echo "$OUT" | grep -q "plan step was just completed" || fail "todo_step signal missing"
# error_resolved is observe-only (anti-predictive on live data): it must
# keep flowing into telemetry but never into the recommendation reason.
echo "$OUT" | grep -q "debug loop just concluded" && fail "observe-only signal leaked into reason" || true

echo "2) cooldown suppresses immediate re-recommendation"
OUT2=$(echo '{"session_id":"smoke","transcript_path":"'$FIX'/rich_transcript.jsonl","cwd":"/tmp","hook_event_name":"UserPromptSubmit","prompt":"again"}' \
  | python3 context_monitor.py)
[ -z "$OUT2" ] || fail "cooldown did not suppress"

echo "3) precompact: instructions + artifacts + backup"
OUT3=$(echo '{"session_id":"smoke","transcript_path":"'$FIX'/rich_transcript.jsonl","cwd":"/tmp","hook_event_name":"PreCompact","trigger":"manual","custom_instructions":"user note"}' \
  | python3 precompact_analyzer.py)
echo "$OUT3" | grep -q "structured handoff" || fail "schema instructions missing"
echo "$OUT3" | grep -q "user note" || fail "user instructions not preserved"
[ -f "$HOME/.claude/autocompactor/artifacts/smoke.json" ] || fail "artifacts not written"
ls "$HOME"/.claude/autocompactor/backups/smoke-*.jsonl >/dev/null || fail "backup missing"
grep -q "can1" "$HOME/.claude/autocompactor/artifacts/smoke.json" || fail "correction not captured"

echo "4) one-shot artifact re-injection on next prompt"
OUT4=$(echo '{"session_id":"smoke","transcript_path":"'$FIX'/rich_transcript.jsonl","cwd":"/tmp","hook_event_name":"UserPromptSubmit","prompt":"continue"}' \
  | python3 context_monitor.py)
echo "$OUT4" | grep -q "USER CORRECTIONS" || fail "digest not injected"
OUT5=$(echo '{"session_id":"smoke","transcript_path":"'$FIX'/rich_transcript.jsonl","cwd":"/tmp","hook_event_name":"UserPromptSubmit","prompt":"continue more"}' \
  | python3 context_monitor.py)
echo "$OUT5" | grep -q "USER CORRECTIONS" && fail "digest injected twice" || true

echo "5) backtester over fixture corpus"
OUT6=$(python3 analyze_corpus.py --root "$FIX/corpus" --days 36500 2>&1)
echo "$OUT6" | grep -q "Compaction events found:  1" || fail "compaction not detected"
echo "$OUT6" | grep -q "Late compactions" || fail "backtest summary incomplete"

echo "6) telemetry recorded and aggregates"
python3 analyze_corpus.py --events | python3 -c "
import json,sys
d=json.load(sys.stdin)
assert d['monitor_evals'] >= 2, d
assert d['compactions']['total'] == 1, d
print('   telemetry ok:', d['monitor_evals'], 'evals,', d['compactions']['total'], 'compaction')"

echo
echo "ALL SMOKE TESTS PASSED"

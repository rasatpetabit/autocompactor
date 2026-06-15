#!/usr/bin/env bash
# tests/smoke_test_pi.sh — end-to-end Pi-bridge smoke test against bundled fixtures, in an isolated HOME. Gated: PI_SMOKE=1 bash tests/smoke_test_pi.sh (no-op otherwise).

set -euo pipefail
cd "$(dirname "$0")/.."

if [ "${PI_SMOKE:-}" != "1" ]; then
    echo "PI_SMOKE not set; skipping pi smoke test"
    exit 0
fi

fail() {
    echo "FAIL: $1"
    exit 1
}

export HOME="$(mktemp -d)"
trap 'rm -rf "$HOME"' EXIT

while IFS='=' read -r v _; do unset "$v"; done < <(env | grep '^AUTOCOMPACTOR_')

export AUTOCOMPACTOR_STATE_DIR="$HOME/pi-state"

FIX=tests/fixtures/pi
BRIDGE=src/pi_bridge.py

echo "1) evaluate recommends at boundary fixture"
OUT=$(python3 "$BRIDGE" evaluate --session "$FIX/with_compaction.jsonl" --tokens 150000 --context-window 200000)
echo "$OUT" | grep -q '"recommend": true' || fail "no recommendation at boundary"

echo "2) cooldown suppresses immediate second evaluate"
OUT2=$(python3 "$BRIDGE" evaluate --session "$FIX/with_compaction.jsonl" --tokens 150000 --context-window 200000)
echo "$OUT2" | grep -q '"recommend": false' || fail "cooldown did not suppress"

echo "3) prepare writes backup + artifacts under the Pi state dir"
OUT3=$(python3 "$BRIDGE" prepare --session "$FIX/with_compaction.jsonl")
echo "$OUT3" | grep -q '"customInstructions"' || fail "no customInstructions"
ls "$AUTOCOMPACTOR_STATE_DIR"/backups/with_compaction-*.jsonl >/dev/null || fail "backup missing"
[ -f "$AUTOCOMPACTOR_STATE_DIR/artifacts/with_compaction.json" ] || fail "artifacts not written"

echo "4) reinject emits a digest object"
OUT4=$(python3 "$BRIDGE" reinject --session "$FIX/with_compaction.jsonl")
echo "$OUT4" | grep -q '"customType": "autocompactor.digest"' || fail "no digest customType"
echo "$OUT4" | grep -q '"text"' || fail "digest text missing"

echo "5) bridge never breaks Pi: garbage stdin + missing session exit 0, no traceback"
ERR=$(echo 'not json at all' | python3 "$BRIDGE" evaluate --session /nonexistent/path.jsonl 2>&1 >/dev/null) || fail "bridge exited non-zero"
echo "$ERR" | grep -q "Traceback" && fail "stack trace leaked" || true

python3 "$BRIDGE" frobnicate >/dev/null 2>&1 || fail "unknown subcommand exited non-zero"

echo "6) shim: actuate-mode compaction runs prepare exactly once (no double-prepare)"
if command -v bun >/dev/null 2>&1; then
  # The double-prepare fix lives in the TS shim; this exercises the live
  # event flow against the real module with a mocked ExtensionAPI. Run from
  # the repo root so the shim's config read resolves.
  ( cd "$(dirname "$0")/.." && bun test tests/shim_prepare.test.ts ) \
    || fail "shim prepare-count regression test failed"
else
  echo "   (bun not installed; skipped)"
fi

echo ""
echo "ALL PI SMOKE TESTS PASSED"

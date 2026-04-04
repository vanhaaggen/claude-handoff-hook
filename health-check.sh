#!/usr/bin/env bash
# health-check.sh — Verify the handoff hook installation is working correctly
#
# Runs five checks:
#   1. hook.py exists
#   2. Python3 is available
#   3. hook.py has valid syntax
#   4. settings.json has a current-version hook entry
#   5. Hook fires correctly on a synthetic payload
#
# Exit code 0 = all passed, 1 = one or more failed.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
HOOK_SCRIPT="$SCRIPT_DIR/hook.py"
SETTINGS="$HOME/.claude/settings.json"

PASS=0
FAIL=0

ok()   { echo "  ✓ $*"; PASS=$((PASS + 1)); }
fail() { echo "  ✗ $*"; FAIL=$((FAIL + 1)); }

echo "Claude Handoff Hook — Health Check"
echo "==================================="
echo ""

# ── 1. hook.py exists ─────────────────────────────────────────────────────────
if [ -f "$HOOK_SCRIPT" ]; then
  ok "hook.py found at $HOOK_SCRIPT"
else
  fail "hook.py not found at $HOOK_SCRIPT"
fi

# ── 2. Python3 is available ───────────────────────────────────────────────────
PYTHON3="$(pyenv which python3 2>/dev/null || which python3 2>/dev/null || echo "")"
if [ -n "$PYTHON3" ] && "$PYTHON3" --version &>/dev/null; then
  ok "Python3 available: $("$PYTHON3" --version 2>&1)"
else
  fail "Python3 not found (tried pyenv and PATH)"
  echo ""
  echo "Results: $PASS passed, $FAIL failed"
  exit 1
fi

# ── 3. hook.py syntax ────────────────────────────────────────────────────────
if "$PYTHON3" -m py_compile "$HOOK_SCRIPT" 2>/dev/null; then
  ok "hook.py syntax valid"
else
  fail "hook.py has a syntax error — run: $PYTHON3 -m py_compile $HOOK_SCRIPT"
fi

# ── 4. settings.json registration + version ──────────────────────────────────
_check_version() {
  "$PYTHON3" - "$SETTINGS" "$HOOK_SCRIPT" <<'PYEOF'
import json, sys, re
settings_path, hook_path = sys.argv[1], sys.argv[2]

hook_version = "unknown"
try:
    with open(hook_path) as f:
        for line in f:
            m = re.match(r'^HOOK_VERSION\s*=\s*["\'](.+)["\']', line.strip())
            if m:
                hook_version = m.group(1)
                break
except OSError:
    pass

try:
    with open(settings_path) as f:
        settings = json.load(f)
    for entry in settings.get("hooks", {}).get("Stop", []):
        if "_handoff_hook" in entry:
            installed = entry.get("version", "unknown")
            if installed == hook_version:
                print("current:" + hook_version)
            else:
                print("outdated:" + installed + ":" + hook_version)
            sys.exit(0)
except Exception as e:
    print("error:" + str(e))
    sys.exit(0)
print("missing")
PYEOF
}

if [ ! -f "$SETTINGS" ]; then
  fail "settings.json not found at $SETTINGS -- run ./install.sh"
elif ! grep -q '"_handoff_hook"' "$SETTINGS" 2>/dev/null; then
  fail "Hook not registered in settings.json -- run ./install.sh"
else
  VS="$(_check_version)"
  case "$VS" in
    current:*)
      ok "Hook v${VS#*:} registered in settings.json (up to date)"
      ;;
    outdated:*)
      VTAIL="${VS#*:}"
      fail "Hook is outdated (installed: v${VTAIL%%:*}, current: v${VTAIL##*:}) -- run ./install.sh to upgrade"
      ;;
    error:*)
      fail "Could not parse settings.json: ${VS#*:}"
      ;;
    *)
      ok "Hook registered in settings.json"
      ;;
  esac
fi

# ── 5. Functional test with synthetic payload ─────────────────────────────────
echo ""
echo "  Running functional test..."

TMPTEST="$(mktemp -d)"
TRANSCRIPT="$TMPTEST/transcript.jsonl"
SESSION_ID="health-check-$$-$RANDOM"
STATE_TRIGGERED="$HOME/.claude/handoff-hook-state/$SESSION_ID.triggered"
STATE_WARNED="$HOME/.claude/handoff-hook-state/$SESSION_ID.warned"
ERR_LOG="$TMPTEST/hook-stderr.txt"

# Synthetic transcript: 160k/200k tokens (80% — above both warn and full threshold)
printf '%s\n' \
  '{"type":"user","message":{"role":"user","content":"Health check test message"}}' \
  '{"type":"assistant","message":{"role":"assistant","model":"claude-sonnet-4-6","content":"Health check test response","usage":{"input_tokens":160000,"cache_creation_input_tokens":0,"cache_read_input_tokens":0,"output_tokens":100}}}' \
  > "$TRANSCRIPT"

PAYLOAD="{\"session_id\":\"$SESSION_ID\",\"transcript_path\":\"$TRANSCRIPT\",\"cwd\":\"$TMPTEST\"}"

OUTPUT="$("$PYTHON3" "$HOOK_SCRIPT" <<< "$PAYLOAD" 2>"$ERR_LOG")"
EXIT_CODE=$?

# Validate output is JSON with systemMessage key
VALID=0
if [ $EXIT_CODE -eq 0 ] && [ -n "$OUTPUT" ]; then
  if "$PYTHON3" -c "
import json, sys
d = json.loads(sys.argv[1])
assert 'systemMessage' in d, 'missing systemMessage key'
assert 'HANDOFF HOOK' in d['systemMessage'], 'unexpected message content'
" "$OUTPUT" 2>/dev/null; then
    VALID=1
  fi
fi

# Cleanup before reporting
rm -f "$STATE_TRIGGERED" "$STATE_WARNED"
rm -rf "$TMPTEST"

if [ $VALID -eq 1 ]; then
  ok "Hook fires and returns valid systemMessage on synthetic payload"
else
  ERRMSG="$(cat "$ERR_LOG" 2>/dev/null | head -1)"
  fail "Hook did not return expected output (exit=$EXIT_CODE${ERRMSG:+, stderr: $ERRMSG})"
fi

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
echo "Results: $PASS passed, $FAIL failed"
if [ $FAIL -eq 0 ]; then
  echo "✓ All checks passed — hook is healthy."
  exit 0
else
  echo "✗ ${FAIL} check(s) failed. Run ./install.sh to fix installation issues."
  exit 1
fi

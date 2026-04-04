#!/usr/bin/env bash
# Installs the handoff hook into ~/.claude/settings.json
# Safe to re-run — upgrades an outdated installation automatically.

set -euo pipefail

HOOK_SCRIPT="$(cd "$(dirname "$0")" && pwd)/hook.py"
SETTINGS="$HOME/.claude/settings.json"

# Resolve python3 — pyenv shims don't work in Claude Code's hook subprocess
# because it runs without shell profile initialization.
PYTHON3="$(pyenv which python3 2>/dev/null || which python3)"

# Validate the resolved binary
if ! "$PYTHON3" --version &>/dev/null; then
  echo "Error: Could not find a working python3 (tried: $PYTHON3)" >&2
  exit 1
fi

# Make hook.py executable
chmod +x "$HOOK_SCRIPT"

# Ensure ~/.claude exists
mkdir -p "$HOME/.claude"

# Create settings.json if it doesn't exist
if [ ! -f "$SETTINGS" ]; then
  echo "{}" > "$SETTINGS"
fi

# ── Check installation status and compare versions ────────────────────────────
# We write the checker to a temp file to avoid heredoc-inside-$() quote issues.
_CHECKER=$(mktemp)
cat > "$_CHECKER" << 'PYEOF'
import json, sys, re

settings_path, hook_path = sys.argv[1], sys.argv[2]

# Extract HOOK_VERSION from hook.py
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

# Check settings.json for an existing entry
try:
    with open(settings_path) as f:
        settings = json.load(f)
    for entry in settings.get("hooks", {}).get("Stop", []):
        if "_handoff_hook" in entry:
            installed = entry.get("version", "unknown")
            if installed == hook_version:
                print("current:" + hook_version)
            else:
                print("upgrade:" + installed + ":" + hook_version)
            sys.exit(0)
except (OSError, json.JSONDecodeError):
    pass

print("install:" + hook_version)
PYEOF

INSTALL_STATUS=$("$PYTHON3" "$_CHECKER" "$SETTINGS" "$HOOK_SCRIPT")
rm -f "$_CHECKER"

STATUS="${INSTALL_STATUS%%:*}"
VERSION_TAIL="${INSTALL_STATUS#*:}"

if [ "$STATUS" = "current" ]; then
  echo "✓ Handoff hook v${VERSION_TAIL} already installed (up to date)"
  exit 0
elif [ "$STATUS" = "upgrade" ]; then
  OLD_VER="${VERSION_TAIL%%:*}"
  NEW_VER="${VERSION_TAIL##*:}"
  echo "↑ Upgrading handoff hook from v${OLD_VER} to v${NEW_VER}..."
  # Remove the outdated entry; fall through to install the new one
  "$PYTHON3" - "$SETTINGS" <<'PYEOF'
import json, sys
settings_path = sys.argv[1]
with open(settings_path) as f:
    settings = json.load(f)
stop_hooks = settings.get("hooks", {}).get("Stop", [])
settings["hooks"]["Stop"] = [e for e in stop_hooks if "_handoff_hook" not in e]
with open(settings_path, "w") as f:
    json.dump(settings, f, indent=2)
    f.write("\n")
PYEOF
fi

# ── Install (fresh install or post-upgrade) ───────────────────────────────────
"$PYTHON3" - "$SETTINGS" "$HOOK_SCRIPT" "$PYTHON3" <<'PYEOF'
import json, sys, re

settings_path, hook_path, python_path = sys.argv[1], sys.argv[2], sys.argv[3]

# Extract version from hook.py
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

with open(settings_path) as f:
    settings = json.load(f)

new_hook = {
    "_handoff_hook": "https://github.com/vanhaaggen/claude-handoff-hook",
    "version": hook_version,
    "matcher": "",
    "hooks": [
        {
            "type": "command",
            "command": '"' + python_path + '" "' + hook_path + '"'
        }
    ]
}

hooks = settings.setdefault("hooks", {})
stop_hooks = hooks.setdefault("Stop", [])
stop_hooks.append(new_hook)

with open(settings_path, "w") as f:
    json.dump(settings, f, indent=2)
    f.write("\n")

print("✓ Hook v" + hook_version + " added to " + settings_path)
PYEOF

echo ""
echo "Done. The handoff hook will fire when context reaches 75%."
echo ""
echo "To adjust thresholds, set env vars in the hook entry or export them:"
echo "  HANDOFF_THRESHOLD=0.75       (default, triggers full handoff)"
echo "  HANDOFF_WARN_THRESHOLD=0.60  (default, triggers early warning; 0 to disable)"
echo "  HANDOFF_CONTEXT_WINDOW=200000  (default, fallback when model unknown)"
echo ""
echo "Run ./health-check.sh at any time to verify the installation."

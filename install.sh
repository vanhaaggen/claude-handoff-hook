#!/usr/bin/env bash
# Installs the handoff hook into ~/.claude/settings.json
# Safe to re-run — checks for existing entry before modifying.

set -euo pipefail

HOOK_SCRIPT="$(cd "$(dirname "$0")" && pwd)/hook.py"
SETTINGS="$HOME/.claude/settings.json"

# Resolve python3 — pyenv shims don't work in Claude Code's hook subprocess
# because it runs without shell profile initialization.
PYTHON3="$(pyenv which python3 2>/dev/null || which python3)"

# Make hook.py executable
chmod +x "$HOOK_SCRIPT"

# Ensure ~/.claude exists
mkdir -p "$HOME/.claude"

# Create settings.json if it doesn't exist
if [ ! -f "$SETTINGS" ]; then
  echo "{}" > "$SETTINGS"
fi

# Check if already installed
if grep -q "claude-handoff-hook" "$SETTINGS" 2>/dev/null; then
  echo "✓ Handoff hook already installed in $SETTINGS"
  exit 0
fi

# Use Python to safely merge the hook into existing JSON
python3 - "$SETTINGS" "$HOOK_SCRIPT" "$PYTHON3" <<'PYEOF'
import json, sys

settings_path = sys.argv[1]
hook_path = sys.argv[2]
python_path = sys.argv[3]

with open(settings_path, "r") as f:
    settings = json.load(f)

new_hook = {
    "matcher": "",
    "hooks": [
        {
            "type": "command",
            "command": f"{python_path} {hook_path}"
        }
    ]
}

hooks = settings.setdefault("hooks", {})
stop_hooks = hooks.setdefault("Stop", [])
stop_hooks.append(new_hook)

with open(settings_path, "w") as f:
    json.dump(settings, f, indent=2)
    f.write("\n")

print(f"✓ Hook added to {settings_path}")
PYEOF

echo ""
echo "Done. The handoff hook will fire when context reaches 60%."
echo ""
echo "To adjust the threshold, set env vars in the hook entry or edit hook.py:"
echo "  HANDOFF_THRESHOLD=0.60      (default)"
echo "  HANDOFF_CONTEXT_WINDOW=200000  (default, tokens)"

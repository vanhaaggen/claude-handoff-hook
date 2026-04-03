#!/usr/bin/env bash
# Removes the handoff hook from ~/.claude/settings.json
# Safe to re-run.

set -euo pipefail

SETTINGS="$HOME/.claude/settings.json"
STATE_DIR="$HOME/.claude/handoff-hook-state"

# Resolve python3 — pyenv shims don't work without shell profile initialization
PYTHON3="$(pyenv which python3 2>/dev/null || which python3)"

if ! "$PYTHON3" --version &>/dev/null; then
  echo "Error: Could not find a working python3 (tried: $PYTHON3)" >&2
  exit 1
fi

if [ ! -f "$SETTINGS" ]; then
  echo "Nothing to do — $SETTINGS does not exist."
  exit 0
fi

if ! grep -q '"_handoff_hook"' "$SETTINGS" 2>/dev/null; then
  echo "Handoff hook not found in $SETTINGS."
else
  "$PYTHON3" - "$SETTINGS" <<'PYEOF'
import json, sys

settings_path = sys.argv[1]

with open(settings_path, "r") as f:
    settings = json.load(f)

stop_hooks = settings.get("hooks", {}).get("Stop", [])
filtered = [h for h in stop_hooks if "_handoff_hook" not in h]

if len(filtered) == len(stop_hooks):
    print("Hook entry not found — nothing changed.")
    sys.exit(0)

settings["hooks"]["Stop"] = filtered

# Clean up empty structures
if not settings["hooks"]["Stop"]:
    del settings["hooks"]["Stop"]
if not settings["hooks"]:
    del settings["hooks"]

with open(settings_path, "w") as f:
    json.dump(settings, f, indent=2)
    f.write("\n")

print(f"✓ Hook removed from {settings_path}")
PYEOF
fi

# Offer to remove state directory
if [ -d "$STATE_DIR" ]; then
  read -r -p "Remove state directory $STATE_DIR? [y/N] " confirm
  if [[ "$confirm" =~ ^[Yy]$ ]]; then
    rm -rf "$STATE_DIR"
    echo "✓ State directory removed."
  fi
fi

echo "Done."

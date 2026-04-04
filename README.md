# claude-handoff-hook

A [Claude Code](https://claude.ai/code) Stop hook that automatically asks Claude to write a handoff document when the context window approaches its limit — so you never lose progress mid-task.

## How it works

The hook fires after every assistant turn. Once context usage crosses a configurable threshold (default: 75%), it:

1. Writes a `handoff-<YYYYMMDD-HHMM>.md` file in the current working directory — directly, without relying on Claude. The file contains the full conversation transcript (user messages, assistant responses, and tool calls).
2. Injects a system message instructing Claude to fill in a structured Summary section (goal, progress, decisions, next steps, critical context) in the already-created file.
3. Asks Claude to suggest starting a fresh session with: *"Read handoff-\<filename\>.md and continue from there."*

An optional early warning fires once at 60% (configurable) so you can wrap up long-running tasks before the hard stop.

The hook fires **once per session** — it won't interrupt you repeatedly.

## Requirements

- Python 3 (no external libraries)
- [Claude Code](https://claude.ai/code) CLI

## Installation

```bash
git clone https://github.com/vanhaaggen/claude-handoff-hook.git
cd claude-handoff-hook
./install.sh
```

`install.sh` adds the hook to `~/.claude/settings.json`. It is safe to re-run — if an outdated version is detected it upgrades in place automatically.

## Verification

```bash
./health-check.sh
```

Runs five checks (hook.py found, Python3 available, syntax valid, settings.json up to date, functional test with a synthetic payload) and exits 0 if everything is healthy.

## Configuration

Three environment variables control behavior. Set them inside the hook entry in `~/.claude/settings.json`, or export them in your shell profile:

| Variable | Default | Description |
|---|---|---|
| `HANDOFF_THRESHOLD` | `0.75` | Fraction of context window that triggers the full handoff (0–1) |
| `HANDOFF_WARN_THRESHOLD` | `0.60` | Fraction that triggers an early heads-up message (0–1); set to `0` to disable |
| `HANDOFF_CONTEXT_WINDOW` | `200000` | Fallback context window size in tokens (used when the model is not recognised) |

The hook auto-detects the context window size from the model name in the transcript, so `HANDOFF_CONTEXT_WINDOW` is only needed for models not yet in the built-in table.

Example — trigger warning at 65%, full handoff at 80%:

```json
{
  "hooks": {
    "Stop": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "HANDOFF_WARN_THRESHOLD=0.65 HANDOFF_THRESHOLD=0.80 python3 /path/to/hook.py"
          }
        ]
      }
    ]
  }
}
```

## Manual installation

If you prefer to add the hook by hand, open `~/.claude/settings.json` and add:

```json
{
  "hooks": {
    "Stop": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "python3 /absolute/path/to/hook.py"
          }
        ]
      }
    ]
  }
}
```

## Uninstallation

```bash
./uninstall.sh
```

This removes the hook entry from `~/.claude/settings.json` and optionally deletes the state directory (`~/.claude/handoff-hook-state`) that tracks which sessions have already triggered a handoff.

## How the token count is calculated

The hook reads token usage directly from Claude Code's JSONL transcript — no API calls, no extra dependencies. It reverse-scans the file from the end to find the last assistant message in O(1) time regardless of transcript length. The formula matches Claude Code's own accounting:

```
input_tokens + cache_creation_input_tokens + cache_read_input_tokens + output_tokens
```

# claude-handoff-hook

A [Claude Code](https://claude.ai/code) Stop hook that automatically asks Claude to write a handoff document when the context window approaches its limit — so you never lose progress mid-task.

## How it works

The hook fires after every assistant turn. Once context usage crosses a configurable threshold (default: 60%), it:

1. Writes a `handoff-<YYYYMMDD-HHMM>.md` file in the current working directory — directly, without relying on Claude. The file contains the full conversation transcript (user messages, assistant responses, and tool calls).
2. Injects a system message instructing Claude to fill in a structured Summary section (goal, progress, decisions, next steps, critical context) in the already-created file.
3. Asks Claude to suggest starting a fresh session with: *"Read handoff-\<filename\>.md and continue from there."*

The hook fires **once per session** — it won't spam you on every subsequent turn.

## Requirements

- Python 3 (no external libraries)
- [Claude Code](https://claude.ai/code) CLI

## Installation

```bash
git clone https://github.com/<your-username>/claude-handoff-hook.git
cd claude-handoff-hook
./install.sh
```

`install.sh` adds the hook to `~/.claude/settings.json` and is safe to re-run (idempotent).

## Configuration

Two environment variables control behavior. Set them inside the hook entry in `~/.claude/settings.json`, or export them in your shell profile:

| Variable | Default | Description |
|---|---|---|
| `HANDOFF_THRESHOLD` | `0.60` | Fraction of context window that triggers the handoff (0–1) |
| `HANDOFF_CONTEXT_WINDOW` | `200000` | Total context window size in tokens |

Example — trigger at 75% for a 200k window:

```json
{
  "hooks": {
    "Stop": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "HANDOFF_THRESHOLD=0.75 python3 /path/to/hook.py"
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

Remove the hook entry from `~/.claude/settings.json`. You can also delete the state directory that tracks which sessions have already been triggered:

```bash
rm -rf ~/.claude/handoff-hook-state
```

## How the token count is calculated

The hook reads token usage directly from Claude Code's JSONL transcript — no API calls, no extra dependencies, the formula it uses is:

```
input_tokens + cache_creation_input_tokens + cache_read_input_tokens + output_tokens
```

#!/usr/bin/env python3
"""
Claude Code Context Handoff Hook

Fires after every assistant turn (Stop hook).
When context usage crosses THRESHOLD, injects a system message asking
Claude to write a handoff .md so you can continue in a fresh session.

Token counting uses the exact usage data already present in the transcript
(same formula as Claude Code internals: tokens.ts:getTokenCountFromUsage).
No API calls, no external libraries needed.

Configuration (env vars):
  HANDOFF_THRESHOLD     Float 0-1, default 0.60  (60%)
  HANDOFF_CONTEXT_WINDOW  Integer, default 200000
"""

import json
import os
import sys
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────────

THRESHOLD = float(os.environ.get("HANDOFF_THRESHOLD", "0.60"))
CONTEXT_WINDOW = int(os.environ.get("HANDOFF_CONTEXT_WINDOW", "200000"))

# ── State dir (one file per session to fire only once) ───────────────────────

STATE_DIR = Path.home() / ".claude" / "handoff-hook-state"


def state_file(session_id: str) -> Path:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    return STATE_DIR / f"{session_id}.triggered"


# ── Token counting ────────────────────────────────────────────────────────────

def count_tokens_from_transcript(transcript_path: str) -> int:
    """
    Reads the JSONL transcript and returns the total context window tokens
    from the last real assistant message.
    """
    last_usage = None

    with open(transcript_path, "r", encoding="utf-8") as f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            try:
                entry = json.loads(raw)
            except json.JSONDecodeError:
                continue

            if entry.get("type") != "assistant":
                continue

            usage = entry.get("message", {}).get("usage")
            if not usage:
                continue

            # Skip synthetic messages (no real model)
            model = entry.get("message", {}).get("model", "")
            if model == "synthetic":
                continue

            last_usage = usage

    if last_usage is None:
        return 0

    return (
        last_usage.get("input_tokens", 0)
        + last_usage.get("cache_creation_input_tokens", 0)
        + last_usage.get("cache_read_input_tokens", 0)
        + last_usage.get("output_tokens", 0)
    )


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    payload = json.loads(sys.stdin.read())

    session_id = payload.get("session_id", "unknown")
    transcript_path = payload.get("transcript_path", "")

    if not transcript_path or not Path(transcript_path).exists():
        sys.exit(0)

    # Fire only once per session
    sf = state_file(session_id)
    if sf.exists():
        sys.exit(0)

    tokens_used = count_tokens_from_transcript(transcript_path)
    if tokens_used == 0:
        sys.exit(0)

    pct = tokens_used / CONTEXT_WINDOW

    if pct < THRESHOLD:
        sys.exit(0)

    # Mark triggered so this doesn't repeat
    sf.write_text(f"{tokens_used}/{CONTEXT_WINDOW} = {pct:.1%}")

    message = (
        f"[HANDOFF HOOK] Context window is at {pct:.0%} "
        f"({tokens_used:,} / {CONTEXT_WINDOW:,} tokens). "
        "You must write a handoff document NOW before doing anything else. "
        "Create a file named handoff-<YYYYMMDD-HHMM>.md in the current working directory. "
        "The file must contain:\n"
        "1. **Goal** — what we are trying to accomplish\n"
        "2. **Progress** — what has been analysed, decided, or implemented so far\n"
        "3. **Key decisions** — rationale for important choices made\n"
        "4. **Exact next steps** — concrete actions for a fresh session to continue\n"
        "5. **Critical context** — file paths, function names, constraints, gotchas\n\n"
        "After writing the file, tell the user the handoff is ready and suggest "
        "starting a new session with: 'Read handoff-<filename>.md and continue from there.'"
    )

    print(json.dumps({"systemMessage": message}))
    sys.exit(0)


if __name__ == "__main__":
    main()

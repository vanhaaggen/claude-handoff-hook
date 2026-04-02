#!/usr/bin/env python3
"""
Claude Code Context Handoff Hook

Fires after every assistant turn (Stop hook).
When context usage crosses THRESHOLD:
  1. Reads the transcript and writes handoff-<YYYYMMDD-HHMM>.md directly.
  2. Injects a system message asking Claude to fill in the Summary section.

Token counting uses the exact usage data already present in the transcript
(same formula as Claude Code internals: tokens.ts:getTokenCountFromUsage).
No API calls, no external libraries needed.

Configuration (env vars):
  HANDOFF_THRESHOLD       Float 0-1, default 0.60  (60%)
  HANDOFF_CONTEXT_WINDOW  Integer, default 200000
"""

import json
import os
import sys
from datetime import datetime
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


# ── Transcript extraction ─────────────────────────────────────────────────────

def content_to_text(content, role: str) -> str:
    """Convert a message content field (str or list of blocks) to plain text."""
    if isinstance(content, str):
        return content

    if not isinstance(content, list):
        return ""

    parts = []
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            text = block.get("text", "").strip()
            if text:
                parts.append(text)
        elif btype == "tool_use":
            name = block.get("name", "unknown")
            inp = block.get("input", {})
            if "command" in inp:
                parts.append(f"[Tool: {name} → {inp['command']}]")
            elif "file_path" in inp:
                parts.append(f"[Tool: {name} → {inp['file_path']}]")
            elif "path" in inp:
                parts.append(f"[Tool: {name} → {inp['path']}]")
            elif "pattern" in inp:
                parts.append(f"[Tool: {name} → {inp['pattern']}]")
            else:
                parts.append(f"[Tool: {name}]")
        # tool_result blocks are skipped — they are verbose and already implied
        # by the tool_use entries above

    return "\n".join(parts)


def extract_conversation(transcript_path: str) -> list:
    """Return a list of {role, text} dicts for user/assistant turns."""
    turns = []

    with open(transcript_path, "r", encoding="utf-8") as f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            try:
                entry = json.loads(raw)
            except json.JSONDecodeError:
                continue

            entry_type = entry.get("type")
            if entry_type not in ("user", "assistant"):
                continue

            model = entry.get("message", {}).get("model", "")
            if model == "synthetic":
                continue

            content = entry.get("message", {}).get("content", "")
            text = content_to_text(content, entry_type)

            if text.strip():
                turns.append({"role": entry_type, "text": text})

    return turns


# ── Handoff file writer ───────────────────────────────────────────────────────

def write_handoff_file(cwd: str, pct: float, tokens_used: int, turns: list) -> Path:
    """Write the handoff markdown file and return its path."""
    timestamp = datetime.now().strftime("%Y%m%d-%H%M")
    filename = f"handoff-{timestamp}.md"
    filepath = Path(cwd) / filename

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [
        f"# Handoff — {now_str}",
        "",
        f"> **Context:** {pct:.0%} used ({tokens_used:,} / {CONTEXT_WINDOW:,} tokens).",
        "> This file was created automatically by the handoff hook.",
        "> Claude: please fill in the Summary section below before ending this session.",
        "",
        "## Summary",
        "",
        "### Goal",
        "",
        "<!-- What are we trying to accomplish? -->",
        "",
        "### Progress",
        "",
        "<!-- What has been analysed, decided, or implemented so far? -->",
        "",
        "### Key Decisions",
        "",
        "<!-- Rationale for important choices made -->",
        "",
        "### Next Steps",
        "",
        "<!-- Concrete actions for a fresh session to continue -->",
        "",
        "### Critical Context",
        "",
        "<!-- File paths, function names, constraints, gotchas -->",
        "",
        "---",
        "",
        "## Conversation Transcript",
        "",
    ]

    for turn in turns:
        role_label = "**User**" if turn["role"] == "user" else "**Assistant**"
        lines.append(f"{role_label}:")
        lines.append("")
        lines.append(turn["text"])
        lines.append("")
        lines.append("---")
        lines.append("")

    filepath.write_text("\n".join(lines), encoding="utf-8")
    return filepath


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    payload = json.loads(sys.stdin.read())

    session_id = payload.get("session_id", "unknown")
    transcript_path = payload.get("transcript_path", "")
    cwd = payload.get("cwd") or os.getcwd()

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

    # Write the handoff file directly — don't rely on Claude to create it
    turns = extract_conversation(transcript_path)
    handoff_path = write_handoff_file(cwd, pct, tokens_used, turns)

    message = (
        f"[HANDOFF HOOK] Context window is at {pct:.0%} "
        f"({tokens_used:,} / {CONTEXT_WINDOW:,} tokens). "
        f"A handoff file has been created at `{handoff_path}`. "
        "Open that file and fill in the Summary section NOW — "
        "Goal, Progress, Key Decisions, Next Steps, and Critical Context. "
        "Do not do anything else first. "
        "After filling in the Summary, tell the user the handoff is ready and suggest: "
        f"'Start a new session and say: Read {handoff_path.name} and continue from there.'"
    )

    print(json.dumps({"systemMessage": message}))
    sys.exit(0)


if __name__ == "__main__":
    main()

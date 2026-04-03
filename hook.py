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
  HANDOFF_THRESHOLD       Float 0-1, default 0.75  (75%)
  HANDOFF_CONTEXT_WINDOW  Integer, default 200000
"""

import json
import os
import sys
from datetime import datetime
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────────

THRESHOLD = float(os.environ.get("HANDOFF_THRESHOLD", "0.75"))
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
        elif btype == "tool_result":
            # Include a truncated snippet so the handoff summary has result context
            result = block.get("content", "")
            if isinstance(result, list):
                result = " ".join(
                    b.get("text", "") for b in result if isinstance(b, dict)
                )
            full = str(result).strip()
            if full:
                parts.append(f"[Result: {full[:200]}{'…' if len(full) > 200 else ''}]")

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
    """Write the handoff markdown file atomically and return its path."""
    timestamp = datetime.now().strftime("%Y%m%d-%H%M")
    filename = f"handoff-{timestamp}.md"
    filepath = Path(cwd) / filename
    tmp_path = filepath.with_suffix(".md.tmp")

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

    tmp_path.write_text("\n".join(lines), encoding="utf-8")
    tmp_path.rename(filepath)
    return filepath


# ── Main ──────────────────────────────────────────────────────────────────────

def prune_state_dir(max_age_days: int = 7) -> None:
    """Remove state files older than max_age_days to prevent unbounded accumulation."""
    if not STATE_DIR.exists():
        return
    cutoff = datetime.now().timestamp() - max_age_days * 86400
    for entry in STATE_DIR.iterdir():
        if entry.is_file() and entry.stat().st_mtime < cutoff:
            entry.unlink(missing_ok=True)


def main() -> None:
    try:
        payload = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, OSError):
        sys.exit(0)

    session_id = payload.get("session_id", "unknown")
    transcript_path = payload.get("transcript_path", "")

    # Use payload cwd; fall back to home dir (os.getcwd() is unreliable in hook subprocesses)
    cwd = payload.get("cwd") or str(Path.home())
    cwd_from_payload = bool(payload.get("cwd"))

    if not transcript_path or not Path(transcript_path).exists():
        sys.exit(0)

    # Fire only once per session
    sf = state_file(session_id)
    if sf.exists():
        sys.exit(0)

    prune_state_dir()

    tokens_used = count_tokens_from_transcript(transcript_path)
    if tokens_used == 0:
        sys.exit(0)

    pct = tokens_used / CONTEXT_WINDOW

    if pct < THRESHOLD:
        sys.exit(0)

    # Write the handoff file first — mark triggered only on success
    turns = extract_conversation(transcript_path)
    handoff_path = write_handoff_file(cwd, pct, tokens_used, turns)

    # Persist state so the hook doesn't fire again this session
    sf.write_text(f"{tokens_used}/{CONTEXT_WINDOW} = {pct:.1%}")

    location_note = (
        ""
        if cwd_from_payload
        else f" (written to home directory {cwd} because project directory was unavailable)"
    )

    message = (
        f"[HANDOFF HOOK] Context window is at {pct:.0%} "
        f"({tokens_used:,} / {CONTEXT_WINDOW:,} tokens). "
        f"A handoff file has been created at `{handoff_path}`{location_note}. "
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

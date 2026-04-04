#!/usr/bin/env python3
"""
Claude Code Context Handoff Hook

Fires after every assistant turn (Stop hook).
When context usage crosses WARN_THRESHOLD (default 60%):
  - Injects a lightweight heads-up message (once per session).
When context usage crosses THRESHOLD (default 75%):
  1. Reads the transcript and writes handoff-<YYYYMMDD-HHMM>.md directly.
  2. Injects a system message asking Claude to fill in the Summary section.

Performance: transcript is reverse-scanned to find the last assistant entry
in O(1) for the common case — no full-file read on every turn.

Context window size is auto-detected from the model name in the transcript,
falling back to HANDOFF_CONTEXT_WINDOW when the model is unknown.

State is guarded by atomic exclusive-create to prevent duplicate handoffs
even if the hook fires concurrently (e.g. rapid Stop events).

Configuration (env vars):
  HANDOFF_THRESHOLD       Float 0-1, default 0.75  (75%)
  HANDOFF_WARN_THRESHOLD  Float 0-1, default 0.60  (60%); set to 0 to disable
  HANDOFF_CONTEXT_WINDOW  Integer, default 200000   (fallback when model unknown)
"""

import json
import os
import sys
from datetime import datetime
from pathlib import Path

HOOK_VERSION = "2.0.0"

# ── Config ────────────────────────────────────────────────────────────────────

THRESHOLD = float(os.environ.get("HANDOFF_THRESHOLD", "0.75"))
WARN_THRESHOLD = float(os.environ.get("HANDOFF_WARN_THRESHOLD", "0.60"))
CONTEXT_WINDOW = int(os.environ.get("HANDOFF_CONTEXT_WINDOW", "200000"))

# Clamp to valid ranges
THRESHOLD = max(0.01, min(1.0, THRESHOLD))
CONTEXT_WINDOW = max(1000, CONTEXT_WINDOW)
WARN_THRESHOLD = max(0.0, min(THRESHOLD - 0.01, WARN_THRESHOLD))

# ── Model → context window map ────────────────────────────────────────────────

# Keyed by model-name prefix. Falls back to CONTEXT_WINDOW for unknown models.
MODEL_CONTEXT_WINDOWS: dict[str, int] = {
    "claude-opus-4":       200_000,
    "claude-sonnet-4":     200_000,
    "claude-haiku-4":      200_000,
    "claude-3-7-sonnet":   200_000,
    "claude-3-5-sonnet":   200_000,
    "claude-3-5-haiku":    200_000,
    "claude-3-opus":       200_000,
    "claude-3-haiku":      200_000,
    "claude-3-sonnet":     200_000,
}


def get_context_window(model: str) -> int:
    """Return the context window for model, falling back to CONTEXT_WINDOW env var."""
    if model:
        for prefix, window in MODEL_CONTEXT_WINDOWS.items():
            if model.startswith(prefix):
                return window
    return CONTEXT_WINDOW


# ── State dir ─────────────────────────────────────────────────────────────────

STATE_DIR = Path.home() / ".claude" / "handoff-hook-state"


def ensure_state_dir() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)


def acquire_once(path: Path) -> bool:
    """
    Atomically create path with exclusive-create (O_EXCL).
    Returns True if this call created the file (we own it),
    False if it already existed (another invocation beat us).
    """
    try:
        path.open("x").close()
        return True
    except FileExistsError:
        return False


def prune_state_dir(max_age_days: int = 7) -> None:
    """Remove state files older than max_age_days."""
    if not STATE_DIR.exists():
        return
    cutoff = datetime.now().timestamp() - max_age_days * 86400
    for entry in STATE_DIR.iterdir():
        if entry.is_file() and entry.stat().st_mtime < cutoff:
            entry.unlink(missing_ok=True)


# ── Token counting — reverse scan ─────────────────────────────────────────────

def find_last_assistant_usage(transcript_path: str) -> tuple[int, str]:
    """
    Reverse-scan the JSONL transcript to find the last non-synthetic assistant
    entry with usage data. Returns (token_count, model_name).

    Reads from the end in 8 KB chunks, so for the common case (last entry is
    the most recent turn) this is effectively O(1) regardless of file size.
    """
    CHUNK = 8192
    with open(transcript_path, "rb") as fh:
        fh.seek(0, 2)
        pos = fh.tell()
        remainder = b""

        while pos > 0:
            step = min(CHUNK, pos)
            pos -= step
            fh.seek(pos)
            block = fh.read(step) + remainder
            lines = block.split(b"\n")

            # lines[0] may be a partial line when not at the file start.
            if pos > 0:
                remainder = lines[0]
                lines = lines[1:]

            for raw in reversed(lines):
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    entry = json.loads(raw)
                except (json.JSONDecodeError, ValueError, UnicodeDecodeError):
                    continue
                if entry.get("type") != "assistant":
                    continue
                msg = entry.get("message", {})
                if msg.get("model") == "synthetic":
                    continue
                usage = msg.get("usage")
                if not usage:
                    continue
                tokens = (
                    usage.get("input_tokens", 0)
                    + usage.get("cache_creation_input_tokens", 0)
                    + usage.get("cache_read_input_tokens", 0)
                    + usage.get("output_tokens", 0)
                )
                return tokens, msg.get("model", "")

    return 0, ""


# ── Transcript extraction ─────────────────────────────────────────────────────

def content_to_text(content) -> str:
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
            text = content_to_text(content)

            if text.strip():
                turns.append({"role": entry_type, "text": text})

    return turns


# ── Handoff file writer ───────────────────────────────────────────────────────

def write_handoff_file(
    cwd: str, pct: float, tokens_used: int, effective_window: int, model: str, turns: list
) -> Path:
    """Write the handoff markdown file atomically and return its path."""
    timestamp = datetime.now().strftime("%Y%m%d-%H%M")
    filename = f"handoff-{timestamp}.md"
    filepath = Path(cwd) / filename
    tmp_path = filepath.with_suffix(".md.tmp")

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    model_note = f" · Model: {model}" if model else ""
    lines = [
        f"# Handoff — {now_str}",
        "",
        f"> **Context:** {pct:.0%} used ({tokens_used:,} / {effective_window:,} tokens{model_note}).",
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

def main() -> None:
    try:
        payload = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, OSError):
        sys.exit(0)

    session_id = payload.get("session_id", "unknown")
    transcript_path = payload.get("transcript_path", "")
    cwd = payload.get("cwd") or str(Path.home())
    cwd_from_payload = bool(payload.get("cwd"))

    if not transcript_path or not Path(transcript_path).exists():
        sys.exit(0)

    # Fast exit: full handoff already done this session (most common path after trigger)
    sf_triggered = STATE_DIR / f"{session_id}.triggered"
    if sf_triggered.exists():
        sys.exit(0)

    # Reverse-scan for token count and model — O(1) for the current turn
    tokens_used, model = find_last_assistant_usage(transcript_path)
    if tokens_used == 0:
        sys.exit(0)

    effective_window = get_context_window(model)
    pct = tokens_used / effective_window

    # Fast exit: below even the lowest active threshold
    lower_bound = WARN_THRESHOLD if WARN_THRESHOLD > 0 else THRESHOLD
    if pct < lower_bound:
        sys.exit(0)

    # From here we need the state directory; also a good time to prune
    ensure_state_dir()
    prune_state_dir()

    if pct >= THRESHOLD:
        # ── Full handoff ───────────────────────────────────────────────────────
        # Atomic exclusive-create: prevents duplicate handoffs if the hook fires
        # concurrently or the process was killed before state was written last time.
        if not acquire_once(sf_triggered):
            sys.exit(0)

        try:
            turns = extract_conversation(transcript_path)
            handoff_path = write_handoff_file(
                cwd, pct, tokens_used, effective_window, model, turns
            )
        except Exception as e:
            # Roll back the lock so the next turn can retry
            sf_triggered.unlink(missing_ok=True)
            print(f"[handoff-hook] Failed to write handoff: {e}", file=sys.stderr)
            sys.exit(0)

        sf_triggered.write_text(
            f"v{HOOK_VERSION} {tokens_used}/{effective_window} = {pct:.1%} model={model}"
        )

        location_note = (
            ""
            if cwd_from_payload
            else f" (written to home directory {cwd} because project directory was unavailable)"
        )

        message = (
            f"[HANDOFF HOOK] Context window is at {pct:.0%} "
            f"({tokens_used:,} / {effective_window:,} tokens). "
            f"A handoff file has been created at `{handoff_path}`{location_note}. "
            "Open that file and fill in the Summary section NOW — "
            "Goal, Progress, Key Decisions, Next Steps, and Critical Context. "
            "Do not do anything else first. "
            "After filling in the Summary, tell the user the handoff is ready and suggest: "
            f"'Start a new session and say: Read {handoff_path.name} and continue from there.'"
        )

    else:
        # ── Early warning (WARN_THRESHOLD <= pct < THRESHOLD) ─────────────────
        sf_warned = STATE_DIR / f"{session_id}.warned"
        if not acquire_once(sf_warned):
            sys.exit(0)

        sf_warned.write_text(
            f"v{HOOK_VERSION} {tokens_used}/{effective_window} = {pct:.1%} model={model}"
        )

        message = (
            f"[HANDOFF HOOK] Heads up: context is at {pct:.0%} "
            f"({tokens_used:,} / {effective_window:,} tokens). "
            f"A handoff document will be prepared automatically at {THRESHOLD:.0%}. "
            "Consider wrapping up long-running tasks soon."
        )

    print(json.dumps({"systemMessage": message}))
    sys.exit(0)


if __name__ == "__main__":
    main()

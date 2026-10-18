"""Claude Code sessions, read from ~/.claude/sessions/<pid>.json (internal format; failures degrade to 'none')."""
import json
import time
from pathlib import Path

import psutil

SESSIONS_DIR = Path.home() / ".claude" / "sessions"
FILETIME_EPOCH_OFFSET = 11644473600  # seconds between 1601-01-01 and 1970-01-01


def filetime_to_epoch(filetime) -> float:
    return int(filetime) / 10_000_000 - FILETIME_EPOCH_OFFSET


def session_status(session: dict, now: float, inactive_after_hours: float) -> str:
    """'active' if busy or recently updated, else 'inactive'."""
    if session.get("status") == "busy":
        return "active"
    idle_s = now - session.get("updated", 0)
    return "active" if idle_s < inactive_after_hours * 3600 else "inactive"


def parse_session(data: dict) -> dict:
    return {
        "pid": int(data["pid"]),
        "id": data.get("sessionId", ""),
        "name": data.get("name") or Path(data.get("cwd", "")).name,
        "cwd": data.get("cwd", ""),
        "status": data.get("status", ""),
        "started": data.get("startedAt", 0) / 1000,
        "updated": data.get("updatedAt", data.get("startedAt", 0)) / 1000,
        "proc_start": filetime_to_epoch(data["procStart"]) if data.get("procStart") else None,
    }


def load_sessions(inactive_after_hours=2, sessions_dir=SESSIONS_DIR) -> dict:
    """Live Claude sessions keyed by claude.exe PID."""
    out = {}
    now = time.time()
    try:
        files = list(Path(sessions_dir).glob("*.json"))
    except OSError:
        return out
    for f in files:
        try:
            s = parse_session(json.loads(f.read_text(encoding="utf-8")))
            proc = psutil.Process(s["pid"])
            # Guard against PID reuse: the process must have started when the session says it did.
            if s["proc_start"] and abs(proc.create_time() - s["proc_start"]) > 5:
                continue
            s["state"] = session_status(s, now, inactive_after_hours)
            try:
                s["mb"] = proc.memory_info().rss / 2**20
            except psutil.Error:
                s["mb"] = 0
            out[s["pid"]] = s
        except (OSError, ValueError, KeyError, psutil.Error):
            continue
    return out

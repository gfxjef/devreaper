"""Who launches processes: short bursts of process-table snapshots, each new PID charged to its launcher.

Polling every 100 ms misses processes that live less than that, so every count is a floor ("at least N per
minute"). A burst lasts a few seconds every few minutes; one snapshot costs ~10 ms and starts nothing.
Command lines are read once per new process, masked (no tokens) and reduced to a signature such as
`git status --porcelain`, so a launcher's repeated polling stands out. They are kept in memory only."""
import os
import re
import subprocess
import time

import psutil

from . import winapi
from .redact import redact

MAX_DEPTH = 12
REPEAT_MIN = 2  # the same command seen this many times in one burst "repeats" (≥ 12/min in a 10 s burst)
# Claude Code's own repo tracking runs git with these overrides; anything else from claude.exe is the user's
# hooks, mods, statusline or tools.
CLAUDE_GIT_MARKS = ("submodule.recurse=false", "gc.auto=0")
GIT_VALUE_OPTS = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path"}
TOKEN_RE = re.compile(r'"[^"]*"|\S+')


def _cmdline(pid):
    try:
        # quoted like Windows does, so "C:\Program Files\…\docker.exe" stays one word
        return redact(subprocess.list2cmdline(psutil.Process(pid).cmdline()))
    except (psutil.Error, OSError):
        return ""


def burst(seconds=10.0, interval=0.1, table_fn=winapi.process_table, cmd_fn=_cmdline, clock=time.monotonic,
          sleep=time.sleep):
    """{'seconds', 'new': {pid: (ppid, name, cmd)}, 'table': {pid: (ppid, name)} of everything seen} or None."""
    table = table_fn()
    if table is None:
        return None
    seen = dict(table)
    new = {}
    start = clock()
    while clock() - start < seconds:
        sleep(interval)
        snap = table_fn()
        if snap is None:
            break
        for pid, entry in snap.items():
            if pid not in seen:
                new[pid] = (entry[0], entry[1], cmd_fn(pid))
            seen[pid] = entry
    return {"seconds": max(clock() - start, interval), "new": new, "table": seen}


def _shape(word):
    """Strip what changes between runs of the same command: paths, numbers, hashes."""
    w = word.strip('"\'')
    if ("\\" in w or "/" in w) and len(w) > 3 and not w.startswith("-"):
        w = os.path.basename(w.rstrip("\\/")) or "…"
    if re.fullmatch(r"[0-9a-f]{12,}", w):
        return "…"
    return re.sub(r"\d+", "N", w)


def signature(name, cmd=""):
    """('git status --porcelain', None) or ('git (seguimiento de Claude Code)', 'claude-git')."""
    stem = name.lower().removesuffix(".exe")
    if not cmd:
        return stem, None
    words = [t.strip('"') for t in TOKEN_RE.findall(cmd)][1:]
    if stem == "git":
        if all(mark in cmd for mark in CLAUDE_GIT_MARKS):
            return "git (seguimiento de Claude Code)", "claude-git"
        out, skip = [], False
        for w in words:
            if skip:
                skip = False
            elif w in GIT_VALUE_OPTS:
                skip = True
            elif not out and w.startswith("-"):
                continue  # global options before the subcommand (-c… --no-pager…)
            else:
                out.append(w)
            if len(out) == 3:
                break
        words = out
    elif stem in ("powershell", "pwsh"):
        low = [w.lower() for w in words]
        at = next((i for i, w in enumerate(low) if w in ("-command", "-c", "-file", "-encodedcommand")), None)
        script = words[at + 1:] if at is not None else words
        words = ["-Command"] + " ".join(script).split()[:3]
    else:
        words = words[:3]
    text = " ".join(s for s in (_shape(w) for w in words) if s)
    return f"{stem} {text}".strip()[:72], None


def owner(pid, table, member_of, limit=MAX_DEPTH):
    """Root PID of the agent among pid's ancestors (or None)."""
    cur = pid
    for _ in range(limit):
        entry = table.get(cur)
        if entry is None:
            return None
        cur = entry[0]
        if cur in member_of:
            return member_of[cur]
        if cur == 0:
            return None
    return None


def attribute(result, member_of, agents, own=frozenset()):
    """Rates per minute: per agent root, per launcher (agent, or parent program) with its commands, and the
    commands that repeat most, heaviest first."""
    if not result:
        return {"at": 0, "seconds": 0, "total": 0, "rate": 0.0, "by_agent": {}, "launchers": [], "repeated": []}
    per_min = 60.0 / result["seconds"]
    table = result["table"]
    by_agent, launchers = {}, {}
    for pid, entry in result["new"].items():
        ppid, name = entry[0], entry[1]
        sig, source = signature(name, entry[2] if len(entry) > 2 else "")
        root = owner(pid, table, member_of)
        if root is not None and root in agents:
            a = agents[root]
            key = ("agent", root)
            label = f"{a.get('agent', 'Agente')} · {a.get('name', root)}"
            by_agent[root] = by_agent.get(root, 0) + 1
        elif ppid in own or pid in own:
            key, label = ("self", 0), "DevReaper"
        else:
            parent = table.get(ppid)
            pname = parent[1] if parent else "(ya cerrado)"
            key, label = ("proc", pname.lower()), pname
        row = launchers.setdefault(key, {"label": label, "count": 0, "children": {}, "sources": {},
                                         "agent_pid": key[1] if key[0] == "agent" else None})
        row["count"] += 1
        row["children"][sig] = row["children"].get(sig, 0) + 1
        if source:
            row["sources"][source] = row["sources"].get(source, 0) + 1
    rows, repeated = [], []
    for row in launchers.values():
        kids = sorted(row["children"].items(), key=lambda kv: -kv[1])
        rows.append({"label": row["label"], "agent_pid": row["agent_pid"],
                     "rate": round(row["count"] * per_min, 1),
                     "claude_git_rate": round(row["sources"].get("claude-git", 0) * per_min, 1),
                     "children": [{"name": n, "rate": round(c * per_min, 1)} for n, c in kids[:4]]})
        repeated += [{"cmd": n, "launcher": row["label"], "agent_pid": row["agent_pid"],
                      "rate": round(c * per_min, 1)} for n, c in kids if c >= REPEAT_MIN]
    rows.sort(key=lambda r: -r["rate"])
    repeated.sort(key=lambda r: -r["rate"])
    total = len(result["new"])
    return {"at": time.time(), "seconds": round(result["seconds"], 1), "total": total,
            "rate": round(total * per_min, 1),
            "by_agent": {pid: round(c * per_min, 1) for pid, c in by_agent.items()},
            "launchers": rows[:12], "repeated": repeated[:10]}

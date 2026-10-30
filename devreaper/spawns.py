"""Who launches processes: short bursts of process-table snapshots, each new PID charged to its launcher.

Polling every 100 ms misses processes that live less than that, so every count is a floor ("at least N per
minute"). A burst lasts a few seconds every few minutes; one snapshot costs ~10 ms and starts nothing."""
import time

from . import winapi

MAX_DEPTH = 12


def burst(seconds=10.0, interval=0.1, table_fn=winapi.process_table, clock=time.monotonic, sleep=time.sleep):
    """{'seconds', 'new': {pid: (ppid, name)}, 'table': {pid: (ppid, name)} of everything seen} or None."""
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
                new[pid] = entry
            seen[pid] = entry
    return {"seconds": max(clock() - start, interval), "new": new, "table": seen}


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
    """Rates per minute: per agent root and per launcher (agent, or parent program), heaviest first."""
    if not result:
        return {"at": 0, "seconds": 0, "total": 0, "rate": 0.0, "by_agent": {}, "launchers": []}
    per_min = 60.0 / result["seconds"]
    table = result["table"]
    by_agent, launchers = {}, {}
    for pid, (ppid, name) in result["new"].items():
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
        row = launchers.setdefault(key, {"label": label, "count": 0, "children": {},
                                         "agent_pid": key[1] if key[0] == "agent" else None})
        row["count"] += 1
        row["children"][name] = row["children"].get(name, 0) + 1
    rows = []
    for row in launchers.values():
        kids = sorted(row["children"].items(), key=lambda kv: -kv[1])[:4]
        rows.append({"label": row["label"], "agent_pid": row["agent_pid"],
                     "rate": round(row["count"] * per_min, 1),
                     "children": [{"name": n, "rate": round(c * per_min, 1)} for n, c in kids]})
    rows.sort(key=lambda r: -r["rate"])
    total = len(result["new"])
    return {"at": time.time(), "seconds": round(result["seconds"], 1), "total": total,
            "rate": round(total * per_min, 1),
            "by_agent": {pid: round(c * per_min, 1) for pid, c in by_agent.items()},
            "launchers": rows[:12]}

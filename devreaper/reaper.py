"""Killing process trees and the auto-close policy."""
import time

import psutil


def keep_reason(group, max_age_hours, excluded_ports, now=None):
    """Why the auto-close cycle spares this group, or None when it is due. Anything that suggests the user is
    still relying on it wins: a protected port, an active agent, recent CPU use, or a postponed close."""
    now = now or time.time()
    if set(group["ports"]) & set(excluded_ports):
        return "protegido"
    if group.get("keep_until", 0) > now:
        return "pospuesto"
    if group.get("state") == "active":
        return "agente activo"
    if group.get("in_use"):
        return "en uso"
    if group["age_hours"] < max_age_hours:
        return "reciente"
    return None


def due_for_auto(groups, max_age_hours, excluded_ports, now=None):
    """Groups the auto-close cycle should kill."""
    return [g for g in groups if keep_reason(g, max_age_hours, excluded_ports, now) is None]


def orphans(groups, excluded_ports, now=None):
    now = now or time.time()
    return [g for g in groups if g["state"] == "orphan" and not set(g["ports"]) & set(excluded_ports)
            and g.get("keep_until", 0) <= now]


def kill_group(group, timeout=3.0) -> int:
    """Kill the group's root and every descendant. Returns how many processes were killed."""
    try:
        root = psutil.Process(group["root_pid"])
        if abs(root.create_time() - group["create_time"]) > 1:
            return 0  # PID was reused by another process; do not touch it
        targets = [root] + root.children(recursive=True)
    except psutil.Error:
        targets = []
    for pid in group["pids"]:  # members whose link to the root broke
        if all(t.pid != pid for t in targets):
            try:
                targets.append(psutil.Process(pid))
            except psutil.Error:
                pass
    return _kill_all(targets, timeout)


def kill_tree(root, skip=frozenset(), timeout=3.0) -> int:
    """Kill `root` and its descendants except PIDs in `skip`. On Windows a child survives its parent's death,
    so skipped processes keep running."""
    try:
        targets = [root] + [c for c in root.children(recursive=True) if c.pid not in skip]
    except psutil.Error:
        targets = [root]
    return _kill_all(targets, timeout)


def _kill_all(targets, timeout):
    for t in reversed(targets):  # children first so they cannot respawn
        try:
            t.kill()
        except psutil.Error:
            pass
    gone, _ = psutil.wait_procs(targets, timeout=timeout)
    return len(gone)


def _current_user():
    try:
        return psutil.Process().username().lower()
    except psutil.Error:
        return None


def ensure_user_process(proc):
    """Refuse to kill anything not owned by this user (Windows services, security agents, other accounts)."""
    try:
        owner = proc.username().lower()
    except psutil.AccessDenied:
        owner = None  # access denied on username means it runs as SYSTEM or another account
    if owner is None or owner != _current_user():
        raise ValueError(f"{proc.name()} (PID {proc.pid}) no es un proceso tuyo; DevReaper no lo cierra")


def pid_on_port(port: int):
    for c in psutil.net_connections(kind="tcp"):
        if c.status == psutil.CONN_LISTEN and c.laddr.port == port and c.pid:
            return c.pid
    return None

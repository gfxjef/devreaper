"""Coding agents (Claude Code, Codex, Gemini CLI, Cursor, VS Code, JetBrains…) found in the process tree.

The process tree is the source of truth: a dev server belongs to the first agent among its ancestors, whatever
files that agent keeps on disk. Session files and transcripts only add detail (name, last activity) and every
reader degrades to None when a format changes. When nothing on disk says how recently an agent was used, its
CPU time decides: a CLI waiting for input burns ~1 % of a core, one that is working burns several times more."""
import json
import os
import re
import time
from datetime import datetime
from pathlib import Path

import psutil

HOME = Path.home()
CLAUDE_PROJECTS = HOME / ".claude" / "projects"
CODEX_SESSIONS = HOME / ".codex" / "sessions"

# CPU share of one core, averaged over >= ACTIVITY_WINDOW_S, above which an agent counts as being used
# (only for agents that leave no session file or transcript to read).
CPU_ACTIVE = {"cli": 0.03, "app": 0.06, "ide": 0.10}
ACTIVITY_WINDOW_S = 60  # long enough that a single background burst averages out
RESUME_RE = re.compile(r"--(?:resume|session-id)[ =]([0-9a-f-]{36})", re.I)

JETBRAINS = {"idea64": "IntelliJ IDEA", "pycharm64": "PyCharm", "webstorm64": "WebStorm", "rider64": "Rider",
             "goland64": "GoLand", "rubymine64": "RubyMine", "phpstorm64": "PhpStorm", "clion64": "CLion",
             "datagrip64": "DataGrip", "rustrover64": "RustRover", "studio64": "Android Studio"}
IDES = {"cursor": "Cursor", "code": "VS Code", "code - insiders": "VS Code Insiders", "windsurf": "Windsurf",
        "kiro": "Kiro", "trae": "Trae", "zed": "Zed", "antigravity": "Antigravity", "devenv": "Visual Studio",
        "void": "Void", "positron": "Positron"}
NATIVE_CLIS = {"cursor-agent": "Cursor Agent", "opencode": "OpenCode", "goose": "Goose", "aider": "Aider",
               "gemini": "Gemini CLI", "copilot": "Copilot CLI", "amp": "Amp", "qwen": "Qwen Code",
               "crush": "Crush", "kilo": "Kilo Code", "droid": "Factory Droid", "auggie": "Auggie"}
# Agents that run on a script host (node, bun, python): recognised by their package in the command line.
SCRIPT_CLIS = [("@anthropic-ai/claude-code", "claude", "Claude Code"), ("claude-code/cli", "claude", "Claude Code"),
               ("@openai/codex", "codex", "Codex"), ("@google/gemini-cli", "gemini", "Gemini CLI"),
               ("@github/copilot", "copilot", "Copilot CLI"), ("@sourcegraph/amp", "amp", "Amp"),
               ("@qwen-code/", "qwen", "Qwen Code"), ("@augmentcode/auggie", "auggie", "Auggie"),
               ("opencode-ai", "opencode", "OpenCode"), ("@kilocode/cli", "kilo", "Kilo Code"),
               ("@charmland/crush", "crush", "Crush")]
SCRIPT_HOSTS = {"node", "bun", "deno", "python", "pythonw", "py"}


def classify(name: str, exe: str, cmd: str):
    """(key, display name, kind) when this process is a coding agent, else None. kind: cli | ide | app."""
    stem = (name or "").lower().removesuffix(".exe")
    exe_l = (exe or "").lower()
    cmd_l = (cmd or "").lower().replace("\\", "/")  # npm paths on Windows use backslashes
    if stem == "claude":
        if "\\anthropicclaude\\" in exe_l or "\\windowsapps\\" in exe_l or "\\claude\\app-" in exe_l:
            return ("claude-desktop", "Claude (escritorio)", "app")
        return ("claude", "Claude Code", "cli")
    if stem.startswith("codex"):
        return ("codex-desktop", "Codex (escritorio)", "app") if "\\windowsapps\\" in exe_l or "\\programs\\codex" in exe_l \
            else ("codex", "Codex", "cli")
    if stem in SCRIPT_HOSTS:
        for needle, key, display in SCRIPT_CLIS:
            if needle in cmd_l:
                return (key, display, "cli")
        if stem.startswith("py") and re.search(r"[/ ]aider(\.exe|\b)", cmd_l):
            return ("aider", "Aider", "cli")
        return None
    if stem in IDES:
        return (stem, IDES[stem], "ide")
    if stem in JETBRAINS and "jetbrains" in exe_l:
        return (stem, JETBRAINS[stem], "ide")
    if stem in NATIVE_CLIS:
        return (stem, NATIVE_CLIS[stem], "cli")
    return None


def _parent(rec, procs):
    p = procs.get(rec["ppid"])
    if p is None or p["pid"] == rec["pid"] or p["create_time"] > rec["create_time"] + 1:
        return None
    return p


def _details(rec):
    p = rec["proc"]
    if "cmd" not in rec:
        try:
            rec["cmd"] = " ".join(p.cmdline())
        except psutil.Error:
            rec["cmd"] = ""
    if "exe" not in rec:
        try:
            rec["exe"] = p.exe()
        except psutil.Error:
            rec["exe"] = ""
    return rec


_class_cache = {}


def classify_rec(rec):
    """classify() with a per-process cache: command lines are only read for names that can be agents."""
    key = (rec["pid"], rec["create_time"])
    if key in _class_cache:
        return _class_cache[key]
    stem = rec["name"].lower().removesuffix(".exe")
    out = None
    if stem in SCRIPT_HOSTS or stem == "claude" or stem.startswith("codex") or stem in IDES or stem in JETBRAINS \
            or stem in NATIVE_CLIS:
        _details(rec)
        out = classify(rec["name"], rec["exe"], rec["cmd"])
    if len(_class_cache) > 20000:
        _class_cache.clear()
    _class_cache[key] = out
    return out


def find_agents(procs, user=None):
    """{pid: agent} for each agent root plus {pid: root pid} for every agent process (helpers included).
    A root is the topmost process of a run of same-agent processes (Cursor.exe main over its helpers,
    the node wrapper over codex.exe)."""
    member_of = {}
    tagged = {pid: classify_rec(rec) for pid, rec in procs.items()}
    roots = {}
    for pid, info in tagged.items():
        if not info:
            continue
        rec = procs[pid]
        root = rec
        while True:
            parent = _parent(root, procs)
            if parent is None or not tagged.get(parent["pid"]) or tagged[parent["pid"]][0] != info[0]:
                break
            root = parent
        member_of[pid] = root["pid"]
        roots.setdefault(root["pid"], info)
    agents = {}
    for pid, (key, display, kind) in roots.items():
        rec = procs[pid]
        if user:
            try:
                if rec["proc"].username().lower() != user:
                    continue
            except psutil.Error:
                continue
        _details(rec)
        agents[pid] = {"pid": pid, "create_time": rec["create_time"], "agent_key": key, "agent": display,
                       "kind": kind, "cmd": rec["cmd"][:300], "exe": rec["exe"]}
    member_of = {p: r for p, r in member_of.items() if r in agents}
    return agents, member_of


def agent_cwd(pid):
    try:
        return psutil.Process(pid).cwd()
    except psutil.Error:
        return ""


# ---------- last activity from what agents leave on disk ----------
def claude_project_dir(cwd: str) -> Path:
    """~/.claude/projects/<cwd with every non-alphanumeric character turned into '-'>."""
    return CLAUDE_PROJECTS / re.sub(r"[^A-Za-z0-9]", "-", cwd or "")


def last_turn(path: Path, tail_bytes=262144):
    """Timestamp of the last user/assistant entry in a JSONL transcript. File mtimes lie: Claude Code appends
    bookkeeping lines (bridge-session, last-prompt) to idle sessions, so only real turns count."""
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - tail_bytes))
            lines = fh.read().decode("utf-8", errors="replace").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        if '"timestamp"' not in line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if obj.get("type") in ("user", "assistant") and isinstance(obj.get("timestamp"), str):
            try:
                return datetime.fromisoformat(obj["timestamp"].replace("Z", "+00:00")).timestamp()
            except ValueError:
                return None
    return None


def claude_transcript(cwd: str, cmd: str, session_id: str = ""):
    """The session's transcript: by its id (session file or --resume), else the newest one for its folder."""
    sid = session_id or (RESUME_RE.search(cmd or "") or [None, None])[1]
    try:
        if sid:
            direct = claude_project_dir(cwd) / f"{sid}.jsonl"
            if direct.is_file():
                return direct
            hit = next(iter(CLAUDE_PROJECTS.glob(f"*/{sid}.jsonl")), None)
            if hit:
                return hit
        if cwd:
            files = list(claude_project_dir(cwd).glob("*.jsonl"))
            return max(files, key=lambda f: f.stat().st_mtime) if files else None
    except OSError:
        return None
    return None


def claude_last_turn(cwd: str, cmd: str, session_id: str = ""):
    path = claude_transcript(cwd, cmd, session_id)
    return last_turn(path) if path else None


def _find_cwd(obj, depth=0):
    if depth > 4:
        return None
    if isinstance(obj, dict):
        if isinstance(obj.get("cwd"), str):
            return obj["cwd"]
        for v in obj.values():
            found = _find_cwd(v, depth + 1)
            if found:
                return found
    return None


def codex_activity(days=2, limit=60):
    """{normalized cwd: newest transcript mtime} from ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl."""
    out = {}
    try:
        files = []
        for d in range(days):
            day = time.localtime(time.time() - d * 86400)
            folder = CODEX_SESSIONS / f"{day.tm_year:04d}" / f"{day.tm_mon:02d}" / f"{day.tm_mday:02d}"
            if folder.is_dir():
                files.extend(folder.glob("*.jsonl"))
        files = sorted(files, key=lambda f: f.stat().st_mtime, reverse=True)[:limit]
    except OSError:
        return out
    for f in files:
        try:
            with open(f, encoding="utf-8") as fh:
                cwd = _find_cwd(json.loads(fh.readline()))
            mtime = f.stat().st_mtime
        except (OSError, ValueError):
            continue
        if cwd:
            key = os.path.normcase(os.path.normpath(cwd))
            out[key] = max(out.get(key, 0), mtime)
    return out


class Activity:
    """Remembers when each process tree last burned more CPU than its idle threshold."""

    def __init__(self, now=None):
        self.started = time.time() if now is None else now
        self._mark, self._prev, self._last = {}, {}, {}

    def observe(self, key, cpu_total, threshold, now=None):
        """Feed the tree's cumulative CPU seconds. Returns the CPU share (of one core) since the previous call."""
        now = time.time() if now is None else now
        prev = self._prev.get(key)
        self._prev[key] = (now, cpu_total)
        share = (cpu_total - prev[1]) / (now - prev[0]) if prev and now > prev[0] else 0.0
        mark = self._mark.get(key)
        if mark is None:
            self._mark[key] = (now, cpu_total)
        elif now - mark[0] >= ACTIVITY_WINDOW_S:
            if (cpu_total - mark[1]) / (now - mark[0]) >= threshold:
                self._last[key] = now
            self._mark[key] = (now, cpu_total)
        return max(share, 0.0)

    def last_active(self, key):
        return self._last.get(key)

    def forget(self, live_keys):
        for d in (self._mark, self._prev, self._last):
            for k in [k for k in d if k not in live_keys]:
                del d[k]


def tree_cpu(pids, procs):
    total = 0.0
    for pid in pids:
        rec = procs.get(pid)
        if rec is None:
            continue
        try:
            t = rec["proc"].cpu_times()
            total += t.user + t.system
        except psutil.Error:
            pass
    return total


def tree_mb(pids, procs):
    total = 0.0
    for pid in pids:
        rec = procs.get(pid)
        if rec is None:
            continue
        try:
            total += rec["proc"].memory_info().rss / 2**20
        except psutil.Error:
            pass
    return total


def agent_state(last_activity, now, inactive_after_hours, observing_since, busy=False):
    """'active' when used within the window. With no evidence yet, stay 'active' until DevReaper has watched for
    a full window: when in doubt, nothing gets auto-closed."""
    window = inactive_after_hours * 3600
    if busy or (last_activity and now - last_activity < window):
        return "active"
    if last_activity is None and now - observing_since < window:
        return "active"
    return "inactive"

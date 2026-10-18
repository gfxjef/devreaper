"""Where the user works, found without configuration: the folders coding agents run in now, plus the project
history agents and editors keep (Claude Code, Gemini CLI, Codex, VS Code-family workspaces). Every reader is
best effort: a missing or changed file just contributes nothing."""
import json
import os
import time
from pathlib import Path
from urllib.parse import unquote, urlsplit

from . import agents

HOME = Path.home()
HISTORY_DAYS = 30
EDITORS = ("Cursor", "Code", "Code - Insiders", "Windsurf", "VSCodium", "Kiro", "Trae", "Positron")


def _norm(path):
    return os.path.normcase(os.path.normpath(path)) if path else ""


def claude_projects():
    try:
        data = json.loads((HOME / ".claude.json").read_text(encoding="utf-8"))
        return [str(Path(p)) for p in (data.get("projects") or {})]
    except (OSError, ValueError, AttributeError):
        return []


def gemini_projects():
    try:
        data = json.loads((HOME / ".gemini" / "projects.json").read_text(encoding="utf-8"))
        return list((data.get("projects") or {}))
    except (OSError, ValueError, AttributeError):
        return []


def codex_projects():
    return list(agents.codex_activity(days=HISTORY_DAYS, limit=400))


def editor_workspaces():
    """Folders opened in VS Code-family editors in the last HISTORY_DAYS (workspaceStorage/*/workspace.json)."""
    out, cutoff = [], time.time() - HISTORY_DAYS * 86400
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return out
    for editor in EDITORS:
        storage = Path(appdata) / editor / "User" / "workspaceStorage"
        try:
            entries = list(os.scandir(storage))
        except OSError:
            continue
        for e in entries:
            try:
                if e.stat().st_mtime < cutoff:
                    continue
                folder = json.loads((Path(e.path) / "workspace.json").read_text(encoding="utf-8")).get("folder")
            except (OSError, ValueError, AttributeError):
                continue
            if isinstance(folder, str) and folder.startswith("file:"):
                path = unquote(urlsplit(folder).path).lstrip("/")
                out.append(os.path.normpath(path))
    return out


def history_dirs():
    return claude_projects() + gemini_projects() + codex_projects() + editor_workspaces()


def skip_dir(path):
    """Home, drive roots, Windows and temp folders are never projects."""
    n = _norm(path)
    if not n or n == _norm(str(HOME)) or len(n) <= 3:
        return True
    for env in ("SystemRoot", "TEMP", "ProgramFiles", "ProgramFiles(x86)"):
        base = _norm(os.environ.get(env, ""))
        if base and (n == base or n.startswith(base + os.sep)):
            return True
    return False


def git_repo_of(path, max_up=12):
    """Main repo root for any folder inside a repo or one of its linked worktrees, else None."""
    if not path or skip_dir(path):
        return None
    cur = Path(path)
    for _ in range(max_up):
        git = cur / ".git"
        try:
            if git.is_dir():
                return None if skip_dir(str(cur)) else os.path.normpath(str(cur))
            if git.is_file():
                text = git.read_text(encoding="utf-8", errors="replace").strip()
                gitdir = text.removeprefix("gitdir:").strip().replace("/", os.sep)
                marker = f"{os.sep}.git{os.sep}worktrees{os.sep}"
                if marker in gitdir:
                    return os.path.normpath(gitdir.split(marker)[0])
                return os.path.normpath(str(cur))
        except OSError:
            return None
        if cur.parent == cur:
            return None
        cur = cur.parent
    return None


def discover(paths):
    """Repo roots for the given folders (deduplicated, existing only)."""
    repos = {}
    for p in paths:
        repo = git_repo_of(p)
        if repo and os.path.isdir(repo):
            repos.setdefault(_norm(repo), os.path.realpath(repo))  # realpath restores the folder's real casing
    return set(repos.values())

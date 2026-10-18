"""Git worktree map: which worktrees exist, their branch, and whether everything in them is already in main."""
import os
import re
import shutil
from pathlib import Path

from . import quiet

SKIP_DIRS = {"node_modules", "venv", "env", "__pycache__", "dist", "build", "site-packages", "target"}
WORKTREE_RE = re.compile(r"[\\/]\.worktrees[\\/]([^\\/\"\s]+)", re.I)
DRIVE_RE = re.compile(r"[A-Za-z]:[\\/]")


def norm(path: str) -> str:
    return os.path.normcase(os.path.normpath(path)) if path else ""


def worktree_of(text: str):
    """Full path of the `.worktrees\\<name>` folder mentioned in a path or command line, or None."""
    if not text:
        return None
    m = WORKTREE_RE.search(text)
    if not m:
        return None
    drives = [d.start() for d in DRIVE_RE.finditer(text, 0, m.start())]
    if not drives:
        return None
    return os.path.normpath(text[drives[-1]:m.end()])


def repo_of(worktree_path: str):
    m = WORKTREE_RE.search(worktree_path or "")
    return os.path.normpath(worktree_path[:m.start()]) if m else None


def find_repos(roots, max_depth=4):
    """Repos under `roots` that have linked worktrees (.git/worktrees exists). Never descends into a repo."""
    repos = set()
    for root in roots:
        stack = [(Path(root), 0)]
        while stack:
            folder, depth = stack.pop()
            try:
                entries = list(os.scandir(folder))
            except OSError:
                continue
            git = folder / ".git"
            if any(e.name == ".git" for e in entries):
                if (git / "worktrees").is_dir():
                    repos.add(os.path.normpath(str(folder)))
                continue
            if depth >= max_depth:
                continue
            for e in entries:
                try:
                    is_dir = e.is_dir(follow_symlinks=False)
                except OSError:
                    continue
                if is_dir and not e.name.startswith(".") and e.name.lower() not in SKIP_DIRS:
                    stack.append((Path(e.path), depth + 1))
    return repos


def parse_worktree_list(text: str):
    """Parse `git worktree list --porcelain`."""
    out, cur = [], None
    for line in text.splitlines():
        line = line.strip()
        if not line:
            if cur:
                out.append(cur)
            cur = None
            continue
        key, _, value = line.partition(" ")
        if key == "worktree":
            if cur:
                out.append(cur)
            cur = {"path": os.path.normpath(value), "branch": None, "detached": False, "prunable": False}
        elif cur is None:
            continue
        elif key == "branch":
            cur["branch"] = value.removeprefix("refs/heads/")
        elif key == "detached":
            cur["detached"] = True
        elif key == "prunable":
            cur["prunable"] = True
    if cur:
        out.append(cur)
    return out


def parse_refs(text: str):
    """Parse `git for-each-ref --format=%(refname:short)|%(upstream:track)|%(committerdate:unix) refs/heads`."""
    refs = {}
    for line in text.splitlines():
        parts = line.strip().split("|")
        if len(parts) != 3 or not parts[0]:
            continue
        name, track, date = parts
        refs[name] = {"gone": "gone" in track, "date": int(date) if date.isdigit() else 0}
    return refs


def default_base(repo, git):
    head = quiet.run([git, "-C", repo, "symbolic-ref", "--short", "refs/remotes/origin/HEAD"], timeout=5).strip()
    if head:
        return head
    for name in ("main", "master", "develop"):
        if quiet.run([git, "-C", repo, "rev-parse", "--verify", "--quiet", name], timeout=5).strip():
            return name
    return None


def classify(wt, base, merged, refs, is_main):
    if wt["prunable"] or not os.path.isdir(wt["path"]):
        return "missing"
    if is_main or wt["detached"] or not wt["branch"]:
        return "main" if is_main else "detached"
    if base and wt["branch"] in merged:
        return "merged"
    if refs.get(wt["branch"], {}).get("gone"):
        return "gone"
    return "open"


def plain_repo(repo, git):
    """A repo without linked worktrees: one row, branch from .git/HEAD, a single git call for the date."""
    try:
        head = (Path(repo) / ".git" / "HEAD").read_text(encoding="utf-8").strip()
    except OSError:
        return []
    branch = head.removeprefix("ref: refs/heads/") if head.startswith("ref:") else None
    date = quiet.run([git, "-C", repo, "log", "-1", "--format=%ct"], timeout=5).strip()
    return [{"repo": repo, "repo_name": os.path.basename(repo), "path": repo, "name": "(principal)",
             "branch": branch, "base": None, "status": "main", "last_commit": int(date) if date.isdigit() else 0}]


def repo_worktrees(repo, git):
    if not (Path(repo) / ".git" / "worktrees").is_dir():
        return plain_repo(repo, git)
    listing = quiet.run([git, "-C", repo, "worktree", "list", "--porcelain"])
    if not listing:
        return []
    refs = parse_refs(quiet.run([git, "-C", repo, "for-each-ref",
                                 "--format=%(refname:short)|%(upstream:track)|%(committerdate:unix)", "refs/heads"]))
    base = default_base(repo, git)
    merged = set()
    if base:
        merged = set(quiet.run([git, "-C", repo, "branch", "--merged", base,
                                "--format=%(refname:short)"]).split())
    rows = []
    for i, wt in enumerate(parse_worktree_list(listing)):
        is_main = i == 0
        rows.append({
            "repo": repo,
            "repo_name": os.path.basename(repo),
            "path": wt["path"],
            "name": os.path.basename(wt["path"]) if not is_main else "(principal)",
            "branch": wt["branch"],
            "base": base,
            "status": classify(wt, base, merged, refs, is_main),
            "last_commit": refs.get(wt["branch"] or "", {}).get("date", 0),
        })
    return rows


def scan_worktrees(roots, extra_paths=(), repos=()):
    """All worktrees of: repos with linked worktrees under `roots`, repos mentioned by running processes,
    and `repos` (discovered from where agents work)."""
    git = shutil.which("git")
    if not git:
        return {"rows": [], "error": "No encontré git en el PATH."}
    found = {norm(r): r for r in find_repos(roots)}
    for r in repos:
        found.setdefault(norm(r), r)
    for p in extra_paths:
        wt = worktree_of(p)
        if wt and repo_of(wt):
            found.setdefault(norm(repo_of(wt)), repo_of(wt))
    rows = []
    for repo in sorted(found.values(), key=str.lower):
        rows.extend(repo_worktrees(repo, git))
    return {"rows": rows, "error": None}

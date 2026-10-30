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


class GitDir:
    """Reads a repo's refs, worktrees, upstreams and reflogs straight from its .git folder: no git process."""

    def __init__(self, repo):
        self.repo = repo
        dot = Path(repo) / ".git"
        self.common = dot
        if dot.is_file():  # `.git` file (submodule, --separate-git-dir): follow it
            text = _read(dot)
            if text.startswith("gitdir:"):
                self.common = Path(repo, text.removeprefix("gitdir:").strip())
        self._packed = None

    def packed(self):
        if self._packed is None:
            self._packed = {}
            for line in _read(self.common / "packed-refs").splitlines():
                if line and line[0] not in "#^":
                    sha, _, name = line.partition(" ")
                    self._packed[name.strip()] = sha
        return self._packed

    def ref(self, name, depth=5):
        """SHA a ref points to (following symbolic refs), or None."""
        text = _read(self.common / name)
        if text.startswith("ref:"):
            return self.ref(text.removeprefix("ref:").strip(), depth - 1) if depth else None
        return text or self.packed().get(name)

    def has_ref(self, name):
        return (self.common / name).is_file() or name in self.packed()

    def heads(self):
        """{branch: sha} for every local branch."""
        out = {name.removeprefix("refs/heads/"): sha for name, sha in self.packed().items()
               if name.startswith("refs/heads/")}
        root = self.common / "refs" / "heads"
        for folder, _, files in os.walk(root):
            for f in files:
                name = Path(folder, f).relative_to(root).as_posix()
                out[name] = _read(Path(folder, f)) or out.get(name)
        return out

    def upstreams(self):
        """{branch: (remote, merge ref)} from .git/config."""
        cfg, branch = {}, None
        for line in _read(self.common / "config").splitlines():
            line = line.strip()
            m = re.match(r'\[branch\s+"(.+)"\]', line)
            if m:
                branch = m.group(1)
            elif line.startswith("["):
                branch = None
            elif branch and "=" in line:
                key, _, value = line.partition("=")
                cfg.setdefault(branch, {})[key.strip().lower()] = value.strip()
        return {b: (c["remote"], c["merge"]) for b, c in cfg.items() if c.get("remote") and c.get("merge")}

    def gone(self, branch, upstreams):
        """Upstream configured but its remote-tracking ref is gone (`git branch -vv` shows [gone])."""
        up = upstreams.get(branch)
        if not up or up[0] == ".":
            return False
        return not self.has_ref(f"refs/remotes/{up[0]}/{up[1].removeprefix('refs/heads/')}")

    def changed_at(self, ref, logs=None):
        """When a ref last moved, from its reflog (0 if there is none)."""
        last = _last_line(Path(logs or self.common / "logs") / ref)
        try:
            return int(last.partition("\t")[0].rsplit(" ", 2)[-2]) if last else 0
        except (IndexError, ValueError):
            return 0

    def tip(self, wt):
        """SHA checked out in a worktree (its branch's tip, or the detached HEAD)."""
        if wt["branch"]:
            return self.ref(f"refs/heads/{wt['branch']}")
        text = _read(wt["admin"] / "HEAD")
        return text if re.fullmatch(r"[0-9a-f]{40,64}", text) else None

    def fallback_time(self, branch, admin):
        """When a worktree last moved, for tips git cannot date: branch reflog, then the worktree's HEAD reflog."""
        return (self.changed_at(f"refs/heads/{branch}") if branch else 0) or self.changed_at("HEAD", admin / "logs")


    def worktrees(self):
        """[{path, branch, detached, prunable, admin}] main checkout first, like `git worktree list`."""
        out = [dict(_head(_read(self.common / "HEAD")), path=os.path.normpath(self.repo), prunable=False,
                    admin=self.common)]
        try:
            entries = sorted(os.scandir(self.common / "worktrees"), key=lambda e: e.name)
        except OSError:
            entries = []
        for e in entries:
            admin = Path(e.path)
            pointer = _read(admin / "gitdir")
            if not pointer:
                continue
            dot_git = Path(pointer) if os.path.isabs(pointer) else admin / pointer
            out.append(dict(_head(_read(admin / "HEAD")), path=os.path.normpath(str(dot_git.parent)),
                            prunable=not dot_git.exists(), admin=admin))
        return out

    def default_base(self):
        text = _read(self.common / "refs" / "remotes" / "origin" / "HEAD")
        if text.startswith("ref: refs/remotes/"):
            return text.removeprefix("ref: refs/remotes/")
        for name in ("main", "master", "develop"):
            if self.has_ref(f"refs/heads/{name}"):
                return name
        return None

    def base_sha(self, base):
        return self.ref(f"refs/remotes/{base}") if "/" in base else self.ref(f"refs/heads/{base}")


# commit SHA -> committer time. A commit's date never changes, so each SHA is asked to git once.
_commit_times = {}


def commit_times(repo, shas, git):
    """{sha: unix time} for `shas`; the unknown ones in a single `git show -s` call."""
    missing = sorted({s for s in shas if s and s not in _commit_times})
    if missing and git:
        out = quiet.run([git, "-C", repo, "show", "-s", "--format=%H %ct", *missing], timeout=15)
        for line in out.splitlines():
            sha, _, ts = line.partition(" ")
            if ts.strip().isdigit():
                _commit_times[sha] = int(ts)
        for sha in missing:
            _commit_times.setdefault(sha, 0)  # unknown to git (pruned): don't ask again
    if len(_commit_times) > 20000:
        _commit_times.clear()
    return {s: _commit_times.get(s, 0) for s in shas if s}


def _read(path):
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


def _last_line(path, tail=4096):
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(f.tell() - tail, 0))
            lines = f.read().decode("utf-8", errors="replace").strip().splitlines()
    except OSError:
        return ""
    return lines[-1] if lines else ""


def _head(text):
    if text.startswith("ref: refs/heads/"):
        return {"branch": text.removeprefix("ref: refs/heads/"), "detached": False}
    return {"branch": None, "detached": bool(text)}


def classify(wt, base, merged, gone, is_main):
    if wt["prunable"] or not os.path.isdir(wt["path"]):
        return "missing"
    if is_main or wt["detached"] or not wt["branch"]:
        return "main" if is_main else "detached"
    if base and wt["branch"] in merged:
        return "merged"
    if gone:
        return "gone"
    return "open"


# repo -> ((base sha, branch tips), merged branches): `git branch --merged` runs only after a ref moved
_merged_cache = {}


def merged_branches(g, base, heads, candidates, git):
    """Branches among `candidates` already contained in `base`. A tip equal to the base needs no git; the rest
    take one `git branch --merged` call, cached until the base or one of those tips moves."""
    base_sha = g.base_sha(base)
    if not base_sha:
        return set()
    merged = {b for b in candidates if heads.get(b) == base_sha}
    pending = [b for b in candidates if b not in merged and heads.get(b)]
    if not pending or not git:
        return merged
    key = (base_sha, tuple(sorted((b, heads[b]) for b in pending)))
    hit = _merged_cache.get(norm(g.repo))
    if hit is None or hit[0] != key:
        out = quiet.run([git, "-C", g.repo, "branch", "--merged", base, "--format=%(refname:short)"])
        hit = (key, set(out.split()))
        _merged_cache[norm(g.repo)] = hit
    return merged | (hit[1] & set(pending))


def repo_worktrees(repo, git):
    g = GitDir(repo)
    if not (g.common / "HEAD").is_file():
        return []
    wts = g.worktrees()
    linked = len(wts) > 1
    base = g.default_base() if linked else None
    ups = g.upstreams() if linked else {}
    merged = set()
    if base:
        merged = merged_branches(g, base, g.heads(), [w["branch"] for w in wts[1:] if w["branch"]], git)
    tips = [g.tip(wt) for wt in wts]
    dates = commit_times(repo, tips, git)
    rows = []
    for i, wt in enumerate(wts):
        is_main = i == 0
        branch = wt["branch"]
        rows.append({
            "repo": repo,
            "repo_name": os.path.basename(repo),
            "path": wt["path"],
            "name": os.path.basename(wt["path"]) if not is_main else "(principal)",
            "branch": branch,
            "base": base,
            "status": classify(wt, base, merged, bool(branch) and g.gone(branch, ups), is_main),
            "last_commit": dates.get(tips[i]) or g.fallback_time(branch, wt["admin"]),
        })
    return rows


def scan_worktrees(roots, extra_paths=(), repos=()):
    """All worktrees of: repos with linked worktrees under `roots`, repos mentioned by running processes,
    and `repos` (discovered from where agents work). Reads .git folders directly; git runs only to tell
    which branches are merged, and only after a ref moved."""
    git = shutil.which("git")
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

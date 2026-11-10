"""App core: periodic scan, actions, the 24 h auto-close cycle, and the slower infra/worktree/RAM loops."""
import logging
import os
import subprocess
import threading
import time
from collections import deque

import psutil

from . import agents, infra, kernel, projects, reaper, scanner, sessions, spawns, startup, winapi, worktrees
from .redact import redact

log = logging.getLogger("devreaper")

SCAN_EVERY_S = 2
AUTO_CHECK_EVERY_S = 30
INFRA_EVERY_S = 30
SAMPLE_EVERY_S = 60
WORKTREES_EVERY_S = 300
HISTORY_EVERY_S = 600
PRUNE_EVERY_S = 3600
ALERT_COOLDOWN_S = 1800
PORT_TOUCH_EVERY_S = 60
LIVE_POINTS = 450  # 15 minutes of live data at one point per scan
SPAWN_EVERY_S = 300
SPAWN_BURST_S = 10
KERNEL_ALERT_EVERY_S = 86400
COMMIT_ALERT_EVERY_S = {"warn": 86400, "crit": 3 * 3600}
# Memory-saver settings page of each browser (opened with the browser's own exe; other apps cannot open them)
MEMORY_SAVER_PAGES = {"chrome.exe": "chrome://settings/performance",
                      "msedge.exe": "edge://settings/system/managePerformance",
                      "brave.exe": "brave://settings/performance"}
TOKENS_PER_PROCESS = 1.5  # tokens a leaking driver keeps per process started (measured on an affected machine)
EMPTY_INFRA = {"vm_mb": 0.0, "vm_running": False, "docker_host_mb": 0.0, "docker_running": False,
               "containers": [], "docker_cli": False, "distros": [], "updated": 0}


class App:
    def __init__(self, db, notify=None):
        self.db = db
        self.notify = notify or (lambda title, msg: None)
        self._lock = threading.Lock()
        self._groups, self._listeners, self._sessions = [], [], {}
        self._infra = dict(EMPTY_INFRA)
        self._worktrees = {"rows": [], "error": None, "updated": 0}
        self._scanned_at = 0.0
        self._port_touched = {}
        self._last = {"sample": 0.0, "alert": 0.0, "prune": 0.0, "worktrees": 0.0}
        self._activity = agents.Activity()
        self._live = deque(maxlen=LIVE_POINTS)
        self._group_live = {}
        self._history = {"dirs": [], "at": 0.0}
        self._extras = {"browsers_mb": 0.0, "browsers": {}, "member_of": {}, "own": frozenset()}
        self._pool = winapi.ResidentPagedPool()
        self._kernel = None
        self._kernel_series = []
        self._spawns = spawns.attribute(None, {}, {})
        self._stop = threading.Event()
        self.on_scan = None  # callback(summary) for the tray icon
        psutil.cpu_percent(None)  # prime the system CPU counter
        if self.db.settings()["auto_last_run"] == 0:
            # First launch: give the user a full interval to set excluded ports before anything is closed.
            self.db.set_setting("auto_last_run", time.time())

    # ---------- scanning ----------
    def rescan(self):
        settings = self.db.settings()
        found = sessions.load_sessions(settings["inactive_after_hours"])
        extras = {}
        groups, listeners, agent_map = scanner.scan(settings, found, self.db, self.db.excluded_ports(),
                                                    activity=self._activity, extras=extras)
        keep = self.db.kept_alive()
        for g in groups:
            g["keep_until"] = keep.get(g["id"], 0)
        with self._lock:
            self._groups, self._listeners, self._sessions = groups, listeners, agent_map
            self._extras = extras or self._extras
            self._scanned_at = time.time()
        self._record_live(groups, agent_map)
        self._record_ports(groups)
        if self.on_scan:
            self.on_scan(self.summary())
        return groups

    def groups(self):
        with self._lock:
            return list(self._groups)

    def agents(self):
        with self._lock:
            return dict(self._sessions)

    def listening_ports(self):
        with self._lock:
            return sorted({l["port"] for l in self._listeners})

    def scanned_at(self):
        with self._lock:
            return self._scanned_at

    def containers(self):
        with self._lock:
            return list(self._infra["containers"])

    def _record_live(self, groups, agent_map):
        now = time.time()
        vm = psutil.virtual_memory()
        with self._lock:
            inf, extras = self._infra, self._extras
        perf = winapi.perf_info() or {}
        self._live.append({
            "ts": round(now, 1),
            "used_mb": round((vm.total - vm.available) / 2**20, 1),
            "dev_mb": round(sum(g["mb"] for g in groups), 1),
            "agents_mb": round(sum(a["mb"] for a in agent_map.values()), 1),
            "infra_mb": round(inf["vm_mb"] + inf["docker_host_mb"], 1),
            "kernel_mb": kernel.resident_mb({"paged_resident_mb": self._pool.read_mb(),
                                             "nonpaged_mb": perf.get("nonpaged_mb")}),
            "browsers_mb": extras.get("browsers_mb", 0.0),
            "browsers": extras.get("browsers", {}),
            "commit_mb": perf.get("commit_mb"), "commit_limit_mb": perf.get("commit_limit_mb"),
            "cpu": psutil.cpu_percent(None),
        })
        alive = set()
        for g in groups:
            alive.add(g["id"])
            self._group_live.setdefault(g["id"], deque(maxlen=LIVE_POINTS)).append((round(now, 1), g["mb"], g["cpu"]))
        for gid in [k for k in self._group_live if k not in alive]:
            del self._group_live[gid]

    def live(self):
        """The last 15 minutes: system points plus per-group [ts, mb, cpu] series."""
        return {"points": list(self._live), "groups": {k: list(v) for k, v in self._group_live.items()}}

    def _record_ports(self, groups):
        now = time.time()
        entries = [(p, g["label"], g["worktree"], g["kind"]) for g in groups for p in g["ports"]]
        entries += [(p["host_port"], f"docker: {c['name']}", c["worktree"], "docker")
                    for c in self.containers() for p in c["ports"]]
        due = [e for e in entries if now - self._port_touched.get(e[:2], 0) >= PORT_TOUCH_EVERY_S]
        if due:
            self.db.touch_ports(due, now)
            for e in due:
                self._port_touched[e[:2]] = now

    def refresh_infra(self):
        snap = infra.snapshot()
        snap["updated"] = time.time()
        with self._lock:
            self._infra = snap

    def refresh_worktrees(self):
        """Repos come from where things run now, from projects seen before, and from agents' own history."""
        now = time.time()
        with self._lock:
            groups, found, containers = list(self._groups), list(self._sessions.values()), list(self._infra["containers"])
        extra = [g["worktree"] for g in groups if g["worktree"]] + [s["cwd"] for s in found]
        extra += [c["workdir"] for c in containers]
        live_repos = projects.discover(extra + [g["cwd"] for g in groups if g["cwd"]])
        if live_repos:
            self.db.touch_projects(sorted(live_repos), now)
        if now - self._history["at"] >= HISTORY_EVERY_S:
            self._history = {"dirs": projects.history_dirs(), "at": now}
        repos = live_repos | projects.discover(self._history["dirs"] + self.db.projects())
        result = worktrees.scan_worktrees(self.db.settings()["project_roots"], extra, repos)
        result["updated"] = time.time()
        with self._lock:
            self._worktrees = result
        self._last["worktrees"] = result["updated"]

    def consumers(self):
        """Everything DevReaper can close, heaviest first: [{kind, label, mb}]."""
        with self._lock:
            groups, found, inf = list(self._groups), dict(self._sessions), dict(self._infra)
        items = [{"kind": "proceso", "label": g["label"], "mb": g["mb"]} for g in groups]
        items += [{"kind": "agente", "label": f"{s.get('agent', 'Claude Code')} · {s['name']}", "mb": round(s["mb"], 1)}
                  for s in found.values()]
        items += [{"kind": "contenedor", "label": c["name"], "mb": c["mb"]} for c in inf["containers"]]
        if inf["vm_running"]:
            items.append({"kind": "WSL", "label": "Máquina virtual WSL", "mb": inf["vm_mb"]})
        return sorted(items, key=lambda i: i["mb"], reverse=True)

    def sample_ram(self):
        vm = psutil.virtual_memory()
        s = self.summary()
        top = [{"label": i["label"], "kind": i["kind"], "mb": i["mb"]}
               for i in self.consumers() if i["kind"] != "WSL"][:3]
        last = self._live[-1] if self._live else {}
        self.db.add_sample(time.time(), round((vm.total - vm.available) / 2**20, 1), s["dev_mb"],
                           s["sessions_mb"], s["infra_mb"], top, last.get("kernel_mb"), last.get("browsers_mb"))

    def refresh_kernel(self, now=None):
        """Read the pools and leak tags, store the sample, and re-assess the leak from the last 6 h."""
        now = time.time() if now is None else now
        reading = kernel.read(self._pool)
        if reading.get("tokens") is not None or reading.get("commit_mb") is not None:
            self.db.add_kernel_sample(now, reading, kernel.resident_mb(reading))
        vm = psutil.virtual_memory()
        week = self.db.kernel_history(now - 7 * 86400)
        recent = [r for r in week if r[0] >= now - kernel.TREND_HOURS * 3600]
        verdict = kernel.assess(reading, recent, vm.total / 2**20, (vm.total - vm.available) / 2**20,
                                psutil.boot_time(), now)
        # What the leak implies, to compare with what the spawn meter caught (it misses very short processes).
        if verdict["tokens_day"]:
            verdict["implied_spawns_min"] = round(max(verdict["tokens_day"], 0) / 1440 / TOKENS_PER_PROCESS, 1)
        step = max(len(week) // 168, 1)
        series = [[round(t), l] for t, _, l in week[::step] if l is not None]
        with self._lock:
            self._kernel, self._kernel_series = verdict, series
        return verdict

    def kernel(self):
        with self._lock:
            return dict(self._kernel or {}, series=list(self._kernel_series))

    def measure_spawns(self):
        """One burst of the spawn meter, charged to agents and launchers."""
        result = spawns.burst(SPAWN_BURST_S)
        with self._lock:
            extras, found = dict(self._extras), dict(self._sessions)
        out = spawns.attribute(result, extras.get("member_of", {}), found, extras.get("own", frozenset()))
        with self._lock:
            self._spawns = out
        return out

    def spawn_stats(self):
        with self._lock:
            return dict(self._spawns)

    def check_ram_alert(self):
        vm = psutil.virtual_memory()
        limit = self.db.settings()["ram_alert_percent"]
        if vm.percent < limit or time.time() - self._last["alert"] < ALERT_COOLDOWN_S:
            return False
        self._last["alert"] = time.time()
        top = ", ".join(f"{i['label']} ({_gb(i['mb'])})" for i in self.consumers()[:2])
        k = self._kernel or {}
        if k.get("level") == "warn":
            top = f"{_gb(k['leak_mb'])} retenidos en el kernel (solo se liberan reiniciando), " + top
        self.notify("DevReaper", f"RAM al {vm.percent:.0f} %. Lo que más pesa: {top}. Abre el panel para liberar.")
        return True

    def check_kernel_alert(self, now=None):
        """The leak, at most once a day and never while snoozed."""
        now = time.time() if now is None else now
        k = self._kernel or {}
        s = self.db.settings()
        if (k.get("level") != "warn" or now < s["kernel_snooze_until"]
                or now - s["kernel_alert_at"] < KERNEL_ALERT_EVERY_S):
            return False
        self.db.set_setting("kernel_alert_at", now)
        self.notify("DevReaper: reinicia cuando puedas", k["message"])
        return True

    def check_commit_alert(self, now=None):
        """Committed memory near its limit: at 100 % Windows refuses memory to programs."""
        now = time.time() if now is None else now
        k = self._kernel or {}
        level = k.get("commit_level")
        if level not in COMMIT_ALERT_EVERY_S or now - self.db.settings()["commit_alert_at"] < COMMIT_ALERT_EVERY_S[level]:
            return False
        self.db.set_setting("commit_alert_at", now)
        msg = (f"Memoria comprometida al {k['commit_pct']:.0f} % del límite ({_gb(k['commit_mb'])} de "
               f"{_gb(k['commit_limit_mb'])}). Al llegar al 100 % Windows empieza a negar memoria a los programas.")
        if k.get("level") == "warn":
            msg += f" {_gb(k['leak_mb'])} son del kernel: reiniciar los libera."
        self.notify("DevReaper", msg)
        return True

    # ---------- views ----------
    def summary(self):
        groups = self.groups()
        settings = self.db.settings()
        excluded = self.db.excluded_ports()
        with self._lock:
            found, inf = dict(self._sessions), dict(self._infra)
        orph = reaper.orphans(groups, excluded)
        due = reaper.due_for_auto(groups, settings["auto_max_age_hours"], excluded)
        return {
            "dev_mb": round(sum(g["mb"] for g in groups), 1),
            "orphans": len(orph),
            "orphans_mb": round(sum(g["mb"] for g in orph), 1),
            "due": len(due),
            "due_mb": round(sum(g["mb"] for g in due), 1),
            "ram_percent": psutil.virtual_memory().percent,
            "sessions_mb": round(sum(s["mb"] for s in found.values()), 1),
            "agents_active": sum(1 for s in found.values() if s["state"] == "active"),
            "infra_mb": round(inf["vm_mb"] + inf["docker_host_mb"], 1),
            "kernel_level": (self._kernel or {}).get("level", "ok"),
            "leak_mb": (self._kernel or {}).get("leak_mb"),
            "commit_pct": (self._kernel or {}).get("commit_pct"),
            "commit_level": (self._kernel or {}).get("commit_level", "ok"),
            "freed_7d_mb": self.db.freed_since(time.time() - 7 * 86400),
            "cpu": self._live[-1]["cpu"] if self._live else 0.0,
        }

    def state(self):
        settings = self.db.settings()
        vm = psutil.virtual_memory()
        with self._lock:
            groups, listeners, found = list(self._groups), list(self._listeners), dict(self._sessions)
            inf, wts, scanned_at = dict(self._infra), dict(self._worktrees), self._scanned_at
        excluded = self.db.excluded_ports()
        now = time.time()
        groups = [dict(g, keep_reason=reaper.keep_reason(g, settings["auto_max_age_hours"], excluded, now))
                  for g in groups]
        return {
            "now": now,
            "scanned_at": scanned_at,
            "system": {"total_mb": vm.total / 2**20, "used_mb": (vm.total - vm.available) / 2**20,
                       "percent": vm.percent, "cpus": scanner.NCPU},
            "groups": groups,
            "sessions": session_rows(found, groups, excluded, now, self.spawn_stats().get("by_agent")),
            "ports": port_rows(groups, listeners, inf["containers"], excluded, self.db.port_history()),
            "worktrees": {"rows": worktree_rows(wts["rows"], groups, inf["containers"], found.values(), excluded),
                          "error": wts["error"], "updated": wts["updated"]},
            "infra": {**inf, "containers": [dict(c, protected=bool({p["host_port"] for p in c["ports"]} & excluded))
                                            for c in inf["containers"]]},
            "settings": settings,
            "excluded_ports": sorted(excluded),
            "next_auto": settings["auto_last_run"] + settings["auto_interval_hours"] * 3600,
            "startup": startup.is_enabled(),
            "log": self.db.kill_log(),
            "summary": self.summary(),
            "live": self._live[-1] if self._live else None,
            "kernel": self.kernel(),
            "spawns": self.spawn_stats(),
        }

    # ---------- actions ----------
    def _kill(self, groups, reason, origin):
        killed, freed = 0, 0.0
        for g in groups:
            if reaper.kill_group(g):
                killed += 1
                freed += g["mb"]
                self.db.log_kill(g, reason, origin)
                log.info("killed %s (%s) pids=%s reason=%s origin=%s", g["label"], g["ports"], g["pids"], reason, origin)
        self.rescan()
        return {"killed": killed, "freed_mb": round(freed, 1)}

    def _stop_container(self, c, reason, origin):
        excluded = self.db.excluded_ports()
        held = {p["host_port"] for p in c["ports"]}
        if held & excluded:
            raise ValueError(f"el contenedor {c['name']} usa un puerto protegido ({', '.join(map(str, sorted(held & excluded)))})")
        infra.docker_stop(c["id"])
        self.db.log_kill({"label": f"Contenedor {c['name']}", "pids": [], "ports": sorted(held), "mb": c["mb"],
                          "age_hours": 0, "cmd": f"docker stop {c['id']}"}, reason, origin)
        log.info("stopped container %s reason=%s origin=%s", c["name"], reason, origin)
        self.refresh_infra()
        return {"killed": 1, "freed_mb": c["mb"]}

    def _container(self, container_id):
        for c in self.containers():
            if c["id"] == container_id:
                return c
        raise KeyError(f"el contenedor {container_id} ya no está corriendo")

    def _agent(self, pid):
        with self._lock:
            s = self._sessions.get(pid)
        if s is None:
            raise KeyError(f"el agente {pid} ya no está abierto")
        return s

    def _kill_session(self, pid, origin):
        """Close a terminal agent (Claude Code, Codex…): its dev groups, then the agent. Protected ports survive.
        Editors (Cursor, VS Code) are never closed from here; their processes can be, with kill-agent-procs."""
        s = self._agent(pid)
        if s.get("kind") != "cli":
            raise ValueError(f"{s.get('agent', 'ese programa')} se cierra desde su ventana; "
                             "aquí solo puedo cerrar los procesos que lanzó")
        proc = psutil.Process(pid)
        info = agents.classify(proc.name(), proc.exe(), " ".join(proc.cmdline()))
        if not info or info[0] != s["agent_key"] or abs(proc.create_time() - s["create_time"]) > 1:
            raise ValueError(f"el PID {pid} ya no es ese agente")
        reaper.ensure_user_process(proc)
        excluded = self.db.excluded_ports()
        groups = [g for g in self.groups() if g["session"] and g["session"]["pid"] == pid]
        protected = [g for g in groups if set(g["ports"]) & excluded]
        keep = {p for g in protected for p in g["pids"]}
        label = f"{s['agent']} · {s['name']}"
        result = self._kill([g for g in groups if g not in protected], label, origin)
        if reaper.kill_tree(proc, skip=keep):
            self.db.log_kill({"label": label, "pids": [pid], "ports": [], "mb": round(s["mb"], 1),
                              "age_hours": round((time.time() - s["started"]) / 3600, 2),
                              "cmd": f"{proc.name()} (cwd {s['cwd']})"}, "cerrar agente", origin)
            log.info("closed agent %s pid=%s kept=%s", label, pid, sorted(keep))
            result["freed_mb"] = round(result["freed_mb"] + s["mb"], 1)
        self.rescan()
        result["kept"] = len(protected)
        return result

    def idle_agents(self, hours):
        """Terminal agents with no activity for `hours` (closing one loses nothing: sessions can be resumed)."""
        now = time.time()
        return [s for s in self.agents().values() if s.get("kind") == "cli" and s["state"] == "inactive"
                and s.get("updated") and now - s["updated"] >= hours * 3600]

    def _kill_idle_agents(self, hours, origin):
        total = {"killed": 0, "freed_mb": 0.0, "kept": 0}
        for s in self.idle_agents(hours):
            try:
                r = self._kill_session(s["pid"], origin)
            except (KeyError, ValueError, psutil.Error) as e:
                log.info("skipped idle agent %s: %s", s["pid"], e)
                continue
            total["killed"] += r["killed"] + 1
            total["freed_mb"] = round(total["freed_mb"] + r["freed_mb"], 1)
            total["kept"] += r["kept"]
        return total

    def _close_browser(self, g, reason, origin):
        """Close only the automation browser of a group (Playwright MCP opens a new one when it needs it)."""
        b = g.get("browser")
        if not b:
            raise ValueError(f"{g['label']} no tiene un navegador abierto")
        proc = psutil.Process(b["pid"])
        if abs(proc.create_time() - b["create_time"]) > 1:
            raise ValueError("ese navegador ya se cerró")
        reaper.ensure_user_process(proc)
        if not reaper.kill_tree(proc):
            return {"killed": 0, "freed_mb": 0.0}
        self.db.log_kill({"label": f"Navegador de {g['label']}", "pids": b["pids"], "ports": [], "mb": b["mb"],
                          "age_hours": round((time.time() - b["create_time"]) / 3600, 2),
                          "cmd": f"{proc.name()} (navegador automatizado)"}, reason, origin)
        log.info("closed browser of %s pid=%s reason=%s origin=%s", g["label"], b["pid"], reason, origin)
        self.rescan()
        return {"killed": 1, "freed_mb": b["mb"]}

    def _browser_settings(self, name):
        """Open the browser's memory-saver page (chrome://… pages only open from the browser's own exe)."""
        url = MEMORY_SAVER_PAGES.get(name)
        if not url:
            raise ValueError(f"no sé abrir los ajustes de memoria de {name}")
        for p in psutil.process_iter():
            try:
                if p.name().lower() == name and p.parent() and p.parent().name().lower() != name:
                    exe = p.exe()
                    break
            except psutil.Error:
                continue
        else:
            raise ValueError(f"{name} no está abierto")
        subprocess.Popen([exe, url], close_fds=True)
        return {"ok": True}

    def close_idle_browsers(self, now=None):
        """Automation browsers whose group has used no CPU for `close_idle_browser_minutes`."""
        now = time.time() if now is None else now
        minutes = self.db.settings()["close_idle_browser_minutes"]
        if not minutes:
            return 0
        closed = 0
        for g in self.groups():
            b = g.get("browser")
            if (not b or g.get("excluded") or g.get("keep_until", 0) > now
                    or g["idle_hours"] * 60 < minutes or now - b["create_time"] < minutes * 60):
                continue
            try:
                closed += self._close_browser(g, f"navegador sin uso {minutes:g} min", "automático")["killed"]
            except (ValueError, psutil.Error) as e:
                log.info("skipped idle browser of %s: %s", g["label"], e)
        return closed

    def _kill_agents(self, pids, origin):
        """Close the listed terminal agents that are still inactive (an agent used meanwhile is skipped)."""
        total = {"killed": 0, "freed_mb": 0.0, "kept": 0}
        found = self.agents()
        for pid in pids:
            s = found.get(pid)
            if not s or s.get("kind") != "cli" or s["state"] != "inactive":
                total["kept"] += 1
                continue
            try:
                r = self._kill_session(pid, origin)
            except (KeyError, ValueError, psutil.Error) as e:
                log.info("skipped agent %s: %s", pid, e)
                total["kept"] += 1
                continue
            total["killed"] += r["killed"] + 1
            total["freed_mb"] = round(total["freed_mb"] + r["freed_mb"], 1)
            total["kept"] += r["kept"]
        return total

    def _kill_agent_procs(self, pid, origin):
        """Close what an agent launched, leaving the agent (or editor) itself open."""
        s = self._agent(pid)
        excluded = self.db.excluded_ports()
        groups = [g for g in self.groups() if g["session"] and g["session"]["pid"] == pid]
        free = [g for g in groups if not set(g["ports"]) & excluded]
        result = self._kill(free, f"procesos de {s.get('agent', 'agente')} · {s['name']}", origin)
        result["kept"] = len(groups) - len(free)
        return result

    def _kill_selected(self, ids, origin):
        excluded = self.db.excluded_ports()
        by_id = {g["id"]: g for g in self.groups()}
        chosen = [by_id[i] for i in ids if i in by_id]
        free = [g for g in chosen if not set(g["ports"]) & excluded]
        result = self._kill(free, "selección", origin)
        result["kept"] = len(chosen) - len(free)
        return result

    def _kill_worktree(self, path, origin):
        key = worktrees.norm(path)
        excluded = self.db.excluded_ports()
        with self._lock:
            known = [w for w in self._worktrees["rows"] if worktrees.norm(w["path"]) == key]
        is_main = bool(known) and known[0]["status"] == "main"
        # same membership rule as the Worktrees table, so "Cerrar todo" closes exactly what the row shows
        groups, containers, _ = worktree_members(path, is_main, self.groups(), self.containers())
        free_groups = [g for g in groups if not set(g["ports"]) & excluded]
        free_containers = [c for c in containers if not {p["host_port"] for p in c["ports"]} & excluded]
        name = os.path.basename(path)
        result = self._kill(free_groups, f"worktree {name}", origin)
        for c in free_containers:
            r = self._stop_container(c, f"worktree {name}", origin)
            result["killed"] += r["killed"]
            result["freed_mb"] = round(result["freed_mb"] + r["freed_mb"], 1)
        result["kept"] = len(groups) - len(free_groups) + len(containers) - len(free_containers)
        return result

    def _free_port(self, port, origin):
        excluded = self.db.excluded_ports()
        if port in excluded:
            raise ValueError(f"el puerto {port} está protegido")
        reason = f"liberar puerto {port}"
        for g in self.rescan():
            if port in g["ports"]:
                return self._kill([g], reason, origin)
        for c in self.containers():
            if port in {p["host_port"] for p in c["ports"]}:
                return self._stop_container(c, f"{reason} (contenedor)", origin)
        holder = next((l for l in self._listeners if l["port"] == port), None)
        if holder is None:
            raise ValueError(f"nada escucha en el puerto {port}")
        if holder["infra"]:
            raise ValueError(f"el puerto {port} lo publica {holder['name']} (Docker/WSL). Detén el contenedor "
                             "desde la pestaña Docker y WSL; matar ese proceso tumbaría Docker o WSL entero.")
        p = psutil.Process(holder["pid"])
        reaper.ensure_user_process(p)
        g = {"root_pid": p.pid, "create_time": p.create_time(), "pids": [p.pid], "label": p.name(),
             "ports": [port], "mb": round(p.memory_info().rss / 2**20, 1), "age_hours": 0,
             "cmd": redact(" ".join(p.cmdline()))[:300]}
        return self._kill([g], reason, origin)

    def _wsl_shutdown(self, origin):
        excluded = self.db.excluded_ports()
        protected = [c["name"] for c in self.containers() if {p["host_port"] for p in c["ports"]} & excluded]
        if protected:
            raise ValueError(f"no apago WSL: detendría {', '.join(protected)}, que usa un puerto protegido")
        with self._lock:
            mb = self._infra["vm_mb"]
        infra.wsl_shutdown()
        self.db.log_kill({"label": "WSL (máquina virtual)", "pids": [], "ports": [], "mb": mb, "age_hours": 0,
                          "cmd": "wsl --shutdown"}, "apagar WSL", origin)
        self.refresh_infra()
        return {"killed": 1, "freed_mb": mb}

    def _group(self, group_id):
        for g in self.groups():
            if g["id"] == group_id:
                return g
        raise KeyError(f"grupo {group_id} ya no existe")

    def action(self, name, body, origin="panel"):
        settings = self.db.settings()
        excluded = self.db.excluded_ports()
        if name == "kill":
            return self._kill([self._group(body["id"])], "manual", origin)
        if name == "kill-session":
            return self._kill_session(int(body["pid"]), origin)
        if name == "kill-agent-procs":
            return self._kill_agent_procs(int(body["pid"]), origin)
        if name == "kill-idle-agents":
            return self._kill_idle_agents(float(body.get("hours", 24)), origin)
        if name == "close-browser":
            return self._close_browser(self._group(str(body["id"])), "cerrar navegador", origin)
        if name == "browser-settings":
            return self._browser_settings(str(body.get("browser", "chrome.exe")))
        if name == "kill-agents":
            return self._kill_agents([int(p) for p in body["pids"]], origin)
        if name == "kill-selected":
            return self._kill_selected([str(i) for i in body["ids"]], origin)
        if name == "keep-alive":
            g = self._group(str(body["id"]))
            hours = float(body["hours"])
            if not 0 <= hours <= 24 * 7:
                raise ValueError("se puede posponer entre 0 y 168 horas")
            self.db.keep_alive(g["id"], hours, g["label"])
            self.rescan()
            return {"ok": True}
        if name == "kill-worktree":
            return self._kill_worktree(str(body["path"]), origin)
        if name == "kill-orphans":
            return self._kill(reaper.orphans(self.rescan(), excluded), "huérfanos", origin)
        if name == "kill-old":
            hours = settings["auto_max_age_hours"]
            return self._kill(reaper.due_for_auto(self.rescan(), hours, excluded), f"más de {hours:g} h", origin)
        if name == "free-port":
            return self._free_port(int(body["port"]), origin)
        if name == "docker-stop":
            return self._stop_container(self._container(str(body["id"])), "detener contenedor", origin)
        if name == "wsl-shutdown":
            return self._wsl_shutdown(origin)
        if name == "refresh-worktrees":
            self.refresh_worktrees()
            return {"ok": True}
        if name == "exclude":
            self.db.exclude_port(int(body["port"]), bool(body["excluded"]))
            self.rescan()
            return {"ok": True}
        if name == "settings":
            for key, value in body.items():
                self.db.set_setting(key, _validate(key, value))
            self.rescan()
            return {"ok": True}
        if name == "run-auto":
            return self.run_auto_cycle(origin)
        if name == "reveal":
            path = str(body["path"])
            if not os.path.isdir(path):
                raise ValueError("esa carpeta ya no existe")
            os.startfile(path)
            return {"ok": True}
        if name == "kernel-snooze":
            days = float(body.get("days", 7))
            if not 0 <= days <= 30:
                raise ValueError("se puede silenciar entre 0 y 30 días")
            self.db.set_setting("kernel_snooze_until", time.time() + days * 86400)
            return {"ok": True}
        if name == "open-startup":
            startup.open_startup_folder()
            startup.reveal_launcher()
            return {"ok": True}
        raise KeyError(f"acción desconocida: {name}")

    def ram_series(self, hours):
        return self.db.ram_series(hours)

    # ---------- loops ----------
    def run_auto_cycle(self, origin="automático"):
        settings = self.db.settings()
        hours = settings["auto_max_age_hours"]
        due = reaper.due_for_auto(self.rescan(), hours, self.db.excluded_ports())
        result = self._kill(due, f"ciclo > {hours:g} h", origin)
        self.db.set_setting("auto_last_run", time.time())
        if result["killed"]:
            self.notify("DevReaper", f"Cerré {result['killed']} procesos con más de {hours:g} h "
                                     f"({_gb(result['freed_mb'])} liberados)")
        return result

    def _loop(self, every, fn, label):
        while not self._stop.wait(every):
            try:
                fn()
            except Exception:
                log.exception("%s failed", label)

    def _auto_tick(self):
        s = self.db.settings()
        if s["auto_enabled"] and time.time() - s["auto_last_run"] >= s["auto_interval_hours"] * 3600:
            self.run_auto_cycle()

    def _slow_tick(self):
        now = time.time()
        self.refresh_infra()
        if now - self._last["worktrees"] >= WORKTREES_EVERY_S:
            self.refresh_worktrees()
        if now - self._last["sample"] >= SAMPLE_EVERY_S - 1:
            self._last["sample"] = now
            self.refresh_kernel()
            self.sample_ram()
        self.close_idle_browsers()
        self.check_ram_alert()
        self.check_kernel_alert()
        self.check_commit_alert()
        if now - self._last["prune"] >= PRUNE_EVERY_S:
            self._last["prune"] = now
            self.db.prune()

    def start(self):
        self.rescan()
        loops = [(SCAN_EVERY_S, self.rescan, "scan"), (AUTO_CHECK_EVERY_S, self._auto_tick, "auto cycle"),
                 (INFRA_EVERY_S, self._slow_tick, "slow loop"), (SPAWN_EVERY_S, self.measure_spawns, "spawn meter")]
        for every, fn, label in loops:
            threading.Thread(target=self._loop, args=(every, fn, label), daemon=True).start()
        threading.Thread(target=self._safe_first_slow_tick, daemon=True).start()

    def _safe_first_slow_tick(self):
        try:
            self._slow_tick()
        except Exception:
            log.exception("first slow tick failed")

    def stop(self):
        self._stop.set()


def _gb(mb):
    return f"{mb / 1024:.1f} GB" if mb >= 1024 else f"{mb:.0f} MB"


def session_rows(found, groups, excluded_ports, now, spawn_rates=None):
    """One row per open agent (Claude Code, Codex, Cursor…) with the dev processes it launched, heaviest first.
    `spawn_rates`: {agent pid: processes started per minute} from the last spawn burst."""
    rows = []
    spawn_rates = spawn_rates or {}
    for s in found.values():
        mine = [g for g in groups if g["session"] and g["session"]["pid"] == s["pid"]]
        groups_mb = sum(g["mb"] for g in mine)
        rows.append({
            "pid": s["pid"], "name": s["name"], "cwd": s["cwd"], "state": s["state"],
            "agent": s.get("agent", "Claude Code"), "agent_key": s.get("agent_key", "claude"),
            "kind": s.get("kind", "cli"), "closable": s.get("closable", True), "cpu": s.get("cpu", 0.0),
            "age_hours": round((now - s["started"]) / 3600, 2) if s.get("started") else None,
            "idle_hours": round((now - s["updated"]) / 3600, 2) if s.get("updated") else None,
            "mb": round(s["mb"], 1),
            "groups": [{"id": g["id"], "label": g["label"], "ports": g["ports"], "mb": g["mb"], "cpu": g.get("cpu", 0),
                        "kind": g.get("kind", "")} for g in mine],
            "groups_mb": round(groups_mb, 1),
            "total_mb": round(s["mb"] + groups_mb, 1),
            "protected": sum(1 for g in mine if set(g["ports"]) & excluded_ports),
            "spawn_rate": spawn_rates.get(s["pid"], 0.0),
        })
    return sorted(rows, key=lambda r: r["total_mb"], reverse=True)


def port_rows(groups, listeners, containers, excluded, history):
    """One row per port: who holds it now (dev group > container > any process), exposure, protection, history."""
    rows = {}

    def row(port):
        return rows.setdefault(port, {"port": port, "holder": None, "exposed": False,
                                      "protected": port in excluded, "history": history.get(port, [])})

    for g in groups:
        for port in g["ports"]:
            r = row(port)
            r["holder"] = {"type": "group", "id": g["id"], "label": g["label"], "kind": g["kind"], "mb": g["mb"],
                           "state": g["state"], "worktree": g["worktree"], "age_hours": g["age_hours"],
                           "killable": True}
            r["exposed"] = r["exposed"] or port in g["exposed"]
    for c in containers:
        for p in c["ports"]:
            r = row(p["host_port"])
            if r["holder"] is None:
                r["holder"] = {"type": "docker", "id": c["id"], "label": c["name"], "kind": "docker",
                               "mb": c["mb"], "worktree": c["worktree"], "image": c["image"], "killable": True}
            r["exposed"] = r["exposed"] or p["host_ip"] not in ("127.0.0.1", "::1")
    for l in listeners:
        r = row(l["port"])
        if r["holder"] is None:
            r["holder"] = {"type": "process", "pid": l["pid"], "label": l["name"], "kind": "proceso",
                           "mb": None, "infra": l["infra"], "killable": l["mine"] and not l["infra"]}
        if r["holder"]["type"] != "docker":
            r["exposed"] = r["exposed"] or l["exposed"]
    for port in set(excluded) | set(history):
        row(port)
    out = []
    for r in sorted(rows.values(), key=lambda r: r["port"]):
        users = {h["worktree"] or h["label"] for h in r["history"]}
        r["conflict"] = len(users) > 1
        h = r["holder"]
        r["dev"] = bool(r["history"] or r["protected"] or (h and h["type"] in ("group", "docker")))
        out.append(r)
    return out


def worktree_members(path, is_main, groups, containers, found=()):
    """(groups, containers, sessions) that belong to a worktree. Linked worktrees match by their
    `.worktrees/<name>` path; the main checkout matches by folder, excluding anything under .worktrees."""
    key = worktrees.norm(path)

    def inside(p):
        p = worktrees.norm(p)
        return bool(p) and (p == key or p.startswith(key + os.sep)) and (
            not is_main or f"{os.sep}.worktrees{os.sep}" not in p)

    def owns(wt_path, folder):
        return worktrees.norm(wt_path) == key if wt_path else is_main and inside(folder)

    gs = [g for g in groups if owns(g["worktree"], g.get("cwd", ""))]
    cs = [c for c in containers if owns(c["worktree"], c["workdir"])]
    ss = [s for s in found if inside(s["cwd"])]
    return gs, cs, ss


def worktree_rows(wt_rows, groups, containers, found, excluded):
    """Each git worktree with the processes, containers and Claude sessions running in it."""
    found = list(found)
    out = []
    for w in wt_rows:
        gs, cs, ss = worktree_members(w["path"], w["status"] == "main", groups, containers, found)
        protected = sum(1 for g in gs if set(g["ports"]) & excluded) + sum(
            1 for c in cs if {p["host_port"] for p in c["ports"]} & excluded)
        mb = round(sum(g["mb"] for g in gs) + sum(c["mb"] for c in cs), 1)
        out.append({
            **w,
            "groups": [{"id": g["id"], "label": g["label"], "ports": g["ports"], "mb": g["mb"], "kind": g["kind"]}
                       for g in gs],
            "containers": [{"name": c["name"], "ports": [p["host_port"] for p in c["ports"]], "mb": c["mb"]} for c in cs],
            "sessions": [{"name": s["name"], "state": s["state"], "agent": s.get("agent", "Claude Code")} for s in ss],
            "mb": mb,
            "ports": sorted({p for g in gs for p in g["ports"]} | {p["host_port"] for c in cs for p in c["ports"]}),
            "protected": protected,
            "candidate": w["status"] in ("merged", "gone", "missing") and len(gs) + len(cs) > protected,
        })
    out.sort(key=lambda r: (not r["candidate"], -r["mb"], -(r["last_commit"] or 0)))
    return out


def _validate(key, value):
    if key == "auto_enabled":
        return bool(value)
    if key == "close_idle_browser_minutes":
        value = float(value)
        if not 0 <= value <= 24 * 60:
            raise ValueError("los minutos deben estar entre 0 (desactivado) y 1440")
        return value
    if key in ("auto_interval_hours", "auto_max_age_hours", "show_min_mb", "show_min_age_hours",
               "inactive_after_hours"):
        value = float(value)
        if value <= 0:
            raise ValueError(f"{key} debe ser > 0")
        return value
    if key == "ram_alert_percent":
        value = float(value)
        if not 50 <= value <= 99:
            raise ValueError("el aviso de RAM debe estar entre 50 y 99 %")
        return value
    if key == "dev_names":
        names = [str(v).strip().lower().removesuffix(".exe") for v in value if str(v).strip()]
        if not names:
            raise ValueError("la lista de procesos no puede quedar vacía")
        return names
    if key == "project_roots":
        roots = [os.path.normpath(str(v).strip()) for v in value if str(v).strip()]
        missing = [r for r in roots if not os.path.isdir(r)]
        if missing:
            raise ValueError(f"no existe la carpeta: {missing[0]}")
        return roots
    raise KeyError(f"ajuste no editable: {key}")

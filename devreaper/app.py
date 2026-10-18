"""App core: periodic scan, actions, the 24 h auto-close cycle, and the slower infra/worktree/RAM loops."""
import logging
import os
import threading
import time
from collections import deque

import psutil

from . import agents, infra, projects, reaper, scanner, sessions, startup, worktrees

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
        groups, listeners, agent_map = scanner.scan(settings, found, self.db, self.db.excluded_ports(),
                                                    activity=self._activity)
        keep = self.db.kept_alive()
        for g in groups:
            g["keep_until"] = keep.get(g["id"], 0)
        with self._lock:
            self._groups, self._listeners, self._sessions = groups, listeners, agent_map
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

    def containers(self):
        with self._lock:
            return list(self._infra["containers"])

    def _record_live(self, groups, agent_map):
        now = time.time()
        vm = psutil.virtual_memory()
        with self._lock:
            inf = self._infra
        self._live.append({
            "ts": round(now, 1),
            "used_mb": round((vm.total - vm.available) / 2**20, 1),
            "dev_mb": round(sum(g["mb"] for g in groups), 1),
            "agents_mb": round(sum(a["mb"] for a in agent_map.values()), 1),
            "infra_mb": round(inf["vm_mb"] + inf["docker_host_mb"], 1),
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
        self.db.add_sample(time.time(), round((vm.total - vm.available) / 2**20, 1), s["dev_mb"],
                           s["sessions_mb"], s["infra_mb"], top)

    def check_ram_alert(self):
        vm = psutil.virtual_memory()
        limit = self.db.settings()["ram_alert_percent"]
        if vm.percent < limit or time.time() - self._last["alert"] < ALERT_COOLDOWN_S:
            return False
        self._last["alert"] = time.time()
        top = ", ".join(f"{i['label']} ({_gb(i['mb'])})" for i in self.consumers()[:2])
        self.notify("DevReaper", f"RAM al {vm.percent:.0f} %. Lo que más pesa: {top}. Abre el panel para liberar.")
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
            "sessions": session_rows(found, groups, excluded, now),
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
             "cmd": " ".join(p.cmdline())[:300]}
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
            self.sample_ram()
        self.check_ram_alert()
        if now - self._last["prune"] >= PRUNE_EVERY_S:
            self._last["prune"] = now
            self.db.prune()

    def start(self):
        self.rescan()
        loops = [(SCAN_EVERY_S, self.rescan, "scan"), (AUTO_CHECK_EVERY_S, self._auto_tick, "auto cycle"),
                 (INFRA_EVERY_S, self._slow_tick, "slow loop")]
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


def session_rows(found, groups, excluded_ports, now):
    """One row per open agent (Claude Code, Codex, Cursor…) with the dev processes it launched, heaviest first."""
    rows = []
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

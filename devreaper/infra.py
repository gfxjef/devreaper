"""Docker containers and the WSL 2 VM: what they hold (ports, RAM) and how to stop them."""
import json
import re
import shutil

import psutil

from . import quiet, worktrees

# Processes that publish ports on behalf of containers/WSL. Killing them would take down Docker Desktop or
# WSL networking as a whole, so "free port" must never target them directly.
INFRA_NAMES = {
    "com.docker.backend.exe", "com.docker.proxy.exe", "com.docker.build.exe", "docker desktop.exe",
    "docker-sandbox.exe", "vpnkit.exe", "wslrelay.exe", "wslhost.exe", "wsl.exe", "wslservice.exe",
    "vmmem", "vmmemwsl",
}
VM_NAMES = {"vmmem", "vmmemwsl"}
DOCKER_HOST_NAMES = {"com.docker.backend.exe", "com.docker.build.exe", "docker desktop.exe", "docker-sandbox.exe"}
PORT_RE = re.compile(r"(?:(\d+\.\d+\.\d+\.\d+)|\[?::\]?):(\d+)->(\d+)/(tcp|udp)")
UNITS = {"b": 1 / 2**20, "kib": 1 / 1024, "kb": 1 / 1024, "mib": 1, "mb": 1, "gib": 1024, "gb": 1024}


def parse_ports(text: str):
    """'127.0.0.1:3990->3000/tcp, :::5432->5432/tcp' -> [{'host_ip', 'host_port', 'container_port'}] (deduped)."""
    seen, out = set(), []
    for ip, host, cport, proto in PORT_RE.findall(text or ""):
        if proto != "tcp" or int(host) in seen:
            continue
        seen.add(int(host))
        out.append({"host_ip": ip or "::", "host_port": int(host), "container_port": int(cport)})
    return out


def parse_mem(text: str) -> float:
    """'364.8MiB / 15.58GiB' -> 364.8 (MB)."""
    m = re.match(r"\s*([\d.]+)\s*([a-zA-Z]+)", text or "")
    if not m:
        return 0.0
    return round(float(m.group(1)) * UNITS.get(m.group(2).lower(), 0), 1)


def parse_labels(text: str) -> dict:
    out = {}
    for part in (text or "").split(","):
        key, sep, value = part.partition("=")
        if sep:
            out[key] = value
    return out


def host_processes():
    """RAM of the WSL VM and of Docker Desktop's own Windows processes."""
    vm_mb = docker_mb = 0.0
    vm_running = docker_running = False
    # Direct calls, not process_iter(attrs): process_iter shares Process objects across threads, so `p.info`
    # can be overwritten mid-loop by the scanner's own iteration. Memory is read only for the few names that matter.
    for p in psutil.process_iter():
        try:
            name = (p.name() or "").lower()
        except psutil.Error:
            continue
        if name not in VM_NAMES and name not in DOCKER_HOST_NAMES:
            continue
        try:
            mem = p.memory_info().rss / 2**20
        except psutil.AccessDenied:
            mem = 0.0  # vmmem runs as SYSTEM on some setups: still counts as running
        except psutil.Error:
            continue
        if name in VM_NAMES:
            vm_mb += mem
            vm_running = True
        elif name in DOCKER_HOST_NAMES:
            docker_mb += mem
            docker_running = docker_running or name == "com.docker.backend.exe"
    return {"vm_mb": round(vm_mb, 1), "vm_running": vm_running,
            "docker_host_mb": round(docker_mb, 1), "docker_running": docker_running}


def docker_containers():
    docker = shutil.which("docker")
    if not docker:
        return None
    raw = quiet.run([docker, "ps", "--format", "{{json .}}"], timeout=8)
    containers = []
    for line in raw.splitlines():
        try:
            c = json.loads(line)
        except ValueError:
            continue
        labels = parse_labels(c.get("Labels", ""))
        workdir = labels.get("com.docker.compose.project.working_dir", "")
        containers.append({
            "id": c.get("ID", ""), "name": c.get("Names", ""), "image": c.get("Image", ""),
            "status": c.get("Status", ""), "running_for": c.get("RunningFor", ""),
            "ports": parse_ports(c.get("Ports", "")),
            "compose_project": labels.get("com.docker.compose.project", ""),
            "workdir": workdir, "worktree": worktrees.worktree_of(workdir),
            "mb": 0.0,
        })
    if containers:
        stats = quiet.run([docker, "stats", "--no-stream", "--format", "{{json .}}"], timeout=15)
        mem = {}
        for line in stats.splitlines():
            try:
                s = json.loads(line)
            except ValueError:
                continue
            mem[s.get("ID", "")[:12]] = parse_mem(s.get("MemUsage", ""))
        for c in containers:
            c["mb"] = mem.get(c["id"][:12], 0.0)
    return containers


def wsl_distros():
    raw = quiet.run(["wsl", "--list", "--running", "--quiet"], timeout=8)
    return [line.strip() for line in raw.splitlines() if line.strip()]


def snapshot():
    host = host_processes()
    containers = docker_containers() if host["docker_running"] else []
    return {
        **host,
        "containers": containers or [],
        "docker_cli": shutil.which("docker") is not None,
        "distros": wsl_distros() if host["vm_running"] else [],
    }


def docker_stop(container_id: str):
    if not re.fullmatch(r"[0-9a-f]{6,64}", container_id or ""):
        raise ValueError("id de contenedor inválido")
    docker = shutil.which("docker")
    if not docker:
        raise ValueError("no encontré docker en el PATH")
    if not quiet.run([docker, "stop", container_id], timeout=60):
        raise ValueError(f"docker no pudo detener {container_id}")


def wsl_shutdown():
    quiet.run(["wsl", "--shutdown"], timeout=60)

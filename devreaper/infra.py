"""Docker containers and the WSL 2 VM: what they hold (ports, RAM) and how to stop them.
Polling never starts a process: Docker is read through its Engine API on a named pipe, WSL from the registry."""
import json
import os
import re
import shutil
import threading

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
DEFAULT_PIPE = "\\\\.\\pipe\\docker_engine"
LXSS_KEY = r"Software\Microsoft\Windows\CurrentVersion\Lxss"


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


def docker_pipe():
    """Named pipe of the Docker Engine API: DOCKER_HOST=npipe://… if set, else Docker Desktop's default."""
    host = os.environ.get("DOCKER_HOST", "")
    if host.startswith("npipe://"):
        return host.removeprefix("npipe://").replace("/", "\\")
    return DEFAULT_PIPE


def decode_chunked(body: bytes) -> bytes:
    out, rest = [], body
    while rest:
        size_line, _, rest = rest.partition(b"\r\n")
        try:
            n = int(size_line.split(b";")[0].strip() or b"0", 16)
        except ValueError:
            break
        if n == 0:
            break
        out.append(rest[:n])
        rest = rest[n + 2:]
    return b"".join(out)


def parse_http(raw: bytes):
    """(status, body) of a raw HTTP/1.1 response."""
    head, _, body = raw.partition(b"\r\n\r\n")
    lines = head.split(b"\r\n")
    try:
        status = int(lines[0].split()[1])
    except (IndexError, ValueError):
        return 0, b""
    headers = {k.strip().lower(): v.strip() for k, _, v in (l.partition(b":") for l in lines[1:])}
    if b"chunked" in headers.get(b"transfer-encoding", b"").lower():
        body = decode_chunked(body)
    return status, body


def _pipe_request(pipe, method, path):
    with open(pipe, "r+b", buffering=0) as f:
        f.write(f"{method} {path} HTTP/1.1\r\nHost: docker\r\nContent-Length: 0\r\nConnection: close\r\n\r\n".encode())
        chunks = []
        while True:
            try:
                data = f.read(65536)
            except OSError:  # the engine closed its end: that is the end of the response
                break
            if not data:
                break
            chunks.append(data)
    return parse_http(b"".join(chunks))


_api_busy = threading.Lock()


def docker_api(method, path, timeout=10.0):
    """(status, body) from the Docker Engine over its named pipe, or None if it is unreachable or hung.
    A hung request is abandoned in its thread and no new one starts until it returns."""
    pipe = docker_pipe()
    if not os.path.exists(pipe) or not _api_busy.acquire(blocking=False):
        return None
    result = {}

    def call():
        try:
            result["r"] = _pipe_request(pipe, method, path)
        except OSError:
            result["r"] = None
        finally:
            _api_busy.release()

    t = threading.Thread(target=call, daemon=True)
    t.start()
    t.join(timeout)
    return result.get("r")


def api_ports(ports):
    """Engine API port list -> [{'host_ip', 'host_port', 'container_port'}] (published TCP only, deduped)."""
    seen, out = set(), []
    for p in ports or []:
        host = p.get("PublicPort")
        if not host or p.get("Type") != "tcp" or host in seen:
            continue
        seen.add(host)
        out.append({"host_ip": p.get("IP") or "::", "host_port": int(host), "container_port": int(p.get("PrivatePort") or 0)})
    return out


def stats_mb(stats) -> float:
    """Memory as `docker stats` shows it: usage minus inactive page cache."""
    mem = (stats or {}).get("memory_stats") or {}
    usage = mem.get("usage") or 0
    extra = mem.get("stats") or {}
    cache = extra.get("inactive_file", extra.get("total_inactive_file", 0)) or 0
    return round(max(usage - cache, 0) / 2**20, 1)


def docker_containers():
    r = docker_api("GET", "/containers/json")
    if r is None or r[0] != 200:
        return None
    try:
        listed = json.loads(r[1])
    except ValueError:
        return None
    containers = []
    for c in listed:
        labels = c.get("Labels") or {}
        workdir = labels.get("com.docker.compose.project.working_dir", "")
        containers.append({
            "id": (c.get("Id") or "")[:12], "name": ((c.get("Names") or ["/"])[0]).lstrip("/"),
            "image": c.get("Image", ""), "status": c.get("Status", ""), "running_for": "",
            "ports": api_ports(c.get("Ports")),
            "compose_project": labels.get("com.docker.compose.project", ""),
            "workdir": workdir, "worktree": worktrees.worktree_of(workdir),
            "mb": 0.0,
        })
    for c in containers:
        s = docker_api("GET", f"/containers/{c['id']}/stats?stream=false&one-shot=true")
        if s and s[0] == 200:
            try:
                c["mb"] = stats_mb(json.loads(s[1]))
            except ValueError:
                pass
    return containers


def wsl_distros():
    """Installed WSL distributions, from the registry (`wsl --list` would start a process every call)."""
    try:
        import winreg
    except ImportError:
        return []
    out = []
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, LXSS_KEY) as root:
            for i in range(winreg.QueryInfoKey(root)[0]):
                try:
                    with winreg.OpenKey(root, winreg.EnumKey(root, i)) as k:
                        out.append(winreg.QueryValueEx(k, "DistributionName")[0])
                except OSError:
                    continue
    except OSError:
        return []
    return sorted(out, key=str.lower)


def snapshot():
    host = host_processes()
    containers = docker_containers() if host["docker_running"] else []
    return {
        **host,
        "containers": containers or [],
        "docker_cli": os.path.exists(docker_pipe()) or shutil.which("docker") is not None,
        "distros": wsl_distros() if host["vm_running"] else [],
    }


def docker_stop(container_id: str):
    if not re.fullmatch(r"[0-9a-f]{6,64}", container_id or ""):
        raise ValueError("id de contenedor inválido")
    r = docker_api("POST", f"/containers/{container_id}/stop?t=10", timeout=30)
    if r is not None and r[0] in (204, 304):
        return
    docker = shutil.which("docker")  # engine API unreachable: fall back to the CLI (a user action, not a poll)
    if not docker:
        raise ValueError("no pude hablar con Docker")
    if not quiet.run([docker, "stop", container_id], timeout=60):
        raise ValueError(f"docker no pudo detener {container_id}")


def wsl_shutdown():
    quiet.run(["wsl", "--shutdown"], timeout=60)

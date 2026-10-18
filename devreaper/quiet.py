"""Run a console tool (git, docker, wsl) without flashing a window from pythonw."""
import subprocess

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def run(args, timeout=10, cwd=None) -> str:
    """stdout as text ('' on any failure). wsl.exe writes UTF-16LE, so decode by sniffing NUL bytes."""
    try:
        r = subprocess.run(args, capture_output=True, timeout=timeout, cwd=cwd, creationflags=NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return ""
    if r.returncode != 0:
        return ""
    return decode(r.stdout)


def decode(raw: bytes) -> str:
    if b"\x00" in raw:
        return raw.decode("utf-16-le", errors="replace").lstrip("﻿")
    return raw.decode("utf-8", errors="replace")

"""Start-with-Windows status. The user adds the shortcut by hand: Bitdefender treats a script writing to the
Startup folder as malware persistence and blocks PowerShell, so this module only reads the folder and opens it."""
import os
import subprocess
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
STARTUP_DIR = Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
LAUNCHER = PROJECT_DIR / "DevReaper (inicio).cmd"


def is_enabled(folder: Path = STARTUP_DIR) -> bool:
    try:
        return any("devreaper" in f.name.lower() for f in Path(folder).iterdir())
    except OSError:
        return False


def open_startup_folder():
    os.startfile(str(STARTUP_DIR))


def reveal_launcher():
    subprocess.Popen(["explorer", f"/select,{LAUNCHER}"])

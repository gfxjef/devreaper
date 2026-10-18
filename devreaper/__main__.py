"""Entry point: `pythonw -m devreaper` (add --startup when launched by Windows at logon)."""
import json
import logging
import sys
import threading
import urllib.request
import webbrowser

import pystray
from PIL import Image, ImageDraw

from .app import App
from .db import DB, default_path
from .server import HOST, PORT, make_server

URL = f"http://{HOST}:{PORT}/"
COLORS = {"ok": (34, 197, 94), "warn": (234, 179, 8), "bad": (239, 68, 68)}


def icon_image(level: str) -> Image.Image:
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((2, 2, 62, 62), radius=14, fill=(24, 24, 27))
    d.ellipse((16, 16, 48, 48), fill=COLORS[level])
    return img


def level_of(summary) -> str:
    if summary["ram_percent"] >= 90 or summary["orphans_mb"] >= 1024:
        return "bad"
    if summary["due"] or summary["orphans"] or summary["dev_mb"] >= 4096:
        return "warn"
    return "ok"


def already_running() -> bool:
    try:
        with urllib.request.urlopen(URL + "api/ping", timeout=1) as r:
            return json.load(r).get("app") == "devreaper"
    except OSError:
        return False


def main():
    default_path().parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=default_path().with_name("devreaper.log"), level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    at_logon = "--startup" in sys.argv
    if already_running():
        if not at_logon:
            webbrowser.open(URL)
        return

    db = DB()
    app = App(db)
    app.rescan()  # so the first panel load is never empty
    server = make_server(app)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    state = {"level": None}

    def open_panel(icon=None, item=None):
        webbrowser.open(URL)

    def bulk(name):
        return lambda icon, item: app.action(name, {}, origin="bandeja")

    def quit_app(icon, item):
        app.stop()
        server.shutdown()
        icon.stop()

    icon = pystray.Icon(
        "devreaper", icon_image("ok"), "DevReaper",
        menu=pystray.Menu(
            pystray.MenuItem("Abrir panel", open_panel, default=True),
            pystray.MenuItem("Cerrar huérfanos", bulk("kill-orphans")),
            pystray.MenuItem("Ejecutar ciclo de cierre ahora", bulk("run-auto")),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Salir", quit_app),
        ),
    )

    def on_scan(summary):
        level = level_of(summary)
        if level != state["level"]:
            state["level"] = level
            icon.icon = icon_image(level)
        icon.title = (f"DevReaper · Dev {summary['dev_mb'] / 1024:.1f} GB · "
                      f"{summary['orphans']} huérfanos · RAM {summary['ram_percent']:.0f}%")

    app.notify = lambda title, msg: icon.notify(msg, title)
    app.on_scan = on_scan

    def setup(icon):
        icon.visible = True
        app.start()
        if not at_logon:
            open_panel()

    icon.run(setup)


if __name__ == "__main__":
    main()

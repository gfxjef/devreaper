"""Entry point: `pythonw -m devreaper` (add --startup when launched by Windows at logon)."""
import json
import logging
import sys
import threading
import urllib.request

import pystray

from . import tray
from .app import App
from .db import DB, default_path
from .server import HOST, PORT, make_server

URL = f"http://{HOST}:{PORT}/"


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
            tray.open_url(URL, as_app=True)
        return

    db = DB()
    app = App(db)
    app.rescan()  # so the first panel load is never empty
    server = make_server(app)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    shown = {"look": None}
    icon = None

    def run(action):
        kind, body = action
        if kind == "open":
            return tray.open_url(body, as_app=body.startswith(URL))
        if kind == "quit":
            app.stop()
            server.shutdown()
            return icon.stop()

        def work():  # off the tray thread: closing can take seconds and must not freeze the menu
            try:
                result = app.action(kind, body, origin="bandeja")
                if kind not in ("settings", "keep-alive"):
                    icon.notify(tray.result_text(result), "DevReaper")
            except (ValueError, KeyError, OSError) as e:
                icon.notify(str(e), "DevReaper: no se pudo")
        tray.run_in_thread(work)

    def model():
        return tray.menu_model(app.state(), URL)

    icon = tray.make_icon_class(pystray)(
        "devreaper", tray.icon_image("ok"), "DevReaper", menu=tray.build_pystray_menu(pystray, model, run))

    def on_scan(summary):
        level = tray.level_of(summary)
        look = (level, round(summary["ram_percent"] / 5) * 5)  # redraw only when the ring visibly changes
        if look != shown["look"]:
            shown["look"] = look
            icon.icon = tray.icon_image(*look)
        icon.title = tray.tooltip(summary)

    app.notify = lambda title, msg: icon.notify(msg, title)
    app.on_scan = on_scan

    def setup(icon):
        icon.visible = True
        app.start()
        if not at_logon:
            tray.open_url(URL, as_app=True)

    icon.run(setup)


if __name__ == "__main__":
    main()

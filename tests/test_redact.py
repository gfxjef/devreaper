"""Secrets in command lines never reach the panel, the kill log or the database."""
import sqlite3

import pytest

from devreaper.db import DB
from devreaper.redact import redact

SECRET = "AbCdEf0123456789xyz"


@pytest.mark.parametrize("cmd", [
    f"npx --yes vercel@latest ls konecta-desarrollo --token {SECRET}",
    f"npx vercel ls --token={SECRET}",
    f'node app.js --github-token "{SECRET}"',
    f"cmd /c set VERCEL_TOKEN={SECRET}&& node x.js",
    f"python run.py --api-key {SECRET}",
    f"git clone https://user:{SECRET}@github.com/org/repo.git",
    f'curl -H "Authorization: Bearer {SECRET}" https://api.x',
    f"node x.js ghp_{SECRET}",
    f"python -m tool --password={SECRET} --verbose",
])
def test_secret_never_survives(cmd):
    out = redact(cmd)
    assert SECRET not in out and "***" in out


def test_ordinary_commands_are_untouched():
    for cmd in ['"C:\\Program Files\\nodejs\\node.exe" next dev -p 3000',
                "python -m uvicorn app:app --port 8000 --reload",
                "git -c core.fsmonitor= status --porcelain", "npx @playwright/mcp@latest", ""]:
        assert redact(cmd) == cmd


def test_kill_log_stores_masked_and_old_rows_are_scrubbed(tmp_path):
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE kill_log (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, label TEXT, "
                "pids TEXT, ports TEXT, mb REAL, age_hours REAL, reason TEXT, origin TEXT DEFAULT '', cmd TEXT DEFAULT '')")
    con.execute("INSERT INTO kill_log (ts, label, pids, ports, mb, age_hours, reason, cmd) "
                "VALUES (1, 'old', '[]', '[]', 5, 1, 'x', ?)", (f"npx vercel --token {SECRET}",))
    con.commit()
    con.close()
    db = DB(path)
    db.log_kill({"label": "new", "pids": [], "ports": [], "mb": 1, "age_hours": 0,
                 "cmd": f"node x --token={SECRET}"}, "manual")
    assert all(SECRET not in row["cmd"] for row in db.kill_log())
    raw = sqlite3.connect(path).execute("SELECT group_concat(cmd) FROM kill_log").fetchone()[0]
    assert SECRET not in raw

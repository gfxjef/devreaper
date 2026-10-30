"""Kernel memory: what the pools hold, the token leak some endpoint-security drivers cause, and when to restart.

The leak shows up as security tokens (pool tag 'Toke') that are never freed: every process started adds one or
two, so a machine that launches many short processes (git polling, docker CLI) grows ~1 GB a day. No process owns
that memory and closing programs does not return it; only a restart does."""
import time

from . import winapi

# Token object plus the security data attached to it (SIDs, token defaults, logon session).
LEAK_TAGS = ("Toke", "SeAt", "SeTd", "SeDt", "SeTl")
INFO_TOKENS = 300_000            # a healthy Windows holds tens of thousands
INFO_TOKENS_PER_DAY = 100_000
WARN_LEAK_MB = 3 * 1024
WARN_ETA_HOURS = 72
MIN_GROWTH_MB_DAY = 200          # below this, a near-full RAM is not the leak's fault
COMMIT_WARN, COMMIT_CRIT = 85.0, 95.0
TREND_HOURS = 6


def read(resident_pool=None):
    """One reading of the pools, commit charge and leak tags. Missing parts are None."""
    perf = winapi.perf_info() or {}
    tags = winapi.pool_tags()
    resident = resident_pool.read_mb() if resident_pool else None
    out = {
        "commit_mb": perf.get("commit_mb"), "commit_limit_mb": perf.get("commit_limit_mb"),
        "paged_mb": perf.get("paged_mb"), "nonpaged_mb": perf.get("nonpaged_mb"),
        "paged_resident_mb": resident,
        "tokens": None, "leak_mb": None,
    }
    if tags:
        out["tokens"] = sum(tags.get("Toke", (0, 0, 0, 0))[i] for i in (1, 3))
        out["leak_mb"] = round(sum(tags[t][0] + tags[t][2] for t in LEAK_TAGS if t in tags) / winapi.MB, 1)
    return out


def resident_mb(reading):
    """Kernel memory that occupies RAM: resident paged pool + nonpaged pool (None if unknown)."""
    if not reading or reading.get("paged_resident_mb") is None or reading.get("nonpaged_mb") is None:
        return None
    return round(reading["paged_resident_mb"] + reading["nonpaged_mb"], 1)


def slope_per_day(points, min_span_s=1800):
    """Least-squares slope of [(ts, value)] in units per day; None without enough spread in time."""
    pts = [(t, v) for t, v in points if v is not None]
    if len(pts) < 3 or pts[-1][0] - pts[0][0] < min_span_s:
        return None
    n = len(pts)
    mt = sum(t for t, _ in pts) / n
    mv = sum(v for _, v in pts) / n
    den = sum((t - mt) ** 2 for t, _ in pts)
    if den == 0:
        return None
    return sum((t - mt) * (v - mv) for t, v in pts) / den * 86400


def _eta_hours(limit, current, growth_mb_day):
    if limit is None or current is None or not growth_mb_day or growth_mb_day <= 0:
        return None
    if current >= limit:
        return 0.0
    return (limit - current) / growth_mb_day * 24


def _gb(mb):
    return f"{mb / 1024:.1f} GB"


def assess(reading, history, total_mb, used_mb, boot_time, now=None):
    """Verdict for the panel and tray. `history`: [(ts, tokens, leak_mb)] of the last hours, oldest first."""
    now = time.time() if now is None else now
    reading = reading or {}
    tokens, leak = reading.get("tokens"), reading.get("leak_mb")
    growth = slope_per_day([(t, l) for t, _, l in history])
    tokens_day = slope_per_day([(t, k) for t, k, _ in history])
    commit, limit = reading.get("commit_mb"), reading.get("commit_limit_mb")
    commit_pct = round(commit / limit * 100, 1) if commit and limit else None

    etas = [e for e in (_eta_hours(0.9 * total_mb if total_mb else None, used_mb, growth),
                        _eta_hours(0.9 * limit if limit else None, commit, growth)) if e is not None]
    eta = min(etas) if etas else None
    leak_growing = growth is not None and growth >= MIN_GROWTH_MB_DAY

    level = "ok"
    if leak is not None and (leak >= WARN_LEAK_MB or (leak_growing and eta is not None and eta < WARN_ETA_HOURS)):
        level = "warn"
    elif (tokens or 0) > INFO_TOKENS or (tokens_day or 0) > INFO_TOKENS_PER_DAY:
        level = "info"

    message = ""
    if level == "warn":
        message = f"Windows retiene {_gb(leak)} en el kernel que ningún programa libera; solo se recupera reiniciando."
        if leak_growing:
            message += f" Crece {_gb(growth)} al día"
            if eta is not None and eta > 0:
                message += f": a este ritmo llegas al 90 % el {time.strftime('%d/%m %H:%M', time.localtime(now + eta * 3600))}"
            message += "."
    elif level == "info":
        message = f"El kernel acumula {tokens:,} tokens de seguridad (lo normal son decenas de miles).".replace(",", ".")

    commit_level = "ok"
    if commit_pct is not None:
        commit_level = "crit" if commit_pct >= COMMIT_CRIT else "warn" if commit_pct >= COMMIT_WARN else "ok"

    return {
        "level": level, "message": message,
        "tokens": tokens, "leak_mb": leak,
        "growth_mb_day": round(growth, 1) if growth is not None else None,
        "tokens_day": round(tokens_day) if tokens_day is not None else None,
        "eta_hours": round(eta, 1) if eta is not None and leak_growing else None,
        "resident_mb": resident_mb(reading),
        "paged_mb": reading.get("paged_mb"), "nonpaged_mb": reading.get("nonpaged_mb"),
        "commit_mb": commit, "commit_limit_mb": limit, "commit_pct": commit_pct, "commit_level": commit_level,
        "boot_time": boot_time, "uptime_hours": round((now - boot_time) / 3600, 1) if boot_time else None,
    }

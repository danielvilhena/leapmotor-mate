"""Diagnostics helpers for the Settings → Diagnostics card.

Gathers a self-service support snapshot (version, model, DB stats, feature flags, current
state) and reads the rotating log files both processes write under the data dir, so a user
hitting a problem can copy/download logs + context to attach to a GitHub issue — instead of
us asking them to dig through Docker / Home-Assistant add-on logs by hand.

Everything here is read-only and redacts obvious secrets + the VIN before it leaves the box.
"""
import os
import re
from datetime import datetime, timezone
from pathlib import Path

import db_reader


def data_dir() -> Path:
    """The persistent data dir — parent of the DB (set via DB_PATH in run.sh, /data on the add-on)."""
    return Path(os.environ.get("DB_PATH", "/data/leapmotor_mate.db")).parent


POLLER_LOG = "mate-poller.log"
WEB_LOG = "mate-web.log"
_LOG_FILES = {"poller": POLLER_LOG, "web": WEB_LOG}


# ── redaction ────────────────────────────────────────────────────────────────
# Defensive: the app never logs credentials, but a diagnostics bundle is shared publicly,
# so scrub anything that smells like one, plus the 17-char VIN and e-mail addresses.
# Sensitive key names — bare or compound (private_key, access_token, refresh_token…), plus the
# Leapmotor device_id (a long account-bound identifier the API auth logs in the clear).
_SECRET_ROOT = (
    r'(?:passwords?|passwd|passphrase|pwd|pass|pins?|secrets?|tokens?|credentials?|auth|apikey|keys?|'
    r'device[_-]?ids?)')
# An optional word-prefix joined by `_`/`-` (access_token, private_key). The separator is REQUIRED,
# so words that merely CONTAIN a root (monkey, compass, passenger) are never matched.
_SECRET_KEY = r'(?:\w+[_-])?' + _SECRET_ROOT
# key=value / key: value, in plain OR JSON form (optional matching quotes around the key),
# value either a quoted string (spaces kept) or an unquoted run up to a delimiter.
_KV_SECRET_RE = re.compile(
    r'(?i)(["\']?)\b(' + _SECRET_KEY + r')\b\1?\s*[:=]\s*'
    r'''("(?:[^"\\\n]|\\.)*"|'(?:[^'\\\n]|\\.)*'|[^\s,;}\n]+)''')
_AUTH_RE = re.compile(r'(?i)\bauthorization\b\s*[:=].*')   # whole header value to EOL
_BEARER_RE = re.compile(r'(?i)\bbearer\s+[\w.\-]+')         # "Bearer <token>" with no key=
# `*` in the local part also catches an already-partly-masked address (sil***@dxc.com → ***@***).
_EMAIL_RE = re.compile(r'\b[\w.+*-]+@[\w.-]+\.\w{2,}\b')
_VIN_RE = re.compile(r'\b([A-HJ-NPR-Z0-9]{17})\b')


def mask_vin(vin: str | None) -> str:
    if not vin:
        return "—"
    return f"{vin[:3]}…{vin[-4:]}" if len(vin) >= 8 else "…"


def _redact(text: str) -> str:
    text = _KV_SECRET_RE.sub(lambda m: f"{m.group(2)}=***", text)
    text = _AUTH_RE.sub("authorization=***", text)
    text = _BEARER_RE.sub("bearer ***", text)
    text = _EMAIL_RE.sub("***@***", text)
    text = _VIN_RE.sub(lambda m: f"{m.group(1)[:3]}…{m.group(1)[-4:]}", text)
    return text


# ── system snapshot ──────────────────────────────────────────────────────────
def build_system_info(version: str) -> dict:
    """Cheap (no live cloud call) support snapshot for the card + the bundle header."""
    vehicle, settings = db_reader.get_vehicle()
    db = db_reader._get()

    def _count(table: str) -> int:
        try:
            return db.execute(f"SELECT COUNT(*) c FROM {table}").fetchone()["c"]
        except Exception:  # noqa: BLE001
            return -1

    last = None
    try:
        last = db.execute(
            "SELECT recorded_at, soc, gear, charging FROM positions ORDER BY id DESC LIMIT 1"
        ).fetchone()
    except Exception:  # noqa: BLE001
        pass

    age_min = None
    if last and last["recorded_at"]:
        try:
            age_min = round(
                (datetime.now(timezone.utc) - datetime.fromisoformat(last["recorded_at"]))
                .total_seconds() / 60, 1)
        except (TypeError, ValueError):
            pass

    return {
        "version": version,
        "model": (vehicle or {}).get("car_type") or "—",
        "year": (vehicle or {}).get("year") or "—",
        "vin_masked": mask_vin((vehicle or {}).get("vin")),
        "battery_kwh": settings.get("battery_capacity_kwh", "—"),
        "language": settings.get("language", "en"),
        "db_size_mb": round(db_reader.get_db_size_bytes() / 1048576, 1),
        "counts": {"trips": _count("trips"), "charges": _count("charges"),
                   "positions": _count("positions")},
        "poll_parked": settings.get("poll_parked", "30"),
        "poll_driving": settings.get("poll_driving", "10"),
        "features": {
            "mqtt": settings.get("mqtt_enabled") == "1",
            "wallbox": bool(settings.get("ha_url") or os.environ.get("SUPERVISOR_TOKEN")),
            "abrp": settings.get("abrp_enabled") == "1",
            "addon": bool(os.environ.get("SUPERVISOR_TOKEN")),
        },
        "last_poll_iso": last["recorded_at"] if last else None,
        "last_poll_age_min": age_min,
        "last_soc": last["soc"] if last else None,
        "last_gear": last["gear"] if last else None,
        "last_charging": bool(last["charging"]) if last else None,
    }


# ── logs ─────────────────────────────────────────────────────────────────────
def read_log_tail(which: str, lines: int = 200) -> str:
    """Last `lines` of the poller/web log file (redacted). Returns a friendly note if absent."""
    name = _LOG_FILES.get(which)
    if not name:
        return f"(unknown log '{which}')"
    path = data_dir() / name
    if not path.exists():
        return ("(no log file yet — it appears after the next restart, once the file logger is "
                "active. Until then, see the container / Home Assistant add-on log.)")
    try:
        with path.open("r", errors="replace") as fh:
            tail = fh.readlines()[-max(1, min(lines, 2000)):]
        return _redact("".join(tail)).strip() or "(log is empty)"
    except Exception as e:  # noqa: BLE001
        return f"(could not read log: {e})"


# ── shareable bundle ─────────────────────────────────────────────────────────
_BUNDLE_PARTS = ("info", "poller", "web")   # user-selectable sections (NO raw signals → no GPS)


def build_bundle(version: str, parts=_BUNDLE_PARTS, lines: int = 300) -> str:
    """One redacted text blob to attach to an issue. `parts` selects which sections to include
    (any of 'info', 'poller', 'web'); a one-line version header is always present. Never includes
    raw signals (those carry GPS) — that stays a separate, explicit copy-from-screen action."""
    want = {p for p in parts if p in _BUNDLE_PARTS} or set(_BUNDLE_PARTS)
    out = [f"===== LeapMotor Mate {version} — diagnostics ====="]

    if "info" in want:
        info = build_system_info(version)
        f = info["features"]
        out += [
            f"Model / year : {info['model']} / {info['year']}",
            f"VIN          : {info['vin_masked']}",
            f"Battery kWh  : {info['battery_kwh']}",
            f"Language     : {info['language']}",
            f"DB size (MB) : {info['db_size_mb']}",
            f"Rows         : trips={info['counts']['trips']} "
            f"charges={info['counts']['charges']} positions={info['counts']['positions']}",
            f"Poll (s)     : parked={info['poll_parked']} driving={info['poll_driving']}",
            f"Features     : mqtt={f['mqtt']} wallbox={f['wallbox']} abrp={f['abrp']} addon={f['addon']}",
            f"Last poll    : {info['last_poll_iso']} (age {info['last_poll_age_min']} min) "
            f"soc={info['last_soc']} gear={info['last_gear']} charging={info['last_charging']}",
        ]
    if "poller" in want:
        out += ["", "----- poller log (recent) -----", read_log_tail("poller", lines)]
    if "web" in want:
        out += ["", "----- web log (recent) -----", read_log_tail("web", lines)]
    out += ["", "===== end ====="]
    return "\n".join(out)

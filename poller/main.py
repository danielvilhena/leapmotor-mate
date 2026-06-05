"""LeapMotor Mate — vehicle data poller."""
import logging
import os
import pathlib
import threading
import time

_PROJECT_ROOT = pathlib.Path(__file__).parent.parent

import abrp
from client import LeapmotorMateClient, set_charge_current_min, EmptyStatusError
from db import Database
from mqtt import MqttService
from recorder import Recorder

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("leapmotor_mate")


# Expected state to publish the instant a command succeeds (mirrors the web UI's
# optimistic overlay), so the HA entity flips immediately instead of waiting for the
# next poll. The boost re-poll below then confirms it from the real signals.
_MQTT_OPTIMISTIC = {
    "lock":        ("locked", True),
    "unlock":      ("locked", False),
    "open_trunk":  ("trunk_open", True),
    "close_trunk": ("trunk_open", False),
}
_MQTT_BOOST_S = 60   # after a command, poll fast for a minute so the state syncs quickly

# The MQTT command handler runs on paho's network thread while the poll loop runs on the
# main thread; both use the same Leapmotor API client (one HTTP session). Serialize API
# access between them so requests can't interleave/corrupt each other.
_API_LOCK = threading.Lock()


def _handle_mqtt_command(client, service, db, vin: str, cmd: str, value):
    """Execute a remote MQTT command, then keep HA in sync the same way the web UI
    does: publish the expected state immediately (optimistic) and trigger a fast
    re-poll so the real signals confirm it within seconds. Without this the MQTT
    state only refreshed on the next scheduled poll (up to 30s when parked), which
    is why it looked stale/out of sync. B10 commands the cloud accepts-but-ignores
    (e.g. full A/C off) stay best-effort."""
    api = client._api
    optimistic = _MQTT_OPTIMISTIC.get(cmd)
    try:
        with _API_LOCK:  # serialize against the poll loop's get_status (shared HTTP session)
            if cmd == "lock":          api.lock_vehicle(vin)
            elif cmd == "unlock":      api.unlock_vehicle(vin)
            elif cmd == "open_trunk":  api.open_trunk(vin)
            elif cmd == "close_trunk": api.close_trunk(vin)
            elif cmd == "find_car":    api.find_vehicle(vin)
            elif cmd == "climate_cool":
                api.quick_cool(vin);         optimistic = ("climate_on", True)
            elif cmd == "climate_heat":
                api.quick_heat(vin);         optimistic = ("climate_on", True)
            elif cmd == "climate_defrost":
                api.windshield_defrost(vin); optimistic = ("climate_on", True)
            elif cmd == "climate_off":
                # leapmotor-api 0.3.1 has a dedicated ac_off() (operate=close), so no more
                # toggle-guard hack — this reliably turns the climate off.
                api.ac_off(vin);             optimistic = ("climate_on", False)
            else:
                return
        log.info("MQTT: executed command %s %s", cmd, value or "")
    except Exception as exc:  # noqa: BLE001
        log.error("MQTT: command %s failed: %s", cmd, exc)
        return

    # Command succeeded → reflect it in HA now, then re-poll fast to confirm the real state.
    if optimistic and service:
        try:
            service.publish_state(vin, optimistic[0], optimistic[1])
        except Exception as exc:  # noqa: BLE001
            log.warning("MQTT: optimistic publish failed: %s", exc)
    try:
        # Write from a short-lived dedicated connection: this runs on paho's network
        # thread and the poller's shared db connection isn't safe to use cross-thread.
        import sqlite3
        c = sqlite3.connect(db._path, timeout=5.0)
        try:
            c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                      ("boost_until", str(time.time() + _MQTT_BOOST_S)))
            c.commit()
        finally:
            c.close()
    except Exception as exc:  # noqa: BLE001
        log.warning("MQTT: boost trigger failed: %s", exc)


def _mqtt_tick(db, client, data, service):
    """Manage the MQTT bridge each poll cycle: (dis)connect on the enable flag,
    then publish the current state. Returns the (possibly new/None) service."""
    if db.get_setting("mqtt_enabled") != "1" or not db.get_setting("mqtt_broker"):
        if service:
            service.disconnect()
        return None
    if service is None:
        service = MqttService(
            broker=db.get_setting("mqtt_broker"),
            port=db.get_setting("mqtt_port", "1883"),
            username=db.get_setting("mqtt_user") or None,
            password=db.get_secret("mqtt_pass") or None,
            topic_prefix=db.get_setting("mqtt_prefix", "leapmotor"),
            use_tls=db.get_setting("mqtt_tls") == "1",
            tls_insecure=db.get_setting("mqtt_tls_insecure") == "1",
            discovery_enabled=db.get_setting("mqtt_discovery", "1") == "1",
        )
        service.on_command = lambda vin, cmd, val: _handle_mqtt_command(client, service, db, vin, cmd, val)
    try:
        service.publish_status(data)
    except Exception as exc:  # noqa: BLE001
        log.error("MQTT: publish failed: %s", exc)
    return service


def load_config(db: "Database") -> dict:
    """Load credentials from DB settings, falling back to env vars (dev mode).
    DB takes precedence over env — same order as the web layer — so a stray
    LEAPMOTOR_USER in the environment (or a mounted .env) can never silently
    switch the poller to a different account than the one set up in the wizard."""
    def _get(key_env: str, key_db: str, default: str = "", secret: bool = False) -> str:
        # decrypt only the DB-sourced value, then fall back to the (plaintext) env var
        val = db.get_secret(key_db) if secret else db.get_setting(key_db)
        return val or os.environ.get(key_env) or default

    return {
        "username":  _get("LEAPMOTOR_USER", "leapmotor_user"),
        "password":  _get("LEAPMOTOR_PASS", "leapmotor_pass", secret=True),
        "pin":       _get("LEAPMOTOR_PIN",  "leapmotor_pin", "", secret=True),
        "cert_path": _cert_path("app.crt", "CERT_PATH"),
        "key_path":  _cert_path("app.key", "KEY_PATH"),
    }


def _cert_path(filename: str, env_key: str) -> str:
    """Resolve the app cert/key: explicit env override → wizard-provided /data/certs →
    image-bundled certs/. The wizard writes user-provided certs to /data/certs (persistent),
    so a fresh install works without any cert baked into the image."""
    override = os.environ.get(env_key)
    if override:
        return override
    data_dir = os.environ.get("DATA_CERT_DIR", "/data/certs")
    data_path = os.path.join(data_dir, filename)
    if os.path.exists(data_path):
        return data_path
    return str(_PROJECT_ROOT / "certs" / filename)


def main():
    db_path = os.environ.get("DB_PATH", "leapmotor_mate.db")
    log.info("Starting LeapMotor Mate poller")

    db = Database(db_path)

    # If no env vars set, wait for the setup wizard to complete
    if not os.environ.get("LEAPMOTOR_USER") and not db.is_setup_complete():
        log.info("Waiting for setup wizard...")
        while not db.is_setup_complete():
            time.sleep(5)
        log.info("Setup complete — starting poller")
    cfg = load_config(db)
    _u = cfg["username"]
    _masked = (_u[:3] + "***" + _u[_u.find("@"):]) if "@" in _u else (_u[:3] + "***")
    device_id = db.get_or_create_device_id()
    log.info("Poller authenticating as account: %s | device_id: %s", _masked, device_id)
    client = LeapmotorMateClient(
        username=cfg["username"],
        password=cfg["password"],
        pin=cfg["pin"],
        cert_path=cfg["cert_path"],
        key_path=cfg["key_path"],
        device_id=device_id,
    )

    client.login()
    # Vehicle is known after login; register it in the DB
    from leapmotor_api import LeapmotorApiClient
    v = client._vehicle
    vehicle_id = db.ensure_vehicle(v.vin, v.car_type, getattr(v, "year", None))

    # First run: set battery capacity from per-model default
    # (will be overridable via setup wizard / settings UI)
    if not db.is_setup_complete():
        from db import default_capacity_for
        capacity = default_capacity_for(v.car_type)
        db.set_battery_capacity(capacity)
        log.info(
            "First run: %s default battery capacity set to %.1f kWh "
            "(change in Settings if you have a different variant)",
            v.car_type, capacity,
        )

    # Crash/restart recovery for open trips/charges happens on the first poll inside
    # Recorder._resume_or_close(), which RESUMES a still-ongoing session (avoiding
    # fragmentation) and only closes it if the activity has actually ended.
    recorder = Recorder(db, vehicle_id)

    log.info("Polling VIN %s (vehicle_id=%d)", v.vin, vehicle_id)

    last_relogin = 0.0   # rate-limit guard for session recovery
    mqtt_service = None   # optional MQTT → HA bridge, created lazily when enabled
    empty_status_count = 0  # consecutive "no live signals" responses (car asleep)

    while True:
        try:
            # Apply user-tunable poll cadence + charge-detection floor (Settings) live, each cycle
            try:
                recorder.set_poll_intervals(
                    int(db.get_setting("poll_parked", "30") or 30),
                    int(db.get_setting("poll_driving", "10") or 10),
                )
                set_charge_current_min(float(db.get_setting("charge_detect_min_a", "2.0") or 2.0))
            except (TypeError, ValueError):
                pass
            with _API_LOCK:
                data = client.get_status()
            recorder.process(data)

            # ABRP live telemetry (opt-in, off by default)
            if db.get_setting("abrp_enabled") == "1":
                abrp.send(db.get_secret("abrp_token"), data)

            # MQTT → Home Assistant bridge (opt-in, off by default)
            mqtt_service = _mqtt_tick(db, client, data, mqtt_service)

            interval = recorder.poll_interval
            # Boost window (set via POST /api/boost, e.g. an iPhone BT shortcut relayed
            # by HA when you get in the car): poll fast so we catch the trip start that
            # deep sleep would otherwise miss. Only matters while still parked — once
            # DRIVING the state machine already polls at 10s.
            try:
                boost_until = float(db.get_setting("boost_until", "0") or 0)
            except (TypeError, ValueError):
                boost_until = 0.0
            boosting = time.time() < boost_until and interval > 10
            if boosting:
                interval = 10
            log.info(
                "SOC %.1f%% | Range %d km | Speed %.0f km/h | State: %-8s | Gear: %s | Next poll: %ds%s",
                data.soc, data.range_km, data.speed_kmh,
                recorder.state.value, data.gear, interval,
                " (boost)" if boosting else "",
            )
            recorder.mark_online()
            empty_status_count = 0
        except KeyboardInterrupt:
            log.info("Stopped by user")
            break
        except EmptyStatusError:
            # Car asleep / not reporting live data (or a brief cloud hiccup) — NOT a
            # real failure. Retry at the normal cadence a couple of times in case it's
            # transient, then back off like any offline state. Recovers on its own once
            # the car reports again. (This used to surface as a scary "Poll error:
            # 'signal'" KeyError.) We log the back-off WARNING only once, not every
            # cycle: a parked car can stay asleep for hours and an ever-climbing
            # "after N tries" warning reads like an escalating failure when it isn't.
            empty_status_count += 1
            if empty_status_count >= 3:
                recorder.mark_offline()
            interval = recorder.poll_interval
            if empty_status_count < 3:
                log.info("Vehicle returned no live data (asleep or briefly unavailable) — "
                         "retry %d/3", empty_status_count)
            elif empty_status_count == 3:
                log.warning("Vehicle not reporting live data (car asleep or unavailable) — "
                            "backing off to %ds polling; recovers automatically when the car "
                            "reports again.", interval)
            # already backed off (count > 3): stay quiet so a sleeping car can't spam the log
        except Exception as exc:
            log.error("Poll error: %s", exc)
            recorder.mark_offline()
            interval = recorder.poll_interval
            # Self-heal: a vanished /tmp account-cert file (or an auth/token/connection
            # drop) makes every poll fail forever — the poller used to just keep erroring.
            # Force a fresh login to re-create the cert. Guarded to ~once/min so a rapid
            # double login can't trip Leapmotor's rate limiter.
            msg = str(exc).lower()
            recoverable = any(s in msg for s in (
                "certificate", "cert", "unauthorized", "token", "login",
                "verification", "connection", "timed out", "timeout", "ssl",
            ))
            if recoverable and time.time() - last_relogin > 60:
                last_relogin = time.time()
                try:
                    log.info("Attempting session recovery (re-login)…")
                    client.relogin()
                    log.info("Session recovered after re-login")
                except Exception as e2:  # noqa: BLE001
                    log.warning("Re-login failed, will retry next cycle: %s", e2)

        # Heartbeat for /healthz: proves the poll loop is alive (written every cycle,
        # even during offline/asleep backoff) regardless of whether the car reported.
        try:
            db.set_setting("last_loop_ts", str(time.time()))
        except Exception:  # noqa: BLE001
            pass

        # Daily DB pruning — opt-in via Settings (positions_retention_days; 0 = keep forever)
        try:
            ret = int(db.get_setting("positions_retention_days", "0") or 0)
            if ret > 0 and time.time() - float(db.get_setting("last_prune_ts", "0") or 0) > 86400:
                db.prune_positions(ret)
                db.set_setting("last_prune_ts", str(time.time()))
        except Exception as exc:  # noqa: BLE001
            log.warning("DB prune skipped: %s", exc)

        # Interruptible sleep: while parked we may be sleeping for minutes, so check the
        # boost flag every few seconds and wake immediately if one is requested.
        deadline = time.time() + interval
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            time.sleep(min(5.0, remaining))
            if interval > 10:
                try:
                    if float(db.get_setting("boost_until", "0") or 0) > time.time():
                        break   # boost just requested → poll now
                except (TypeError, ValueError):
                    pass

    client.close()
    db.close()


if __name__ == "__main__":
    main()

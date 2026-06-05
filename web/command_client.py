"""Persistent Leapmotor session — login once, reuse for all commands and status fetches."""
import os
import time
import types
import logging
import threading
from urllib.parse import quote

from leapmotor_api import LeapmotorApiClient
from leapmotor_api.client import build_signed_headers

log = logging.getLogger(__name__)

# App certificate location. The wizard writes the user-provided cert to /data/certs
# (persistent); fall back to the image-bundled CERT_DIR for local dev. Resolved at call
# time so certs uploaded mid-setup are picked up without a restart.
_DATA_CERT_DIR     = os.environ.get("DATA_CERT_DIR", "/data/certs")
_FALLBACK_CERT_DIR = os.environ.get("CERT_DIR", "certs")


def cert_dir() -> str:
    if (os.path.exists(os.path.join(_DATA_CERT_DIR, "app.crt"))
            and os.path.exists(os.path.join(_DATA_CERT_DIR, "app.key"))):
        return _DATA_CERT_DIR
    return _FALLBACK_CERT_DIR


def certs_present() -> bool:
    d = cert_dir()
    return (os.path.exists(os.path.join(d, "app.crt"))
            and os.path.exists(os.path.join(d, "app.key")))


def _status_has_signals(raw) -> bool:
    """True when a status response carries a live `signal` block."""
    sig = ((raw or {}).get("data") or {}).get("signal")
    return isinstance(sig, dict) and bool(sig)


# T03/EU status carries live data as named fields at the top level of `data` instead
# of a numeric-id `signal` sub-dict (C10/B10). Map per leapmotor-api 0.3.1; kept in
# sync with poller/client.py's copy.
_SIGNAL_TO_NAMED = {
    "47": "acInputSlowCharge", "1204": "soc", "100003": "preciseSoc",
    "1200": "chargeRemainTime", "1178": "batteryCurrent", "1177": "batteryVoltage",
    "1197": "dcInputFastCharge", "1149": "chargeState", "1182": "minBatteryTemp",
    "1186": "batteryThermalRequest", "3736": "chargeCompleted", "48": "healthyChargeEnabled",
    "3737": "chargeScheduleCancelledOnce",
    "3260": "expectedMileage", "2188": "liveRemainingRange", "3257": "maxRange", "3262": "rangeMode",
    "1319": "speed", "1318": "totalMileage", "1010": "gearStatus", "1944": "vehicleState",
    "1480": "parkingBrakeState", "6048": "speedLimit", "6047": "speedLimitUnit",
    "12054": "speedLimitActive",
    "3725": "latitude", "3724": "longitude",
    "1938": "acSwitch", "2183": "acSetting", "2184": "acSettingRight", "1349": "interiorTemp",
    "1943": "recirculationMode", "1945": "windshieldDefrost", "1946": "rearWindowHeating",
    "3713": "climateMode", "2669": "rapidCooling", "2681": "rapidHeating",
    "1939": "acOperateMode", "1941": "acAirVolume",
    "3727": "leftFrontWindowPercent", "3728": "rightFrontWindowPercent",
    "1879": "leftRearWindowPercent", "1880": "rightRearWindowPercent",
    "1693": "driverWindowStatus", "1694": "rightFrontWindowStatus",
    "1695": "leftRearWindowStatus", "1696": "rightRearWindowStatus",
    "1298": "driverDoorLockStatus", "1277": "lbcmDriverDoorStatus", "1278": "rbcmDriverDoorStatus",
    "1279": "lbcmLeftRearDoorStatus", "1280": "rbcmRightRearDoorStatus", "1281": "bbcmBackDoorStatus",
    "2667": "leftFrontTirePressure", "2653": "rightFrontTirePressure",
    "2646": "leftRearTirePressure", "2660": "rightRearTirePressure",
    "2641": "leftFrontTirePressureState", "2648": "rightFrontTirePressureState",
    "2655": "leftRearTirePressureState", "2662": "rightRearTirePressureState",
    "1256": "bcmKeyPositionOn1", "1257": "bcmKeyPositionOn2", "1258": "bcmKeyPositionOn3",
    "2100": "driverSeatHeating", "2101": "driverSeatVentilation",
    "2118": "passengerSeatHeating", "2119": "passengerSeatVentilation",
    "1816": "steeringWheelHeating", "1624": "steeringWheelHeaterMinutes",
    "1255": "vehicleSecurityActive", "3636": "sentryMode",
    "49": "leftMirrorHeating", "50": "rightMirrorHeating", "1724": "roofOpening",
}


def _named_fields_to_signal(data: dict) -> dict | None:
    """Rebuild a numeric-id `signal` dict from a T03/EU named-field response."""
    if not isinstance(data, dict):
        return None
    sig = {sid: data[name] for sid, name in _SIGNAL_TO_NAMED.items()
           if data.get(name) is not None}
    return sig or None


def _b10_patched_get_vehicle_raw_status(self, vehicle):
    """B10 status lives under the /c10 path; SHARED cars need `carId` in the body or
    the cloud returns an empty signal block. See poller/client.py for the full
    rationale — both layers patch the same pip-lib method the same way."""
    car_type_path = "c10" if vehicle.car_type.upper() == "B10" else vehicle.car_type.lower()

    def _fetch(body: str):
        headers = build_signed_headers(
            sign_key=self.sign_key,
            device_id=self.device_id,
            vin=vehicle.vin,
            language=self.language,
        )
        headers.update(self._auth_headers(content_type="application/x-www-form-urlencoded"))
        response = self._post(
            path=f"/carownerservice/oversea/vehicle/v1/status/get/{car_type_path}",
            headers=headers,
            data=body,
            cert=self.account_cert,
        )
        return self._parse_api_body(response["status_code"], response["body"], "vehicle status")

    vin_q = quote(vehicle.vin, safe="")
    raw = _fetch(f"vin={vin_q}")

    car_id = getattr(vehicle, "car_id", None)
    if car_id and getattr(vehicle, "is_shared", False) and not _status_has_signals(raw):
        try:
            shared = _fetch(f"vin={vin_q}&carId={quote(str(car_id), safe='')}")
        except Exception:  # noqa: BLE001 — keep the original response on any error
            shared = None
        if _status_has_signals(shared):
            return shared
    return raw


def _get_credentials() -> tuple[str, str, str]:
    """Read credentials from DB settings, falling back to env vars for dev."""
    try:
        import sys, pathlib
        sys.path.insert(0, str(pathlib.Path(__file__).parent))
        import db_reader as _dr
        user = _dr.get_setting("leapmotor_user") or os.environ.get("LEAPMOTOR_USER", "")
        pwd  = _dr.get_secret("leapmotor_pass") or os.environ.get("LEAPMOTOR_PASS", "")
        pin  = _dr.get_secret("leapmotor_pin")  or os.environ.get("LEAPMOTOR_PIN", "")
    except Exception:
        user = os.environ.get("LEAPMOTOR_USER", "")
        pwd  = os.environ.get("LEAPMOTOR_PASS", "")
        pin  = os.environ.get("LEAPMOTOR_PIN", "")
    return user, pwd, pin


def _make_client() -> LeapmotorApiClient:
    user, pwd, pin = _get_credentials()
    try:
        import sys, pathlib
        sys.path.insert(0, str(pathlib.Path(__file__).parent))
        import db_reader as _dr
        device_id = _dr.get_or_create_device_id()
    except Exception:
        device_id = None
    api = LeapmotorApiClient(
        username=user,
        password=pwd,
        operation_password=pin,
        app_cert_path=os.path.join(cert_dir(), "app.crt"),
        app_key_path=os.path.join(cert_dir(), "app.key"),
        language="en-US",
        device_id=device_id,
    )
    api._get_vehicle_raw_status = types.MethodType(_b10_patched_get_vehicle_raw_status, api)
    import session_share
    session_share.install(api)   # share ONE token with the poller (avoid mutual eviction)
    return api


class LeapmotorSession:
    """Login once, reuse token for all subsequent API calls."""

    def __init__(self):
        self._api: LeapmotorApiClient | None = None
        self._vehicle = None
        self._lock = threading.Lock()

    def _connect(self):
        if self._api is not None:
            return
        self._api = _make_client()
        self._api.login()
        vehicles = self._api.get_vehicle_list()
        if not vehicles:
            raise RuntimeError("No vehicle found on this account")
        self._vehicle = vehicles[0]
        log.info("Session started — VIN %s  model %s", self._vehicle.vin, self._vehicle.car_type)

    def _is_auth_error(self, err: str) -> bool:
        low = err.lower()
        return any(k in low for k in ("token", "verification", "unauthori", "login"))

    def _is_connection_error(self, err: str) -> bool:
        low = err.lower()
        return any(k in low for k in ("connection aborted", "remotedisconnected", "connectionerror",
                                       "connection reset", "broken pipe", "timed out"))

    def _reset(self):
        try:
            if self._api:
                self._api.close()
        except Exception:
            pass
        self._api = None
        self._vehicle = None
        log.info("Session reset — will re-login on next call")

    def execute(self, action_fn) -> tuple[bool, str]:
        with self._lock:
            for attempt in range(2):
                try:
                    self._connect()
                    action_fn(self._api, self._vehicle.vin)
                    return True, "OK"
                except Exception as e:
                    err = str(e)
                    log.error("Command error (attempt %d): %s", attempt + 1, err)
                    if self._is_connection_error(err):
                        # Stale keep-alive connection — reset and retry immediately
                        self._reset()
                        if attempt == 1:
                            return False, err
                        continue
                    if not self._is_auth_error(err):
                        return False, err
                    self._reset()
                    if attempt == 1:
                        return False, err
                    time.sleep(3)  # avoid rate limit before re-login
            return False, "Unknown error"

    def get_fresh_signals(self) -> dict | None:
        with self._lock:
            for attempt in range(2):
                try:
                    self._connect()
                    raw = self._api.get_vehicle_raw_status(self._vehicle)
                    data = (raw or {}).get("data") or {}
                    # C10/B10: numeric `signal` dict. T03/EU: named fields at top level.
                    return data.get("signal") or _named_fields_to_signal(data)
                except Exception as e:
                    log.warning("Status fetch (attempt %d): %s", attempt + 1, e)
                    self._reset()
            return None

    def get_charge_plan(self) -> dict | None:
        """Return charge plan dict with at least 'charge_limit_percent' key."""
        with self._lock:
            for attempt in range(2):
                try:
                    self._connect()
                    raw = self._api.get_vehicle_raw_status(self._vehicle)
                    plan = ((raw.get("data") or {}).get("config") or {}).get("3") or {}
                    return {
                        "charge_limit_percent": plan.get("percent"),
                        "charge_enabled": plan.get("isEnable"),
                        "start_time": plan.get("beginTime"),
                        "end_time": plan.get("endTime"),
                    }
                except Exception as e:
                    log.warning("Charge plan fetch (attempt %d): %s", attempt + 1, e)
                    self._reset()
            return None

    def get_car_picture(self) -> bytes | None:
        """Static owner vehicle PNG, extracted from the car-picture package ZIP
        (android/xxhdpi/carpic_for_tripsum.png) — same as the HA integration's
        image.leapmotor_vehicle_picture. Rarely changes → caller should cache."""
        import io, zipfile
        with self._lock:
            for attempt in range(2):
                try:
                    self._connect()
                    meta = self._api.get_car_picture(self._vehicle)
                    key = (meta.get("data") or {}).get("key") if isinstance(meta, dict) else None
                    if not key:
                        return None
                    pkg = self._api.download_car_picture_package(picture_key=key)
                    with zipfile.ZipFile(io.BytesIO(pkg)) as z:
                        return z.read("android/xxhdpi/carpic_for_tripsum.png")
                except Exception as e:
                    log.warning("Car picture fetch (attempt %d): %s", attempt + 1, e)
                    self._reset()
            return None


    def get_energy_breakdown(self) -> dict | None:
        """Last-week energy split (driving / A/C / other) from the cloud endpoint
        getLastweekEC. Not in the pip package — replicated here using its signing
        primitives (sign_key HMAC-SHA256), same as the kerniger/markoceri HA integration."""
        import time, hmac, hashlib, random, requests
        from leapmotor_api import const as C
        with self._lock:
            for attempt in range(2):
                try:
                    self._connect()
                    api = self._api
                    vin = self._vehicle.vin
                    now = int(time.time())
                    end, begin = now, now - 7 * 24 * 3600
                    nonce = str(random.randint(100000, 9999999))
                    ts = str(int(time.time() * 1000))
                    sign_input = "".join([
                        api.language, str(begin), vin, str(C.DEFAULT_CHANNEL), api.device_id,
                        str(C.DEFAULT_DEVICE_TYPE), str(end), nonce, C.DEFAULT_SOURCE, ts, C.DEFAULT_APP_VERSION,
                    ])
                    sign = hmac.new(api.sign_key, sign_input.encode("utf-8"), hashlib.sha256).hexdigest()
                    hdr = {
                        "acceptLanguage": api.language, "channel": str(C.DEFAULT_CHANNEL),
                        "deviceType": str(C.DEFAULT_DEVICE_TYPE), "X-P12_ENC_ALG": str(C.DEFAULT_P12_ENC_ALG),
                        "source": C.DEFAULT_SOURCE, "version": C.DEFAULT_APP_VERSION, "nonce": nonce,
                        "deviceId": api.device_id, "timestamp": ts, "sign": sign,
                    }
                    hdr.update(api._auth_headers(content_type="application/x-www-form-urlencoded"))
                    body = f"endtime={end}&begintime={begin}&carvin={requests.utils.quote(vin, safe='')}"
                    r = api._post(path="/carownerservice/oversea/drivingRecord/v1/getLastweekEC",
                                  headers=hdr, data=body, cert=api.account_cert)
                    d = (api._parse_api_body(r["status_code"], r["body"], "energy breakdown") or {}).get("data") or {}
                    drv = float(d.get("driverEC") or 0)
                    ac = float(d.get("acEC") or 0)
                    oth = float(d.get("otherEC") or 0)
                    total = drv + ac + oth
                    pct = (lambda v: round(v / total * 100, 1)) if total > 0 else (lambda v: 0)
                    return {
                        "driving_kwh": round(drv, 1), "ac_kwh": round(ac, 1), "other_kwh": round(oth, 1),
                        "total_kwh": round(total, 1),
                        "driving_pct": pct(drv), "ac_pct": pct(ac), "other_pct": pct(oth),
                    }
                except Exception as e:
                    log.warning("Energy breakdown fetch (attempt %d): %s", attempt + 1, e)
                    self._reset()
            return None


    def get_consumption_rank(self) -> dict | None:
        """6-week energy-consumption trend (kWh/100km per week) + driver ranking, from
        the cloud endpoint getLastNweeks100kmECAndRank. Not in the pip package."""
        import time, hmac, hashlib, random, requests
        from leapmotor_api import const as C
        with self._lock:
            for attempt in range(2):
                try:
                    self._connect()
                    api = self._api
                    vin = self._vehicle.vin
                    nonce = str(random.randint(100000, 9999999))
                    ts = str(int(time.time() * 1000))
                    sign_input = "".join([
                        api.language, vin, str(C.DEFAULT_CHANNEL), api.device_id,
                        str(C.DEFAULT_DEVICE_TYPE), nonce, C.DEFAULT_SOURCE, ts, C.DEFAULT_APP_VERSION,
                    ])
                    sign = hmac.new(api.sign_key, sign_input.encode("utf-8"), hashlib.sha256).hexdigest()
                    hdr = {
                        "acceptLanguage": api.language, "channel": str(C.DEFAULT_CHANNEL),
                        "deviceType": str(C.DEFAULT_DEVICE_TYPE), "X-P12_ENC_ALG": str(C.DEFAULT_P12_ENC_ALG),
                        "source": C.DEFAULT_SOURCE, "version": C.DEFAULT_APP_VERSION, "nonce": nonce,
                        "deviceId": api.device_id, "timestamp": ts, "sign": sign,
                    }
                    hdr.update(api._auth_headers(content_type="application/x-www-form-urlencoded"))
                    body = f"carvin={requests.utils.quote(vin, safe='')}"
                    r = api._post(path="/carownerservice/oversea/drivingRecord/v1/getLastNweeks100kmECAndRank",
                                  headers=hdr, data=body, cert=api.account_cert)
                    d = (api._parse_api_body(r["status_code"], r["body"], "consumption rank") or {}).get("data") or {}
                    rank = d.get("rankResult") or {}
                    weeks = []
                    for w in (d.get("weeklyEC") or []):
                        try:
                            weeks.append({"start": w.get("weekStart"), "end": w.get("weekEnd"),
                                          "ec": float(w.get("hundredKmEC") or 0)})
                        except (TypeError, ValueError):
                            pass
                    return {"rank": rank.get("rank"),
                            "current_ec": round(float(rank.get("hundredKmEC") or 0), 1),
                            "weeks": weeks}
                except Exception as e:
                    log.warning("Consumption rank fetch (attempt %d): %s", attempt + 1, e)
                    self._reset()
            return None


_session = LeapmotorSession()


def get_car_picture() -> bytes | None:
    return _session.get_car_picture()


def get_energy_breakdown() -> dict | None:
    return _session.get_energy_breakdown()


def get_consumption_rank() -> dict | None:
    return _session.get_consumption_rank()


def detect_vehicle(user: str, pwd: str, pin: str) -> dict:
    """Login with provided credentials, return vehicle info. Does NOT save to DB."""
    try:
        api = LeapmotorApiClient(
            username=user,
            password=pwd,
            operation_password=pin,
            app_cert_path=os.path.join(cert_dir(), "app.crt"),
            app_key_path=os.path.join(cert_dir(), "app.key"),
            language="en-US",
        )
        api.login()
        vehicles = api.get_vehicle_list()
        if not vehicles:
            return {"error": "No vehicle found on this account"}
        v = vehicles[0]
        car_type = v.car_type.upper()
        try:
            api.close()
        except Exception:
            pass
        return {"vin": v.vin, "car_type": car_type}
    except Exception as e:
        return {"error": str(e)}


def get_fresh_signals() -> dict | None:
    return _session.get_fresh_signals()

def get_charge_plan() -> dict | None:
    return _session.get_charge_plan()

def set_charge_limit(percent: int):
    return _session.execute(lambda api, vin: api.set_charge_limit(vin, percent))


def lock():              return _session.execute(lambda api, vin: api.lock_vehicle(vin))
def unlock():            return _session.execute(lambda api, vin: api.unlock_vehicle(vin))
def open_trunk():        return _session.execute(lambda api, vin: api.open_trunk(vin))
def close_trunk():       return _session.execute(lambda api, vin: api.close_trunk(vin))
# No public find_car() on the pip client — drive the registered action (cmd_id 120,
# {"value":"true"}) through the generic remote-control entry point.
def find_car():          return _session.execute(lambda api, vin: api._remote_control(vin=vin, action="find_car"))
def ac_on():             return _session.execute(lambda api, vin: api.ac_switch(vin))
def quick_cool():        return _session.execute(lambda api, vin: api.quick_cool(vin))
def quick_heat():        return _session.execute(lambda api, vin: api.quick_heat(vin))
def windshield_defrost():return _session.execute(lambda api, vin: api.windshield_defrost(vin))
def open_windows():      return _session.execute(lambda api, vin: api.open_windows(vin, value="2"))
def close_windows():     return _session.execute(lambda api, vin: api.close_windows(vin, value="0"))
def battery_preheat():   return _session.execute(lambda api, vin: api.battery_preheat(vin))
def open_sunshade():     return _session.execute(lambda api, vin: api.open_sunshade(vin))
def close_sunshade():    return _session.execute(lambda api, vin: api.close_sunshade(vin))
def send_destination(name, address, lat, lon):
    """Push a navigation destination to the car (cmd_id 180, no PIN)."""
    return _session.execute(lambda api, vin: api.send_destination(
        vin, address=address, address_name=name,
        latitude=float(lat), longitude=float(lon)))

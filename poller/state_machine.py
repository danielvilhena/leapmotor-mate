"""
Adaptive polling state machine.

States and intervals:
  PARKED_SLEEP   5 min   — no activity for 30+ min (car sleeping)
  PARKED_ACTIVE  60s     — normal parked, nothing unusual
  PARKED_ALERT   15s     — something changed (door/lock/temp): drive imminent
  DRIVING        10s     — speed > 0 or gear D
  CHARGING       60s     — plugged in
  OFFLINE        15 min  — API unreachable after 3 consecutive errors

Transitions (all independent of HA and phone):
  UNKNOWN/OFFLINE     → PARKED_ACTIVE  first successful poll
  PARKED_SLEEP        → PARKED_ACTIVE  fingerprint changes (any signal)
  PARKED_ACTIVE       → PARKED_ALERT   fingerprint changes
  PARKED_ACTIVE       → PARKED_SLEEP   no change for SLEEP_AFTER_S (30 min)
  PARKED_ALERT        → DRIVING        speed > 0 or gear D
  PARKED_ALERT        → PARKED_ACTIVE  no drive within ALERT_EXPIRES_S (5 min)
  DRIVING             → PARKED_ACTIVE  gear P confirmed twice (2 × 10s)
  ANY_PARKED          → CHARGING       charging_status > 0
  CHARGING            → PARKED_ACTIVE  charging_status == 0
  ANY                 → OFFLINE        3 consecutive API errors
"""
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from client import VehicleData

log = logging.getLogger(__name__)

SLEEP_AFTER_S   = 1800   # 30 min without changes → PARKED_SLEEP
ALERT_EXPIRES_S = 300    # 5 min in PARKED_ALERT without driving → back to ACTIVE
PARKED_CONFIRM  = 2      # consecutive parked readings to end a trip


class State(Enum):
    UNKNOWN       = "unknown"
    PARKED_SLEEP  = "parked_sleep"
    PARKED_ACTIVE = "parked_active"
    PARKED_ALERT  = "parked_alert"
    DRIVING       = "driving"
    CHARGING      = "charging"
    OFFLINE       = "offline"


# Default poll cadence (seconds). Polling the Leapmotor cloud does NOT wake/drain the
# car (it reads the last cloud-reported state), so a steady ~30s parked cadence is safe
# and keeps Mate independent (no HA/boost needed to catch a trip start). User-tunable.
DEFAULT_POLL_PARKED  = 30
DEFAULT_POLL_DRIVING = 10
OFFLINE_INTERVAL     = 900   # back off when the API is unreachable

_PARKED_STATES = {State.PARKED_SLEEP, State.PARKED_ACTIVE, State.PARKED_ALERT}


@dataclass
class StateEvent:
    from_state: State
    to_state: State
    data: Optional[VehicleData]


@dataclass
class StateMachine:
    state: State = State.UNKNOWN
    poll_parked: int           = DEFAULT_POLL_PARKED    # tunable from Settings
    poll_driving: int          = DEFAULT_POLL_DRIVING
    _prev_fp: Optional[tuple]  = field(default=None,  repr=False)
    _last_change_ts: float     = field(default=0.0,   repr=False)
    _alert_start_ts: float     = field(default=0.0,   repr=False)
    _parked_count: int         = field(default=0,     repr=False)
    _error_count: int          = field(default=0,     repr=False)

    def update(self, data: VehicleData) -> list[StateEvent]:
        self._error_count = 0
        events: list[StateEvent] = []
        now = time.monotonic()

        is_driving  = data.vehicle_state == "driving" or data.speed_kmh > 1
        # Plug inserted OR charging active = charge session
        # plug_connected alone is enough to close the trip immediately
        is_charging = data.charging_status > 0 or data.plug_connected
        fp          = data.fingerprint()
        fp_changed  = (self._prev_fp is not None) and (fp != self._prev_fp)

        if fp_changed:
            self._last_change_ts = now
        if self._prev_fp is None:
            self._last_change_ts = now
        self._prev_fp = fp

        # ── UNKNOWN / OFFLINE → first successful poll ─────────────────────
        if self.state in (State.UNKNOWN, State.OFFLINE):
            if is_driving:
                events.append(self._go(State.DRIVING, data))
            elif is_charging:
                events.append(self._go(State.CHARGING, data))
            else:
                events.append(self._go(State.PARKED_ACTIVE, data))
            return events

        # ── Any parked → CHARGING ─────────────────────────────────────────
        if self.state in _PARKED_STATES and is_charging:
            events.append(self._go(State.CHARGING, data))
            return events

        # ── Any parked → DRIVING ──────────────────────────────────────────
        if self.state in _PARKED_STATES and is_driving:
            self._parked_count = 0
            events.append(self._go(State.DRIVING, data))
            return events

        # ── PARKED_SLEEP ──────────────────────────────────────────────────
        if self.state == State.PARKED_SLEEP:
            if fp_changed:
                events.append(self._go(State.PARKED_ACTIVE, data))

        # ── PARKED_ACTIVE ─────────────────────────────────────────────────
        elif self.state == State.PARKED_ACTIVE:
            idle_s = now - self._last_change_ts
            if fp_changed:
                self._alert_start_ts = now
                events.append(self._go(State.PARKED_ALERT, data))
            elif idle_s >= SLEEP_AFTER_S:
                events.append(self._go(State.PARKED_SLEEP, data))

        # ── PARKED_ALERT ──────────────────────────────────────────────────
        elif self.state == State.PARKED_ALERT:
            alert_age_s = now - self._alert_start_ts
            if fp_changed:
                self._alert_start_ts = now  # reset timer on new activity
            elif alert_age_s >= ALERT_EXPIRES_S:
                events.append(self._go(State.PARKED_ACTIVE, data))

        # ── DRIVING ───────────────────────────────────────────────────────
        elif self.state == State.DRIVING:
            if is_charging:
                self._parked_count = 0
                events.append(self._go(State.CHARGING, data))
            elif not is_driving:
                self._parked_count += 1
                if self._parked_count >= PARKED_CONFIRM:
                    self._parked_count = 0
                    self._alert_start_ts = now
                    events.append(self._go(State.PARKED_ACTIVE, data))
            else:
                self._parked_count = 0

        # ── CHARGING ──────────────────────────────────────────────────────
        elif self.state == State.CHARGING:
            if not is_charging:
                events.append(self._go(State.PARKED_ACTIVE, data))

        return events

    def mark_offline(self) -> list[StateEvent]:
        self._error_count += 1
        if self._error_count >= 3 and self.state != State.OFFLINE:
            return [self._go(State.OFFLINE, None)]
        return []

    def mark_online(self) -> list[StateEvent]:
        self._error_count = 0
        return []

    def _go(self, new_state: State, data) -> StateEvent:
        event = StateEvent(from_state=self.state, to_state=new_state, data=data)
        self.state = new_state
        log.info(
            "State: %-14s → %-14s  (poll: %ds)",
            event.from_state.value, new_state.value, self.poll_interval,
        )
        return event

    @property
    def poll_interval(self) -> int:
        if self.state in (State.DRIVING, State.PARKED_ALERT):
            return self.poll_driving        # active / drive imminent → fast
        if self.state == State.OFFLINE:
            return OFFLINE_INTERVAL
        if self.state == State.UNKNOWN:
            return min(self.poll_parked, 30)
        return self.poll_parked             # PARKED_SLEEP/ACTIVE, CHARGING

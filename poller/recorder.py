"""
Recorder: reacts to state machine events to persist trips, charges, and positions.
"""
import logging
from typing import Optional

from db import Database
from state_machine import State, StateMachine, StateEvent, _PARKED_STATES
from client import VehicleData

log = logging.getLogger(__name__)


class Recorder:
    def __init__(self, db: Database, vehicle_id: int):
        self._db = db
        self._vehicle_id = vehicle_id
        self._sm = StateMachine()
        self._active_trip_id: Optional[int] = None
        self._active_charge_id: Optional[int] = None
        self._regen_kwh: float = 0.0
        self._max_charge_kw: float = 0.0
        self._started: bool = False

    @property
    def state(self) -> State:
        return self._sm.state

    @property
    def poll_interval(self) -> int:
        return self._sm.poll_interval

    def set_poll_intervals(self, parked: int, driving: int) -> None:
        self._sm.poll_parked = parked
        self._sm.poll_driving = driving

    def _resume_or_close(self, data: VehicleData) -> None:
        """At startup, reconcile sessions left open by a previous run (poller/HA
        restart, crash). If the activity is STILL ongoing, RESUME the open session
        instead of closing it — this avoids fragmenting one physical charge/trip into
        multiple DB records. If it's no longer ongoing, close it (crash recovery)."""
        is_charging = data.charging_status > 0 or data.plug_connected
        is_driving  = data.vehicle_state == "driving" or data.speed_kmh > 1

        open_charge = self._db.get_open_charge(self._vehicle_id)
        if open_charge:
            if is_charging:
                self._active_charge_id = open_charge["id"]
                self._max_charge_kw = open_charge["max_power_kw"] or 0.0
                self._sm.state = State.CHARGING
                log.info("Resumed open charge #%d (car still charging)", open_charge["id"])
            else:
                self._db.close_orphan_charges(self._vehicle_id)

        open_trip = self._db.get_open_trip(self._vehicle_id)
        if open_trip:
            if is_driving and not is_charging:
                self._active_trip_id = open_trip["id"]
                self._sm.state = State.DRIVING
                log.info("Resumed open trip #%d (car still driving)", open_trip["id"])
            else:
                self._db.close_orphan_trips(self._vehicle_id)

    def process(self, data: VehicleData) -> None:
        """Called every poll cycle with fresh vehicle data."""
        if not self._started:
            self._started = True
            self._resume_or_close(data)

        self._db.save_position(self._vehicle_id, data)

        events = self._sm.update(data)
        for event in events:
            self._handle_event(event, data)

        # During active trip: record GPS point and accumulate regen.
        # Regen = energy flowing INTO the pack while unplugged. charge_power_kw is now a
        # magnitude (|current×voltage|), so we gate on a clearly-negative charge current
        # (1178 < 0 = into pack, per the Leapmotor convention). The B10 sign still needs
        # on-road verification — gating this way stays conservative: at worst it counts 0,
        # never mistaking driving discharge for regen.
        if self._sm.state == State.DRIVING and self._active_trip_id:
            self._db.add_trip_position(self._active_trip_id, data)
            if not data.plug_connected and data.charge_current_a < -3.0:
                self._regen_kwh += data.charge_power_kw * (10 / 3600)

        # During active charge: track peak power (persisted so it survives a restart)
        if self._sm.state == State.CHARGING and self._active_charge_id:
            if data.charge_power_kw > self._max_charge_kw:
                self._max_charge_kw = data.charge_power_kw
                self._db.update_charge_max_power(self._active_charge_id, self._max_charge_kw)

    def mark_offline(self) -> None:
        events = self._sm.mark_offline()
        for e in events:
            self._handle_event(e, None)

    def mark_online(self) -> None:
        events = self._sm.mark_online()
        for e in events:
            self._handle_event(e, None)

    def _handle_event(self, event: StateEvent, data: Optional[VehicleData]) -> None:
        frm, to = event.from_state, event.to_state

        if to == State.DRIVING:
            self._regen_kwh = 0.0
            self._active_trip_id = self._db.create_trip(self._vehicle_id, data)

        elif frm == State.DRIVING and to in _PARKED_STATES:
            if self._active_trip_id and data:
                self._db.finalize_trip(self._active_trip_id, data, self._regen_kwh)
            self._active_trip_id = None
            self._regen_kwh = 0.0

        elif to == State.CHARGING:
            if self._active_trip_id and data:
                # Plug inserted while driving → trip closed immediately, no 20s wait
                self._db.finalize_trip(self._active_trip_id, data, self._regen_kwh)
                self._active_trip_id = None
                self._regen_kwh = 0.0
            self._max_charge_kw = 0.0
            if data:
                self._active_charge_id = self._db.create_charge(self._vehicle_id, data)

        elif frm == State.CHARGING and to in _PARKED_STATES:
            if self._active_charge_id and data:
                self._db.finalize_charge(
                    self._active_charge_id, data,
                    max_power_kw=self._max_charge_kw,
                )
            self._active_charge_id = None
            self._max_charge_kw = 0.0

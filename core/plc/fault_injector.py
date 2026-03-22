"""
core/plc/fault_injector.py
Fault Injection Engine

Simulates realistic fault scenarios for:
  - ML training data generation (fault classifier needs labeled faults)
  - SCADA alarm testing
  - RCA module validation

Fault types (mirrors 4-class ML classifier):
  0 = Logic issue      — PLC rung logic error, unexpected state
  1 = Sensor failure   — sensor drift, stuck, out-of-range
  2 = Network issue    — comm loss, timeout
  3 = Human error      — wrong setpoint, mode change
"""
from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass, field
from enum import IntEnum
import logging
logger = logging.getLogger("fault_injector")
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.plc.runtime import TagDatabase


class FaultType(IntEnum):
    LOGIC   = 0
    SENSOR  = 1
    NETWORK = 2
    HUMAN   = 3


@dataclass
class FaultEvent:
    """Records a single fault event — fed to RCA module."""
    fault_id:    str
    fault_type:  FaultType
    tag_affected:str
    timestamp:   float = field(default_factory=time.time)
    description: str   = ""
    resolved:    bool  = False
    resolved_at: float = 0.0

    def resolve(self) -> None:
        self.resolved    = True
        self.resolved_at = time.time()

    @property
    def duration_sec(self) -> float:
        if self.resolved:
            return self.resolved_at - self.timestamp
        return time.time() - self.timestamp


class FaultInjector:
    """
    Probabilistic fault injector — creates realistic fault scenarios
    at configurable rates. All faults are labeled with FaultType
    so ML classifier can be trained on actual simulation output.
    """

    # Fault probability per second for each scenario
    FAULT_RATES = {
        "motor_overload":    0.002,   # ~1 event per 500s
        "sensor_drift":      0.003,
        "comm_loss":         0.001,
        "conveyor_jam":      0.002,
        "estop":             0.0005,
    }

    def __init__(self, db: "TagDatabase") -> None:
        self.db     = db
        self.history: list[FaultEvent] = []
        self._active: dict[str, FaultEvent] = {}
        self.enabled = True

    async def tick(self, dt: float) -> None:
        """Called every scan cycle — probabilistically inject faults."""
        if not self.enabled:
            return

        for scenario, rate_per_sec in self.FAULT_RATES.items():
            if scenario in self._active:
                continue   # Don't double-inject
            if random.random() < rate_per_sec * dt:
                await self._inject(scenario)

        # Auto-resolve faults after random duration
        for scenario, event in list(self._active.items()):
            if time.time() - event.timestamp > random.uniform(5, 30):
                await self._resolve(scenario, event)

    async def _inject(self, scenario: str) -> None:
        """Inject a specific fault scenario."""
        if scenario == "motor_overload":
            await self.inject_motor_overload("Mixer_Motor")

        elif scenario == "sensor_drift":
            event = FaultEvent(
                fault_id=f"F{int(time.time()*1000)}",
                fault_type=FaultType.SENSOR,
                tag_affected="Filler_Temp.EUValue",
                description="Temperature sensor drift — reading stuck at last value",
            )
            # Inject: stick sensor at wrong value
            self.db.write_sync("Filler_Temp.EUValue", 999.9)
            self.db.write_sync("Filler_Temp.Fault",   True)
            self._active[scenario] = event
            self.history.append(event)
            logger.warning(f"[FAULT INJECT] Sensor drift on Filler_Temp")

        elif scenario == "comm_loss":
            event = FaultEvent(
                fault_id=f"F{int(time.time()*1000)}",
                fault_type=FaultType.NETWORK,
                tag_affected="Mixer_Motor.Running",
                description="EtherNet/IP communication loss — tags frozen",
            )
            self._active[scenario] = event
            self.history.append(event)
            logger.warning(f"[FAULT INJECT] Comm loss simulated")

        elif scenario == "conveyor_jam":
            event = FaultEvent(
                fault_id=f"F{int(time.time()*1000)}",
                fault_type=FaultType.LOGIC,
                tag_affected="Conveyor_Main.JamDetected",
                description="Conveyor jam detected — line interlock triggered",
            )
            self.db.write_sync("Conveyor_Main.JamDetected", True)
            self.db.write_sync("Conveyor_Main.Running",     False)
            self._active[scenario] = event
            self.history.append(event)
            logger.warning(f"[FAULT INJECT] Conveyor jam")

        elif scenario == "estop":
            event = FaultEvent(
                fault_id=f"F{int(time.time()*1000)}",
                fault_type=FaultType.HUMAN,
                tag_affected="Line.E_Stop_Active",
                description="Emergency stop activated by operator",
            )
            self.db.write_sync("Line.E_Stop_Active", True)
            self._active[scenario] = event
            self.history.append(event)
            logger.warning(f"[FAULT INJECT] E-Stop activated")

    async def _resolve(self, scenario: str, event: FaultEvent) -> None:
        """Auto-resolve fault after duration."""
        event.resolve()
        del self._active[scenario]

        if scenario == "sensor_drift":
            self.db.write_sync("Filler_Temp.Fault", False)
        elif scenario == "conveyor_jam":
            self.db.write_sync("Conveyor_Main.JamDetected", False)
        elif scenario == "estop":
            self.db.write_sync("Line.E_Stop_Active", False)

        logger.info(f"[FAULT RESOLVE] {scenario} resolved after {event.duration_sec:.1f}s")

    async def inject_motor_overload(self, motor_tag: str) -> None:
        """Manually inject motor overload fault."""
        scenario = "motor_overload"
        event = FaultEvent(
            fault_id=f"F{int(time.time()*1000)}",
            fault_type=FaultType.LOGIC,
            tag_affected=f"{motor_tag}.Faulted",
            description=f"{motor_tag} overload — current exceeded 21A threshold",
        )
        self.db.write_sync(f"{motor_tag}.Faulted",   True)
        self.db.write_sync(f"{motor_tag}.FaultCode", 1)
        self.db.write_sync(f"{motor_tag}.Running",   False)
        self.db.write_sync(f"{motor_tag}.RunCmd",    False)
        self._active[scenario] = event
        self.history.append(event)
        logger.error(f"[FAULT INJECT] Motor overload on {motor_tag}")

    def get_fault_history(self) -> list[dict]:
        """Export fault history for RCA module and ML training."""
        return [
            {
                "fault_id":     e.fault_id,
                "fault_type":   e.fault_type.name,
                "fault_type_id":int(e.fault_type),
                "tag_affected": e.tag_affected,
                "timestamp":    e.timestamp,
                "description":  e.description,
                "resolved":     e.resolved,
                "duration_sec": e.duration_sec,
            }
            for e in self.history
        ]

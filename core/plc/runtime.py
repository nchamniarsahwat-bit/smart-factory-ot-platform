"""
core/plc/runtime.py
Allen-Bradley ControlLogix Deterministic Scan Cycle Engine

Mirrors real ControlLogix L7x execution model:
  - Continuous Task   : free-running, lowest priority
  - Periodic Task     : fixed interval (e.g. 10ms), preempts Continuous
  - Event Task        : triggered by tag value change, highest priority

Run:  python -m core.plc.runtime
"""
from __future__ import annotations

import asyncio
import time
import random
import math
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Callable, Awaitable
import logging
logger = logging.getLogger("plc")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

from core.plc.tags import (
    Tag, TagType, TagScope,
    MotorUDT, ConveyorUDT, SensorUDT, ProductionLineUDT,
)
from core.plc.instructions import InstructionExecutor
from core.plc.fault_injector import FaultInjector


# ── Task Types ────────────────────────────────────────────────────────────────

class TaskType(IntEnum):
    CONTINUOUS = 0   # Free-running, always scanning
    PERIODIC   = 1   # Fixed interval — preempts Continuous
    EVENT      = 2   # Tag-change triggered — highest priority


@dataclass
class PLCTask:
    """
    Mirrors a ControlLogix Task object.
    Each task has a list of programs (routines) it executes.
    """
    name:       str
    task_type:  TaskType
    period_ms:  float = 10.0      # Periodic only — scan interval
    priority:   int   = 10        # 1 (highest) – 15 (lowest)
    watchdog_ms:float = 500.0     # Abort if scan exceeds this

    programs:   list[str] = field(default_factory=list)
    scan_count: int        = 0
    last_scan_ms: float    = 0.0
    max_scan_ms:  float    = 0.0
    _running:   bool       = False


# ── Tag Database (mirrors Controller Tag DB) ──────────────────────────────────

class TagDatabase:
    """
    In-memory tag store — equivalent to the ControlLogix controller tag database.
    Thread-safe read/write via asyncio.Lock.
    """

    def __init__(self) -> None:
        self._tags:  dict[str, Tag] = {}
        self._lock   = asyncio.Lock()
        self._subscribers: dict[str, list[Callable]] = {}

    async def write(self, name: str, value) -> None:
        async with self._lock:
            if name in self._tags:
                old = self._tags[name].read()
                self._tags[name].write(value)
                self._tags[name].last_updated = time.time()
                if old != value and name in self._subscribers:
                    for cb in self._subscribers[name]:
                        asyncio.create_task(cb(name, old, value))

    async def read(self, name: str):
        async with self._lock:
            tag = self._tags.get(name)
            return tag.read() if tag else None

    def read_sync(self, name: str):
        """Synchronous read — use inside scan cycle routines only."""
        tag = self._tags.get(name)
        return tag.read() if tag else None

    def write_sync(self, name: str, value) -> None:
        """Synchronous write — use inside scan cycle routines only."""
        if name in self._tags:
            self._tags[name].write(value)
            self._tags[name].last_updated = time.time()

    def register(self, tags: dict[str, Tag]) -> None:
        self._tags.update(tags)

    def subscribe(self, tag_name: str, callback: Callable) -> None:
        self._subscribers.setdefault(tag_name, []).append(callback)

    def snapshot(self) -> dict[str, any]:
        """Return current values of all tags — for MQTT/OPC-UA publish."""
        return {name: tag.read() for name, tag in self._tags.items()}

    def get_all(self) -> dict[str, Tag]:
        return dict(self._tags)


# ── Pet Food Production Line Model ────────────────────────────────────────────

class ProductionLineModel:
    """
    Simulates physical behavior of each station.
    Called every scan cycle by the main program routine.
    This is the 'process' that the PLC logic controls.
    """

    def __init__(self, db: TagDatabase) -> None:
        self.db = db
        self._sim_time = 0.0

    def update(self, dt: float) -> None:
        """Advance simulation by dt seconds."""
        self._sim_time += dt
        self._update_mixer(dt)
        self._update_conveyor(dt)
        self._update_filler(dt)
        self._update_sensors(dt)
        self._update_production_counters(dt)

    def _update_mixer(self, dt: float) -> None:
        running = self.db.read_sync("Mixer_Motor.Running")
        if running:
            # Simulate speed ramp-up
            spd = self.db.read_sync("Mixer_Motor.Speed_RPM") or 0.0
            target = 1450.0
            spd = min(target, spd + 200 * dt)
            # Current draw proportional to speed + noise
            current = (spd / target) * 18.5 + random.gauss(0, 0.3)
            # Accumulate run hours
            run_h = (self.db.read_sync("Mixer_Motor.RunHours") or 0.0) + dt / 3600
            self.db.write_sync("Mixer_Motor.Speed_RPM", round(spd, 2))
            self.db.write_sync("Mixer_Motor.Current_A", round(max(0, current), 2))
            self.db.write_sync("Mixer_Motor.RunHours",  round(run_h, 4))
            # Detect overload (>21A threshold)
            overload = current > 21.0
            self.db.write_sync("Mixer_Motor.Overload", overload)
            if overload:
                self.db.write_sync("Mixer_Motor.FaultCode", 1)
                self.db.write_sync("Mixer_Motor.Faulted",   True)
        else:
            # Coast down
            spd = self.db.read_sync("Mixer_Motor.Speed_RPM") or 0.0
            spd = max(0.0, spd - 300 * dt)
            self.db.write_sync("Mixer_Motor.Speed_RPM", round(spd, 2))
            self.db.write_sync("Mixer_Motor.Current_A", 0.0)

    def _update_conveyor(self, dt: float) -> None:
        running = self.db.read_sync("Conveyor_Main.Running")
        if running:
            spd  = self.db.read_sync("Conveyor_Main.Speed_mpm") or 0.0
            spd  = min(12.0, spd + 5 * dt)
            load = 45.0 + 15.0 * math.sin(self._sim_time * 0.1) + random.gauss(0, 2)
            run_h = (self.db.read_sync("Conveyor_Main.RunHours") or 0.0) + dt / 3600
            self.db.write_sync("Conveyor_Main.Speed_mpm", round(spd, 2))
            self.db.write_sync("Conveyor_Main.Load_pct",  round(max(0, load), 1))
            self.db.write_sync("Conveyor_Main.RunHours",  round(run_h, 4))
        else:
            spd = self.db.read_sync("Conveyor_Main.Speed_mpm") or 0.0
            self.db.write_sync("Conveyor_Main.Speed_mpm", round(max(0, spd - 8 * dt), 2))

    def _update_filler(self, dt: float) -> None:
        # Temperature sensor on filler — drifts slowly
        temp = self.db.read_sync("Filler_Temp.EUValue") or 25.0
        target_temp = 72.0  # pasteurisation target
        running = self.db.read_sync("Mixer_Motor.Running")
        if running:
            temp += (target_temp - temp) * 0.05 * dt + random.gauss(0, 0.1)
        else:
            temp += (22.0 - temp) * 0.02 * dt
        self.db.write_sync("Filler_Temp.EUValue", round(temp, 2))
        # Update alarms
        self.db.write_sync("Filler_Temp.HiHi_Alarm", temp >= 85.0)
        self.db.write_sync("Filler_Temp.Hi_Alarm",   temp >= 78.0 and temp < 85.0)
        self.db.write_sync("Filler_Temp.Lo_Alarm",   temp <= 60.0 and temp > 50.0)
        self.db.write_sync("Filler_Temp.LoLo_Alarm", temp <= 50.0)

    def _update_sensors(self, dt: float) -> None:
        # Pressure sensor on mixer outlet
        pres = self.db.read_sync("Mixer_Pressure.EUValue") or 0.0
        running = self.db.read_sync("Mixer_Motor.Running")
        target_pres = 3.2 if running else 0.0
        pres += (target_pres - pres) * 0.3 * dt + random.gauss(0, 0.02)
        self.db.write_sync("Mixer_Pressure.EUValue", round(max(0, pres), 3))
        self.db.write_sync("Mixer_Pressure.HiHi_Alarm", pres >= 4.5)
        self.db.write_sync("Mixer_Pressure.Hi_Alarm",   pres >= 4.0 and pres < 4.5)

    def _update_production_counters(self, dt: float) -> None:
        running = self.db.read_sync("Line.LineRunning")
        if running:
            # Accumulate production at ~500 kg/hr
            kg = (self.db.read_sync("Line.CurrentBatch_kg") or 0.0) + (500 / 3600) * dt
            self.db.write_sync("Line.CurrentBatch_kg", round(kg, 3))


# ── Program Routines (Ladder Logic executed each scan) ────────────────────────

class MainProgram:
    """
    Mirrors ControlLogix Program: MainProgram
    Routines execute in order each scan: Main → Safety → MotorControl → Alarms
    """

    def __init__(self, db: TagDatabase, executor: InstructionExecutor) -> None:
        self.db  = db
        self.ex  = executor

    def scan(self) -> None:
        """Execute all routines in sequence — called every scan cycle."""
        self._routine_safety()
        self._routine_motor_control()
        self._routine_conveyor_control()
        self._routine_interlock()

    def _routine_safety(self) -> None:
        """Safety routine — E-Stop and safety gate logic."""
        e_stop = self.db.read_sync("Line.E_Stop_Active")
        safety = self.db.read_sync("Line.SafetyOK")

        if e_stop or not safety:
            # XIC(E_Stop_Active) → OTE(Mixer_Motor.StopCmd)
            self.ex.XIC("Line.E_Stop_Active")
            self.ex.OTE("Mixer_Motor.StopCmd")
            self.ex.OTE("Conveyor_Main.E_Stop")
            self.db.write_sync("Mixer_Motor.RunCmd",    False)
            self.db.write_sync("Conveyor_Main.Running", False)

    def _routine_motor_control(self) -> None:
        """Motor start/stop with fault interlock."""
        faulted = self.db.read_sync("Mixer_Motor.Faulted")
        run_cmd = self.db.read_sync("Mixer_Motor.RunCmd")
        stop_cmd= self.db.read_sync("Mixer_Motor.StopCmd")
        auto    = self.db.read_sync("Mixer_Motor.AutoMode")
        line_ok = self.db.read_sync("Line.SafetyOK")

        # XIO(Faulted) AND XIO(StopCmd) AND XIC(RunCmd) AND XIC(SafetyOK) → OTE(Running)
        can_run = (not faulted) and (not stop_cmd) and run_cmd and line_ok
        self.db.write_sync("Mixer_Motor.Running", can_run)

        # If fault cleared — reset fault code
        if not faulted:
            self.db.write_sync("Mixer_Motor.FaultCode", 0)
            self.db.write_sync("Mixer_Motor.Overload",  False)

    def _routine_conveyor_control(self) -> None:
        """Conveyor runs only when Mixer is running (interlock)."""
        mixer_running = self.db.read_sync("Mixer_Motor.Running")
        e_stop        = self.db.read_sync("Conveyor_Main.E_Stop")
        jam           = self.db.read_sync("Conveyor_Main.JamDetected")

        can_run = mixer_running and (not e_stop) and (not jam)
        self.db.write_sync("Conveyor_Main.Running", can_run)

    def _routine_interlock(self) -> None:
        """Line-level interlock — all running = line running."""
        mixer_ok = self.db.read_sync("Mixer_Motor.Running")
        conv_ok  = self.db.read_sync("Conveyor_Main.Running")
        self.db.write_sync("Line.LineRunning", mixer_ok and conv_ok)


# ── ControlLogix Runtime Engine ───────────────────────────────────────────────

class ControlLogixRuntime:
    """
    Main PLC runtime — orchestrates all tasks, programs, and the process model.

    Task hierarchy (mirrors real ControlLogix):
      EventTask   (priority 1)  — safety E-stop response
      PeriodicTask(priority 5)  — 10ms control loop
      ContinuousTask(priority 10)— free-running background logic
    """

    def __init__(self) -> None:
        self.db        = TagDatabase()
        self.executor  = InstructionExecutor(self.db)
        self.program   = MainProgram(self.db, self.executor)
        self.model     = ProductionLineModel(self.db)
        self.injector  = FaultInjector(self.db)
        self._running  = False

        # Task registry
        self._tasks: list[PLCTask] = [
            PLCTask("EventTask",      TaskType.EVENT,      period_ms=0,    priority=1),
            PLCTask("PeriodicTask",   TaskType.PERIODIC,   period_ms=10.0, priority=5),
            PLCTask("ContinuousTask", TaskType.CONTINUOUS, period_ms=0,    priority=10),
        ]
        self._tag_publishers: list[Callable] = []

    def add_publisher(self, cb: Callable) -> None:
        """Register a callback that receives tag snapshots each scan."""
        self._tag_publishers.append(cb)

    # ── Tag Initialisation ────────────────────────────────────────────────────

    def _init_tags(self) -> None:
        """Populate controller tag database with all UDTs."""
        mixer    = MotorUDT()
        conveyor = ConveyorUDT()
        filler_t = SensorUDT(EUMin=0, EUMax=100, HiHi_SP=85, Hi_SP=78, Lo_SP=60, LoLo_SP=50)
        mixer_p  = SensorUDT(EUMin=0, EUMax=6,   HiHi_SP=4.5, Hi_SP=4.0, Lo_SP=0.5, LoLo_SP=0.2)
        line     = ProductionLineUDT()

        self.db.register(mixer.to_tags("Mixer_Motor"))
        self.db.register(conveyor.to_tags("Conveyor_Main"))
        self.db.register(filler_t.to_tags("Filler_Temp"))
        self.db.register(mixer_p.to_tags("Mixer_Pressure"))

        # Line-level tags
        self.db.register({
            "Line.LineRunning":           Tag("Line.LineRunning",           TagType.BOOL, value=False),
            "Line.LineAuto":              Tag("Line.LineAuto",               TagType.BOOL, value=True),
            "Line.E_Stop_Active":         Tag("Line.E_Stop_Active",         TagType.BOOL, value=False),
            "Line.SafetyOK":              Tag("Line.SafetyOK",              TagType.BOOL, value=True),
            "Line.BatchCount":            Tag("Line.BatchCount",            TagType.DINT, value=0),
            "Line.RejectCount":           Tag("Line.RejectCount",           TagType.DINT, value=0),
            "Line.GoodCount":             Tag("Line.GoodCount",             TagType.DINT, value=0),
            "Line.TotalProd_kg":          Tag("Line.TotalProd_kg",          TagType.REAL, value=0.0),
            "Line.CurrentBatch_kg":       Tag("Line.CurrentBatch_kg",       TagType.REAL, value=0.0),
            "Line.TargetBatch_kg":        Tag("Line.TargetBatch_kg",        TagType.REAL, value=1000.0),
            "Line.PlannedDowntime_min":   Tag("Line.PlannedDowntime_min",   TagType.REAL, value=0.0),
            "Line.UnplannedDowntime_min": Tag("Line.UnplannedDowntime_min", TagType.REAL, value=0.0),
        })

        logger.info(f"[TagDB] Initialised {len(self.db.get_all())} controller tags")

    # ── Task Execution ────────────────────────────────────────────────────────

    async def _run_periodic_task(self) -> None:
        """10ms periodic task — main control loop."""
        task = self._tasks[1]
        interval = task.period_ms / 1000.0
        task._running = True
        logger.info(f"[Task] PeriodicTask started @ {task.period_ms}ms interval")

        while self._running:
            t0 = time.perf_counter()

            # Execute main program scan
            self.program.scan()

            # Advance physical model
            self.model.update(interval)

            # Inject faults (probabilistic)
            await self.injector.tick(interval)

            # Publish tag snapshot to subscribers
            if self._tag_publishers:
                snapshot = self.db.snapshot()
                for pub in self._tag_publishers:
                    asyncio.create_task(pub(snapshot))

            # Track scan time
            scan_ms = (time.perf_counter() - t0) * 1000
            task.scan_count += 1
            task.last_scan_ms = scan_ms
            task.max_scan_ms  = max(task.max_scan_ms, scan_ms)

            # Watchdog check
            if scan_ms > task.watchdog_ms:
                logger.error(f"[WATCHDOG] PeriodicTask scan time {scan_ms:.1f}ms > {task.watchdog_ms}ms!")

            # Sleep remaining interval
            elapsed = time.perf_counter() - t0
            await asyncio.sleep(max(0, interval - elapsed))

    async def _run_continuous_task(self) -> None:
        """Continuous task — background diagnostics."""
        task = self._tasks[2]
        task._running = True
        logger.info("[Task] ContinuousTask started")

        while self._running:
            task.scan_count += 1
            # Background: accumulate unplanned downtime
            line_running = self.db.read_sync("Line.LineRunning")
            line_auto    = self.db.read_sync("Line.LineAuto")
            if not line_running and line_auto:
                dt_min = self.db.read_sync("Line.UnplannedDowntime_min") or 0.0
                self.db.write_sync("Line.UnplannedDowntime_min", round(dt_min + 0.1/60, 6))
            await asyncio.sleep(0.1)

    async def _run_event_task(self) -> None:
        """Event task — monitors E-Stop tag for immediate response."""
        task = self._tasks[0]
        task._running = True
        logger.info("[Task] EventTask started (monitoring E_Stop)")

        prev_estop = False
        while self._running:
            estop = self.db.read_sync("Line.E_Stop_Active")
            if estop and not prev_estop:
                logger.warning("[EVENT] E-Stop activated — executing safety routine")
                task.scan_count += 1
                self.program._routine_safety()
            prev_estop = estop
            await asyncio.sleep(0.001)   # 1ms polling — highest priority

    # ── Startup Sequence ──────────────────────────────────────────────────────

    async def start(self) -> None:
        """Boot the PLC runtime."""
        logger.info("=" * 60)
        logger.info("  AB ControlLogix Runtime Engine — Booting")
        logger.info("=" * 60)

        self._init_tags()
        self._running = True

        # Simulate operator pressing "Start" after 2s
        asyncio.create_task(self._demo_sequence())

        # Launch all tasks concurrently
        await asyncio.gather(
            self._run_event_task(),
            self._run_periodic_task(),
            self._run_continuous_task(),
        )

    async def stop(self) -> None:
        self._running = False
        logger.info("[Runtime] PLC stopped")

    async def _demo_sequence(self) -> None:
        """Demo: auto-start line, inject a fault, recover."""
        await asyncio.sleep(2)
        logger.info("[Demo] Operator: Line Start")
        self.db.write_sync("Mixer_Motor.RunCmd",  True)
        self.db.write_sync("Mixer_Motor.AutoMode", True)

        await asyncio.sleep(5)
        logger.info("[Demo] Operator: Conveyor start command")

        await asyncio.sleep(10)
        logger.info("[Demo] Fault injector: triggering motor overload in 3s")
        await asyncio.sleep(3)
        await self.injector.inject_motor_overload("Mixer_Motor")

        await asyncio.sleep(5)
        logger.info("[Demo] Operator: Fault reset")
        self.db.write_sync("Mixer_Motor.Faulted",   False)
        self.db.write_sync("Mixer_Motor.FaultCode", 0)
        self.db.write_sync("Mixer_Motor.RunCmd",    True)

    def get_task_stats(self) -> list[dict]:
        return [
            {
                "name":         t.name,
                "type":         t.task_type.name,
                "scan_count":   t.scan_count,
                "last_scan_ms": round(t.last_scan_ms, 3),
                "max_scan_ms":  round(t.max_scan_ms, 3),
            }
            for t in self._tasks
        ]


# ── Entry Point ───────────────────────────────────────────────────────────────

async def _main() -> None:
    from rich.console import Console
    from rich.table import Table
    import signal

    console = Console()
    runtime = ControlLogixRuntime()

    # Console publisher — prints key tags every second
    async def console_publisher(snapshot: dict) -> None:
        pass  # Handled by status loop below

    runtime.add_publisher(console_publisher)

    # Status display loop
    async def status_loop() -> None:
        await asyncio.sleep(3)
        while runtime._running:
            await asyncio.sleep(2)
            snap = runtime.db.snapshot()
            table = Table(title="AB ControlLogix Tag Monitor", show_header=True)
            table.add_column("Tag", style="cyan",  width=32)
            table.add_column("Value", style="green", width=16)
            table.add_column("Status", style="yellow")

            key_tags = [
                ("Mixer_Motor.Running",    "Motor"),
                ("Mixer_Motor.Speed_RPM",  "Motor"),
                ("Mixer_Motor.Current_A",  "Motor"),
                ("Mixer_Motor.Faulted",    "Motor"),
                ("Mixer_Motor.RunHours",   "Motor"),
                ("Conveyor_Main.Running",  "Conveyor"),
                ("Conveyor_Main.Speed_mpm","Conveyor"),
                ("Filler_Temp.EUValue",    "Sensor"),
                ("Filler_Temp.Hi_Alarm",   "Alarm"),
                ("Mixer_Pressure.EUValue", "Sensor"),
                ("Line.LineRunning",       "Line"),
                ("Line.CurrentBatch_kg",   "Production"),
                ("Line.UnplannedDowntime_min", "KPI"),
            ]
            for tag_name, category in key_tags:
                val = snap.get(tag_name, "—")
                if isinstance(val, float):
                    val = f"{val:.2f}"
                elif isinstance(val, bool):
                    val = "TRUE" if val else "false"
                status = "⚠ ALARM" if ("Alarm" in tag_name and snap.get(tag_name)) else \
                         "⚠ FAULT" if ("Faulted" in tag_name and snap.get(tag_name)) else "OK"
                table.add_row(tag_name, str(val), status)

            console.clear()
            console.print(table)

            stats = runtime.get_task_stats()
            for s in stats:
                console.print(
                    f"  [{s['name']}] scans={s['scan_count']:,}  "
                    f"last={s['last_scan_ms']}ms  max={s['max_scan_ms']}ms"
                )

    asyncio.create_task(status_loop())

    try:
        await runtime.start()
    except KeyboardInterrupt:
        await runtime.stop()


if __name__ == "__main__":
    asyncio.run(_main())

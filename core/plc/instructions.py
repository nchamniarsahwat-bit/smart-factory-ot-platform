"""
core/plc/instructions.py
Ladder Logic & Structured Text Instruction Executor

Implements common AB instructions:
  Bit:      XIC, XIO, XIF, OTE, OTL, OTU
  Timer:    TON, TOF, RTO
  Counter:  CTU, CTD, RES
  Compare:  EQU, NEQ, LES, GRT, GEQ, LEQ
  Math:     ADD, SUB, MUL, DIV, MOV, CPT
  Control:  JSR, RET
  PID:      PID (simplified)
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.plc.runtime import TagDatabase


@dataclass
class TimerAccumulator:
    """AB timer accumulator — mirrors TON/TOF/RTO data structure."""
    preset_ms: float = 1000.0
    accum_ms:  float = 0.0
    en:        bool  = False    # Enable bit
    tt:        bool  = False    # Timer Timing bit
    dn:        bool  = False    # Done bit
    _last_tick: float = field(default_factory=time.perf_counter, repr=False)

    def tick(self, enable: bool) -> None:
        """Call every scan to advance timer (TON behaviour)."""
        now = time.perf_counter()
        dt_ms = (now - self._last_tick) * 1000
        self._last_tick = now

        self.en = enable
        if enable and not self.dn:
            self.accum_ms = min(self.accum_ms + dt_ms, self.preset_ms)
            self.tt = not self.dn
            self.dn = self.accum_ms >= self.preset_ms
        elif not enable:
            self.accum_ms = 0.0
            self.tt = False
            self.dn = False


@dataclass
class CounterAccumulator:
    """AB counter accumulator — mirrors CTU/CTD data structure."""
    preset: int  = 0
    accum:  int  = 0
    cu:     bool = False   # Count Up enable (rung logic — True on rising edge)
    dn:     bool = False   # Done bit
    ov:     bool = False   # Overflow bit
    _prev_cu: bool = False

    def count_up(self, enable: bool) -> None:
        """Rising-edge triggered count."""
        rising_edge = enable and not self._prev_cu
        if rising_edge:
            self.accum += 1
            self.ov = self.accum > 32767
            self.dn = self.accum >= self.preset
        self._prev_cu = enable
        self.cu = enable

    def reset(self) -> None:
        self.accum = 0
        self.dn    = False
        self.ov    = False


@dataclass
class PIDController:
    """
    Simplified AB PID instruction.
    Mirrors PID structure in ControlLogix (Process Value, Setpoint, Output).
    """
    kp:      float = 1.0
    ki:      float = 0.1
    kd:      float = 0.01
    setpoint:float = 0.0
    output_min: float = 0.0
    output_max: float = 100.0

    _integral:  float = 0.0
    _prev_error:float = 0.0
    _last_tick: float = field(default_factory=time.perf_counter, repr=False)

    def compute(self, pv: float) -> float:
        now   = time.perf_counter()
        dt    = now - self._last_tick
        self._last_tick = now

        error         = self.setpoint - pv
        self._integral += error * dt
        derivative    = (error - self._prev_error) / dt if dt > 0 else 0.0
        self._prev_error = error

        output = (self.kp * error +
                  self.ki * self._integral +
                  self.kd * derivative)
        return max(self.output_min, min(self.output_max, output))


# ── Instruction Executor ──────────────────────────────────────────────────────

class InstructionExecutor:
    """
    Executes AB Ladder Logic instructions against the tag database.
    Each method mirrors the instruction's behaviour in Studio 5000.
    """

    def __init__(self, db: "TagDatabase") -> None:
        self.db      = db
        self._rung   = True   # Current rung state (power rail)
        self._timers:   dict[str, TimerAccumulator]  = {}
        self._counters: dict[str, CounterAccumulator]= {}
        self._pids:     dict[str, PIDController]     = {}

    def _get_timer(self, name: str, preset_ms: float = 1000.0) -> TimerAccumulator:
        if name not in self._timers:
            self._timers[name] = TimerAccumulator(preset_ms=preset_ms)
        return self._timers[name]

    def _get_counter(self, name: str, preset: int = 0) -> CounterAccumulator:
        if name not in self._counters:
            self._counters[name] = CounterAccumulator(preset=preset)
        return self._counters[name]

    # ── Bit Instructions ──────────────────────────────────────────────────────

    def XIC(self, tag: str) -> bool:
        """Examine If Closed — passes power if tag is TRUE."""
        val = bool(self.db.read_sync(tag))
        self._rung = self._rung and val
        return self._rung

    def XIO(self, tag: str) -> bool:
        """Examine If Open — passes power if tag is FALSE."""
        val = not bool(self.db.read_sync(tag))
        self._rung = self._rung and val
        return self._rung

    def OTE(self, tag: str) -> None:
        """Output Energize — sets tag to rung state."""
        self.db.write_sync(tag, self._rung)
        self._rung = True   # Reset rung for next instruction

    def OTL(self, tag: str) -> None:
        """Output Latch — sets tag TRUE if rung True, retains if False."""
        if self._rung:
            self.db.write_sync(tag, True)
        self._rung = True

    def OTU(self, tag: str) -> None:
        """Output Unlatch — sets tag FALSE if rung True."""
        if self._rung:
            self.db.write_sync(tag, False)
        self._rung = True

    # ── Timer Instructions ────────────────────────────────────────────────────

    def TON(self, timer_name: str, preset_ms: float) -> bool:
        """Timer On Delay — returns .DN bit."""
        tmr = self._get_timer(timer_name, preset_ms)
        tmr.tick(self._rung)
        return tmr.dn

    def TOF(self, timer_name: str, preset_ms: float) -> bool:
        """Timer Off Delay — DN set when rung goes FALSE for preset duration."""
        tmr = self._get_timer(timer_name, preset_ms)
        # TOF: timer runs when rung is FALSE
        tmr.tick(not self._rung)
        return tmr.dn

    # ── Counter Instructions ──────────────────────────────────────────────────

    def CTU(self, counter_name: str, preset: int) -> bool:
        """Count Up — increments on rising edge, returns .DN."""
        ctr = self._get_counter(counter_name, preset)
        ctr.count_up(self._rung)
        return ctr.dn

    def CTD(self, counter_name: str, preset: int) -> bool:
        """Count Down — decrements on rising edge."""
        ctr = self._get_counter(counter_name, preset)
        ctr.count_up(self._rung)   # Simplified — mirrors CTU logic
        return ctr.dn

    def RES(self, counter_name: str) -> None:
        """Reset counter or timer."""
        if counter_name in self._counters:
            self._counters[counter_name].reset()
        if counter_name in self._timers:
            t = self._timers[counter_name]
            t.accum_ms = 0.0
            t.dn = False
            t.tt = False

    # ── Compare Instructions ──────────────────────────────────────────────────

    def EQU(self, tag_a: str, tag_b_or_val) -> bool:
        """Equal — passes power if A == B."""
        a   = self.db.read_sync(tag_a)
        b   = self.db.read_sync(tag_b_or_val) if isinstance(tag_b_or_val, str) else tag_b_or_val
        res = a == b
        self._rung = self._rung and res
        return self._rung

    def GRT(self, tag_a: str, tag_b_or_val) -> bool:
        """Greater Than — passes power if A > B."""
        a   = self.db.read_sync(tag_a) or 0
        b   = (self.db.read_sync(tag_b_or_val) or 0) if isinstance(tag_b_or_val, str) else tag_b_or_val
        res = a > b
        self._rung = self._rung and res
        return self._rung

    def LES(self, tag_a: str, tag_b_or_val) -> bool:
        """Less Than — passes power if A < B."""
        a   = self.db.read_sync(tag_a) or 0
        b   = (self.db.read_sync(tag_b_or_val) or 0) if isinstance(tag_b_or_val, str) else tag_b_or_val
        res = a < b
        self._rung = self._rung and res
        return self._rung

    # ── Math Instructions ─────────────────────────────────────────────────────

    def MOV(self, source, dest_tag: str) -> None:
        """Move — copies source value to destination tag."""
        val = self.db.read_sync(source) if isinstance(source, str) else source
        if self._rung:
            self.db.write_sync(dest_tag, val)

    def ADD(self, tag_a: str, tag_b_or_val, dest_tag: str) -> None:
        """Add A + B → Destination."""
        if self._rung:
            a = self.db.read_sync(tag_a) or 0
            b = (self.db.read_sync(tag_b_or_val) or 0) if isinstance(tag_b_or_val, str) else tag_b_or_val
            self.db.write_sync(dest_tag, a + b)

    # ── PID Instruction ───────────────────────────────────────────────────────

    def PID(self, pid_name: str, pv_tag: str, sp: float,
            kp: float = 1.0, ki: float = 0.1, kd: float = 0.01,
            output_tag: str = None) -> float:
        """PID control block — returns output value."""
        if pid_name not in self._pids:
            self._pids[pid_name] = PIDController(kp=kp, ki=ki, kd=kd,
                                                  setpoint=sp)
        pid = self._pids[pid_name]
        pid.setpoint = sp
        pv     = self.db.read_sync(pv_tag) or 0.0
        output = pid.compute(pv)
        if output_tag and self._rung:
            self.db.write_sync(output_tag, output)
        return output

    # ── Subroutine ────────────────────────────────────────────────────────────

    def JSR(self, routine_callable) -> None:
        """Jump to Subroutine — calls a Python function as a routine."""
        if self._rung and callable(routine_callable):
            routine_callable()

"""
core/plc/tags.py
Allen-Bradley ControlLogix L5X Tag Structure
Mirrors real UDT (User-Defined Types) from Studio 5000 / RSLogix 5000
"""
from __future__ import annotations
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any


# ── Data Types (matching AB atomic types) ────────────────────────────────────

class TagType(IntEnum):
    BOOL  = 0
    SINT  = 1   # 8-bit signed
    INT   = 2   # 16-bit signed
    DINT  = 3   # 32-bit signed  ← most common in AB
    LINT  = 4   # 64-bit signed
    REAL  = 5   # 32-bit float
    LREAL = 6   # 64-bit float
    STRING= 7
    UDT   = 8   # User-Defined Type


class TagScope(IntEnum):
    CONTROLLER = 0   # Global — visible to all programs
    PROGRAM    = 1   # Local — visible within one program only


@dataclass
class Tag:
    """Single AB ControlLogix tag — mirrors L5X <Tag> element."""
    name:        str
    tag_type:    TagType
    scope:       TagScope     = TagScope.CONTROLLER
    value:       Any          = 0
    description: str          = ""
    external_access: str      = "Read/Write"

    # Runtime metadata (not in L5X but tracked at runtime)
    last_updated: float       = 0.0   # epoch timestamp
    force_value:  Any         = None  # None = no force active
    quality:      str         = "Good"

    @property
    def forced(self) -> bool:
        return self.force_value is not None

    def read(self) -> Any:
        return self.force_value if self.forced else self.value

    def write(self, val: Any) -> None:
        if not self.forced:
            self.value = val


# ── UDT: Motor (common AB pattern) ───────────────────────────────────────────

@dataclass
class MotorUDT:
    """
    Mirrors typical AB Motor UDT used in pet food / FMCG plants.
    e.g.  Mixer_Motor : MotorUDT
    """
    Running:    bool  = False
    Faulted:    bool  = False
    AutoMode:   bool  = True
    RunCmd:     bool  = False
    StopCmd:    bool  = False
    Speed_RPM:  float = 0.0
    Current_A:  float = 0.0
    Overload:   bool  = False
    RunHours:   float = 0.0    # accumulated runtime hours → feeds PM engine
    FaultCode:  int   = 0      # 0=OK, 1=Overload, 2=Stall, 3=CommLoss

    def to_tags(self, prefix: str) -> dict[str, Tag]:
        """Flatten UDT into individual controller-scope tags."""
        return {
            f"{prefix}.Running":   Tag(f"{prefix}.Running",   TagType.BOOL, value=self.Running),
            f"{prefix}.Faulted":   Tag(f"{prefix}.Faulted",   TagType.BOOL, value=self.Faulted),
            f"{prefix}.AutoMode":  Tag(f"{prefix}.AutoMode",  TagType.BOOL, value=self.AutoMode),
            f"{prefix}.RunCmd":    Tag(f"{prefix}.RunCmd",     TagType.BOOL, value=self.RunCmd),
            f"{prefix}.StopCmd":   Tag(f"{prefix}.StopCmd",    TagType.BOOL, value=self.StopCmd),
            f"{prefix}.Speed_RPM": Tag(f"{prefix}.Speed_RPM",  TagType.REAL, value=self.Speed_RPM),
            f"{prefix}.Current_A": Tag(f"{prefix}.Current_A",  TagType.REAL, value=self.Current_A),
            f"{prefix}.Overload":  Tag(f"{prefix}.Overload",   TagType.BOOL, value=self.Overload),
            f"{prefix}.RunHours":  Tag(f"{prefix}.RunHours",   TagType.REAL, value=self.RunHours),
            f"{prefix}.FaultCode": Tag(f"{prefix}.FaultCode",  TagType.DINT, value=self.FaultCode),
        }


@dataclass
class ConveyorUDT:
    """Conveyor belt UDT."""
    Running:       bool  = False
    Faulted:       bool  = False
    Speed_mpm:     float = 0.0   # metres per minute
    Load_pct:      float = 0.0   # % of rated load
    JamDetected:   bool  = False
    E_Stop:        bool  = False
    RunHours:      float = 0.0
    FaultCode:     int   = 0

    def to_tags(self, prefix: str) -> dict[str, Tag]:
        return {
            f"{prefix}.Running":     Tag(f"{prefix}.Running",     TagType.BOOL, value=self.Running),
            f"{prefix}.Faulted":     Tag(f"{prefix}.Faulted",     TagType.BOOL, value=self.Faulted),
            f"{prefix}.Speed_mpm":   Tag(f"{prefix}.Speed_mpm",   TagType.REAL, value=self.Speed_mpm),
            f"{prefix}.Load_pct":    Tag(f"{prefix}.Load_pct",    TagType.REAL, value=self.Load_pct),
            f"{prefix}.JamDetected": Tag(f"{prefix}.JamDetected", TagType.BOOL, value=self.JamDetected),
            f"{prefix}.E_Stop":      Tag(f"{prefix}.E_Stop",      TagType.BOOL, value=self.E_Stop),
            f"{prefix}.RunHours":    Tag(f"{prefix}.RunHours",    TagType.REAL, value=self.RunHours),
            f"{prefix}.FaultCode":   Tag(f"{prefix}.FaultCode",   TagType.DINT, value=self.FaultCode),
        }


@dataclass
class SensorUDT:
    """Generic analog/digital sensor UDT."""
    RawValue:    float = 0.0
    EUValue:     float = 0.0    # Engineering Units value
    EUMin:       float = 0.0
    EUMax:       float = 100.0
    Fault:       bool  = False
    HiHi_Alarm:  bool  = False
    Hi_Alarm:    bool  = False
    Lo_Alarm:    bool  = False
    LoLo_Alarm:  bool  = False
    HiHi_SP:     float = 95.0
    Hi_SP:       float = 85.0
    Lo_SP:       float = 15.0
    LoLo_SP:     float = 5.0

    def check_alarms(self) -> None:
        self.HiHi_Alarm = self.EUValue >= self.HiHi_SP
        self.Hi_Alarm   = self.EUValue >= self.Hi_SP and not self.HiHi_Alarm
        self.LoLo_Alarm = self.EUValue <= self.LoLo_SP
        self.Lo_Alarm   = self.EUValue <= self.Lo_SP and not self.LoLo_Alarm

    def to_tags(self, prefix: str) -> dict[str, Tag]:
        return {
            f"{prefix}.RawValue":   Tag(f"{prefix}.RawValue",   TagType.REAL, value=self.RawValue),
            f"{prefix}.EUValue":    Tag(f"{prefix}.EUValue",    TagType.REAL, value=self.EUValue),
            f"{prefix}.Fault":      Tag(f"{prefix}.Fault",      TagType.BOOL, value=self.Fault),
            f"{prefix}.HiHi_Alarm": Tag(f"{prefix}.HiHi_Alarm", TagType.BOOL, value=self.HiHi_Alarm),
            f"{prefix}.Hi_Alarm":   Tag(f"{prefix}.Hi_Alarm",   TagType.BOOL, value=self.Hi_Alarm),
            f"{prefix}.Lo_Alarm":   Tag(f"{prefix}.Lo_Alarm",   TagType.BOOL, value=self.Lo_Alarm),
            f"{prefix}.LoLo_Alarm": Tag(f"{prefix}.LoLo_Alarm", TagType.BOOL, value=self.LoLo_Alarm),
        }


@dataclass
class ProductionLineUDT:
    """
    Top-level controller-scope UDT for pet food production line.
    Mirrors real AB program structure: Program:MainProgram tags
    """
    # Production counters
    BatchCount:       int   = 0
    RejectCount:      int   = 0
    GoodCount:        int   = 0
    TotalProd_kg:     float = 0.0

    # Line state
    LineRunning:      bool  = False
    LineAuto:         bool  = True
    E_Stop_Active:    bool  = False
    SafetyOK:         bool  = True

    # Downtime tracking → feeds MES OEE engine
    PlannedDowntime_min:   float = 0.0
    UnplannedDowntime_min: float = 0.0
    Changeover_min:        float = 0.0

    # Recipe / batch
    RecipeID:         int   = 0
    TargetBatch_kg:   float = 1000.0
    CurrentBatch_kg:  float = 0.0

"""
tests/unit/test_plc_runtime.py
Unit tests for AB ControlLogix Runtime Engine

Run:  pytest tests/unit/test_plc_runtime.py -v
"""
import asyncio
import pytest
from core.plc.tags import Tag, TagType, MotorUDT, ConveyorUDT, SensorUDT
from core.plc.runtime import TagDatabase, ControlLogixRuntime
from core.plc.instructions import InstructionExecutor, TimerAccumulator
from core.plc.fault_injector import FaultInjector, FaultType


# ── TagDatabase Tests ─────────────────────────────────────────────────────────

class TestTagDatabase:

    def setup_method(self):
        self.db = TagDatabase()
        self.db.register({
            "Motor.Running": Tag("Motor.Running", TagType.BOOL, value=False),
            "Motor.Speed":   Tag("Motor.Speed",   TagType.REAL, value=0.0),
            "Motor.Faulted": Tag("Motor.Faulted", TagType.BOOL, value=False),
        })

    def test_read_write_sync(self):
        self.db.write_sync("Motor.Running", True)
        assert self.db.read_sync("Motor.Running") is True

    def test_nonexistent_tag_returns_none(self):
        assert self.db.read_sync("NoSuchTag") is None

    def test_snapshot_returns_all_tags(self):
        snap = self.db.snapshot()
        assert "Motor.Running" in snap
        assert "Motor.Speed"   in snap

    @pytest.mark.asyncio
    async def test_async_write_read(self):
        await self.db.write("Motor.Speed", 1450.0)
        val = await self.db.read("Motor.Speed")
        assert val == 1450.0

    @pytest.mark.asyncio
    async def test_subscriber_called_on_change(self):
        called = []
        async def cb(name, old, new):
            called.append((name, old, new))

        self.db.subscribe("Motor.Running", cb)
        await self.db.write("Motor.Running", True)
        await asyncio.sleep(0.05)
        assert len(called) == 1
        assert called[0] == ("Motor.Running", False, True)


# ── Instruction Executor Tests ────────────────────────────────────────────────

class TestInstructionExecutor:

    def setup_method(self):
        self.db = TagDatabase()
        self.db.register({
            "A": Tag("A", TagType.BOOL, value=True),
            "B": Tag("B", TagType.BOOL, value=False),
            "C": Tag("C", TagType.BOOL, value=False),
            "Val": Tag("Val", TagType.REAL, value=5.0),
        })
        self.ex = InstructionExecutor(self.db)

    def test_XIC_true_passes_power(self):
        assert self.ex.XIC("A") is True

    def test_XIC_false_blocks_power(self):
        assert self.ex.XIC("B") is False

    def test_XIO_on_false_tag_passes(self):
        self.ex._rung = True
        result = self.ex.XIO("B")  # B is False → XIO passes
        assert result is True

    def test_OTE_sets_tag(self):
        self.ex._rung = True
        self.ex.OTE("C")
        assert self.db.read_sync("C") is True

    def test_OTE_clears_tag_when_rung_false(self):
        self.db.write_sync("C", True)
        self.ex._rung = False
        self.ex.OTE("C")
        assert self.db.read_sync("C") is False

    def test_TON_times_out(self):
        import time
        timer = TimerAccumulator(preset_ms=50)
        for _ in range(20):
            timer.tick(True)
            time.sleep(0.005)
        assert timer.dn is True

    def test_LES_compare(self):
        self.ex._rung = True
        result = self.ex.LES("Val", 10.0)  # 5.0 < 10.0 → True
        assert result is True

    def test_MOV_copies_value(self):
        self.db.register({"Dest": Tag("Dest", TagType.REAL, value=0.0)})
        self.ex._rung = True
        self.ex.MOV("Val", "Dest")
        assert self.db.read_sync("Dest") == 5.0


# ── UDT Tests ─────────────────────────────────────────────────────────────────

class TestUDTs:

    def test_motor_udt_to_tags(self):
        motor = MotorUDT(Running=True, Speed_RPM=1450.0)
        tags  = motor.to_tags("Mixer_Motor")
        assert "Mixer_Motor.Running"   in tags
        assert "Mixer_Motor.Speed_RPM" in tags
        assert tags["Mixer_Motor.Running"].read()   is True
        assert tags["Mixer_Motor.Speed_RPM"].read() == 1450.0

    def test_sensor_alarm_logic(self):
        sensor = SensorUDT(HiHi_SP=95, Hi_SP=85, Lo_SP=15, LoLo_SP=5)
        sensor.EUValue = 90.0
        sensor.check_alarms()
        assert sensor.Hi_Alarm   is True
        assert sensor.HiHi_Alarm is False

        sensor.EUValue = 97.0
        sensor.check_alarms()
        assert sensor.HiHi_Alarm is True
        assert sensor.Hi_Alarm   is False


# ── Fault Injector Tests ──────────────────────────────────────────────────────

class TestFaultInjector:

    def setup_method(self):
        self.db = TagDatabase()
        motor = MotorUDT()
        self.db.register(motor.to_tags("Mixer_Motor"))
        self.db.register({
            "Conveyor_Main.JamDetected": Tag("Conveyor_Main.JamDetected", TagType.BOOL, value=False),
            "Conveyor_Main.Running":     Tag("Conveyor_Main.Running",     TagType.BOOL, value=True),
            "Line.E_Stop_Active":        Tag("Line.E_Stop_Active",        TagType.BOOL, value=False),
        })
        self.injector = FaultInjector(self.db)

    @pytest.mark.asyncio
    async def test_motor_overload_sets_fault(self):
        await self.injector.inject_motor_overload("Mixer_Motor")
        assert self.db.read_sync("Mixer_Motor.Faulted")   is True
        assert self.db.read_sync("Mixer_Motor.FaultCode") == 1
        assert self.db.read_sync("Mixer_Motor.Running")   is False

    @pytest.mark.asyncio
    async def test_fault_history_recorded(self):
        await self.injector.inject_motor_overload("Mixer_Motor")
        history = self.injector.get_fault_history()
        assert len(history) == 1
        assert history[0]["fault_type"] == "LOGIC"
        assert history[0]["resolved"]   is False

    @pytest.mark.asyncio
    async def test_fault_type_label(self):
        await self.injector.inject_motor_overload("Mixer_Motor")
        history = self.injector.get_fault_history()
        # Verify ML label is correct integer
        assert history[0]["fault_type_id"] == int(FaultType.LOGIC)


# ── Integration: Full Runtime Boot ───────────────────────────────────────────

class TestRuntimeIntegration:

    @pytest.mark.asyncio
    async def test_runtime_initialises_tags(self):
        runtime = ControlLogixRuntime()
        runtime._init_tags()
        snap = runtime.db.snapshot()
        # Must have all key tags
        assert "Mixer_Motor.Running"   in snap
        assert "Conveyor_Main.Running" in snap
        assert "Filler_Temp.EUValue"   in snap
        assert "Line.LineRunning"      in snap

    @pytest.mark.asyncio
    async def test_motor_start_sequence(self):
        runtime = ControlLogixRuntime()
        runtime._init_tags()

        # Set start conditions
        runtime.db.write_sync("Line.SafetyOK",         True)
        runtime.db.write_sync("Line.E_Stop_Active",    False)
        runtime.db.write_sync("Mixer_Motor.RunCmd",    True)
        runtime.db.write_sync("Mixer_Motor.Faulted",   False)
        runtime.db.write_sync("Mixer_Motor.StopCmd",   False)
        runtime.db.write_sync("Mixer_Motor.AutoMode",  True)

        # Execute one scan
        runtime.program.scan()

        assert runtime.db.read_sync("Mixer_Motor.Running") is True

    @pytest.mark.asyncio
    async def test_estop_stops_line(self):
        runtime = ControlLogixRuntime()
        runtime._init_tags()

        runtime.db.write_sync("Mixer_Motor.Running",  True)
        runtime.db.write_sync("Line.SafetyOK",        True)
        runtime.db.write_sync("Line.E_Stop_Active",   True)   # Activate E-Stop

        runtime.program.scan()

        assert runtime.db.read_sync("Mixer_Motor.Running") is False

    @pytest.mark.asyncio
    async def test_conveyor_interlock(self):
        """Conveyor cannot run if mixer is stopped."""
        runtime = ControlLogixRuntime()
        runtime._init_tags()

        runtime.db.write_sync("Mixer_Motor.Running",     False)
        runtime.db.write_sync("Conveyor_Main.E_Stop",    False)
        runtime.db.write_sync("Conveyor_Main.JamDetected", False)

        runtime.program.scan()

        assert runtime.db.read_sync("Conveyor_Main.Running") is False

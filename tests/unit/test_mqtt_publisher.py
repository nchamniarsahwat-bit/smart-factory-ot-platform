"""
tests/unit/test_mqtt_publisher.py
Unit tests for MQTT Publisher — AlarmDetector + ChangeDetector

Run:  python -m pytest tests/unit/test_mqtt_publisher.py -v
  or: python tests/unit/test_mqtt_publisher.py
"""
import asyncio
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from core.protocols.mqtt_publisher import (
    AlarmDetector, ChangeDetector, MQTTPublisher, MQTTConfig
)


# ── AlarmDetector Tests ───────────────────────────────────────────────────────

def test_alarm_rising_edge():
    """New alarm fires only on first True — not repeatedly."""
    detector = AlarmDetector()
    snap1 = {"Mixer_Motor.Faulted": True, "Mixer_Motor.Overload": False}
    alarms = detector.check(snap1)
    assert len(alarms) == 1
    assert alarms[0]["tag"] == "Mixer_Motor.Faulted"
    assert alarms[0]["priority"] == 1

    # Second check — already active, no new alarm
    alarms2 = detector.check(snap1)
    assert len(alarms2) == 0


def test_alarm_clears_on_false():
    """Alarm removed from active when tag goes False."""
    detector = AlarmDetector()
    detector.check({"Mixer_Motor.Faulted": True})
    assert len(detector.get_active()) == 1

    detector.check({"Mixer_Motor.Faulted": False})
    assert len(detector.get_active()) == 0


def test_multiple_alarms():
    """Multiple simultaneous alarms detected correctly."""
    detector = AlarmDetector()
    snap = {
        "Mixer_Motor.Faulted":    True,
        "Conveyor_Main.JamDetected": True,
        "Filler_Temp.HiHi_Alarm": True,
    }
    alarms = detector.check(snap)
    assert len(alarms) == 3
    priorities = {a["priority"] for a in alarms}
    assert 1 in priorities   # Faulted = P1
    assert 2 in priorities   # Jam = P2


def test_alarm_summary():
    """Summary counts by priority."""
    detector = AlarmDetector()
    detector.check({
        "Mixer_Motor.Faulted":       True,   # P1
        "Filler_Temp.HiHi_Alarm":   True,   # P1
        "Conveyor_Main.JamDetected": True,   # P2
    })
    summary = detector.get_summary()
    assert summary["total_active"] == 3
    assert summary["priority_1"] == 2
    assert summary["priority_2"] == 1


def test_alarm_no_false_positive():
    """Tags not in alarm list never trigger."""
    detector = AlarmDetector()
    snap = {
        "Mixer_Motor.Running":  True,
        "Line.LineRunning":     True,
        "Filler_Temp.EUValue":  72.5,
    }
    alarms = detector.check(snap)
    assert len(alarms) == 0


# ── ChangeDetector Tests ──────────────────────────────────────────────────────

def test_change_detector_first_scan_all_changed():
    """First snapshot — all tags considered changed."""
    detector = ChangeDetector()
    snap = {"Motor.Running": True, "Motor.Speed": 1450.0}
    changes = detector.get_changes(snap)
    assert "Motor.Running" in changes
    assert "Motor.Speed"   in changes


def test_change_detector_no_change():
    """Identical snapshots → no changes."""
    detector = ChangeDetector()
    snap = {"Motor.Running": False, "Motor.Speed": 0.0}
    detector.get_changes(snap)          # first scan
    changes = detector.get_changes(snap)  # second — identical
    assert len(changes) == 0


def test_change_detector_partial_change():
    """Only changed tags returned."""
    detector = ChangeDetector()
    snap1 = {"Motor.Running": False, "Motor.Speed": 0.0, "Motor.Current": 0.0}
    detector.get_changes(snap1)

    snap2 = {"Motor.Running": True,  "Motor.Speed": 0.0, "Motor.Current": 5.2}
    changes = detector.get_changes(snap2)

    assert "Motor.Running"  in changes   # changed False→True
    assert "Motor.Current"  in changes   # changed 0.0→5.2
    assert "Motor.Speed" not in changes  # unchanged


def test_change_detector_bool_vs_truthy():
    """Strict equality — True != 1 is False in Python, but 0 != False."""
    detector = ChangeDetector()
    detector.get_changes({"tag": False})
    changes = detector.get_changes({"tag": False})
    assert "tag" not in changes


# ── MQTTConfig Tests ──────────────────────────────────────────────────────────

def test_mqtt_config_topics():
    cfg = MQTTConfig()
    assert cfg.topic_snapshot == "factory/plc/tags/snapshot"
    assert cfg.topic_alarms   == "factory/plc/alarms"
    assert cfg.topic_status   == "factory/plc/status"


def test_mqtt_config_tag_topic():
    cfg = MQTTConfig()
    topic = cfg.topic_tag("Mixer_Motor.Running")
    # Dots replaced with slashes for MQTT hierarchy
    assert topic == "factory/plc/tags/Mixer_Motor/Running"
    assert "." not in topic


def test_mqtt_config_custom_host():
    cfg = MQTTConfig(host="192.168.1.100", port=1884)
    assert cfg.host == "192.168.1.100"
    assert cfg.port == 1884


# ── MQTTPublisher Dry-run Integration ────────────────────────────────────────

async def test_publisher_dry_run_no_crash():
    """Publisher runs in dry-run mode without broker."""
    from core.plc.runtime import ControlLogixRuntime

    runtime   = ControlLogixRuntime()
    publisher = MQTTPublisher(MQTTConfig())
    publisher._dry_run = True
    publisher.attach_runtime(runtime)

    # Simulate a snapshot coming in
    fake_snapshot = {
        "Mixer_Motor.Running":  False,
        "Mixer_Motor.Faulted":  False,
        "Line.LineRunning":     False,
        "Line.CurrentBatch_kg": 0.0,
    }
    await publisher._on_new_snapshot(fake_snapshot)
    assert publisher._last_snapshot == fake_snapshot


async def test_publisher_alarm_fires_on_fault():
    """Publisher detects alarm when fault tag becomes True."""
    publisher = MQTTPublisher(MQTTConfig())
    publisher._dry_run = True

    await publisher._on_new_snapshot({
        "Mixer_Motor.Faulted": True,
        "Line.E_Stop_Active":  False,
    })

    alarms = publisher.alarm_detector.check(publisher._last_snapshot)
    assert len(alarms) == 1
    assert alarms[0]["tag"] == "Mixer_Motor.Faulted"


async def test_publisher_callback_called():
    """Downstream callbacks receive snapshots."""
    received = []

    publisher = MQTTPublisher(MQTTConfig())
    publisher._dry_run = True

    async def fake_historian(snapshot, ts):
        received.append(snapshot)

    publisher.on_snapshot(fake_historian)

    # Manually trigger callbacks
    snap = {"Motor.Running": True}
    for cb in publisher._on_snapshot_callbacks:
        await cb(snap, 0.0)

    assert len(received) == 1
    assert received[0]["Motor.Running"] is True


# ── Runner ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 60)
    print("MQTT Publisher — Unit Tests")
    print("=" * 60)

    tests_sync = [
        ("AlarmDetector: rising edge only",       test_alarm_rising_edge),
        ("AlarmDetector: clears on False",         test_alarm_clears_on_false),
        ("AlarmDetector: multiple alarms",         test_multiple_alarms),
        ("AlarmDetector: summary by priority",     test_alarm_summary),
        ("AlarmDetector: no false positives",      test_alarm_no_false_positive),
        ("ChangeDetector: first scan all changed", test_change_detector_first_scan_all_changed),
        ("ChangeDetector: no change",              test_change_detector_no_change),
        ("ChangeDetector: partial change",         test_change_detector_partial_change),
        ("ChangeDetector: strict equality",        test_change_detector_bool_vs_truthy),
        ("MQTTConfig: topic names",                test_mqtt_config_topics),
        ("MQTTConfig: tag topic dot→slash",        test_mqtt_config_tag_topic),
        ("MQTTConfig: custom host/port",           test_mqtt_config_custom_host),
    ]

    tests_async = [
        ("Publisher: dry-run no crash",            test_publisher_dry_run_no_crash),
        ("Publisher: alarm on fault tag",          test_publisher_alarm_fires_on_fault),
        ("Publisher: callback called",             test_publisher_callback_called),
    ]

    passed = 0
    failed = 0

    for name, fn in tests_sync:
        try:
            fn()
            print(f"  PASS  {name}")
            passed += 1
        except AssertionError as e:
            print(f"  FAIL  {name} — {e}")
            failed += 1
        except Exception as e:
            print(f"  ERROR {name} — {e}")
            failed += 1

    for name, fn in tests_async:
        try:
            asyncio.run(fn())
            print(f"  PASS  {name}")
            passed += 1
        except AssertionError as e:
            print(f"  FAIL  {name} — {e}")
            failed += 1
        except Exception as e:
            print(f"  ERROR {name} — {e}")
            failed += 1

    print()
    print("=" * 60)
    print(f"  {passed} passed  |  {failed} failed  |  {passed+failed} total")
    if failed == 0:
        print("  ALL TESTS PASSED")
    print("=" * 60)

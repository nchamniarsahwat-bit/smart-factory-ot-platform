"""
tests/unit/test_opcua_server.py
Unit tests for OPC-UA Server + Network Health Monitor

Run:  python tests/unit/test_opcua_server.py
"""
import asyncio
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from core.protocols.opcua_server import (
    OPCUAServer, NetworkHealthMonitor,
    PURDUE_ZONES, _ab_type_to_ua_variant
)
from core.plc.runtime import ControlLogixRuntime


# ── OPCUAServer Tests ─────────────────────────────────────────────────────────

async def test_server_starts_dry_run():
    """Server initialises in dry-run without asyncua installed."""
    runtime = ControlLogixRuntime()
    runtime._init_tags()

    server = OPCUAServer()
    server._dry_run = True
    await server.start(runtime)

    assert server._running is True
    stats = server.get_stats()
    assert stats["running"]  is True
    assert stats["dry_run"]  is True


async def test_address_space_has_all_tags():
    """All 44 controller tags registered in stub node store."""
    runtime = ControlLogixRuntime()
    runtime._init_tags()

    server = OPCUAServer()
    server._dry_run = True
    await server.start(runtime)

    stats = server.get_stats()
    assert stats["node_count"] == 44


async def test_tag_updates_propagate():
    """Runtime tag change propagates to OPC-UA stub node."""
    runtime = ControlLogixRuntime()
    runtime._init_tags()

    server = OPCUAServer()
    server._dry_run = True
    server.attach_runtime(runtime)

    # Simulate a snapshot update
    fake_snapshot = {
        "Mixer_Motor.Running":  True,
        "Mixer_Motor.Speed_RPM": 1450.0,
        "Line.LineRunning":      True,
    }
    await server._on_tag_update(fake_snapshot)

    assert server._stub_values.get("Mixer_Motor.Running")   is True
    assert server._stub_values.get("Mixer_Motor.Speed_RPM") == 1450.0
    assert server._update_count == 1


async def test_update_count_increments():
    """Update counter increments with each snapshot."""
    server = OPCUAServer()
    server._dry_run = True

    for i in range(5):
        await server._on_tag_update({"tag": i})

    assert server._update_count == 5


def test_writable_tags_defined():
    """Writable tags set is non-empty and contains expected tags."""
    server = OPCUAServer()
    assert "Mixer_Motor.RunCmd"  in server.WRITABLE_TAGS
    assert "Line.E_Stop_Active"  in server.WRITABLE_TAGS
    assert "Line.LineAuto"       in server.WRITABLE_TAGS
    # Read-only tags must NOT be writable
    assert "Mixer_Motor.Running"   not in server.WRITABLE_TAGS
    assert "Mixer_Motor.Current_A" not in server.WRITABLE_TAGS
    assert "Line.LineRunning"      not in server.WRITABLE_TAGS


def test_endpoint_format():
    """OPC-UA endpoint follows opc.tcp:// format."""
    server = OPCUAServer()
    assert server.ENDPOINT.startswith("opc.tcp://")
    assert "4840" in server.ENDPOINT


def test_stats_structure():
    """get_stats() returns expected keys."""
    server = OPCUAServer()
    stats  = server.get_stats()
    for key in ["running", "dry_run", "endpoint", "node_count",
                "update_count", "writable_tags"]:
        assert key in stats, f"Missing key: {key}"


# ── Purdue Zone Tests ─────────────────────────────────────────────────────────

def test_purdue_zones_defined():
    """Three Purdue levels defined."""
    assert "Level0_Field"      in PURDUE_ZONES
    assert "Level1_Control"    in PURDUE_ZONES
    assert "Level2_Supervisory"in PURDUE_ZONES


def test_purdue_zones_have_tags():
    """Each zone has at least one tag."""
    for zone_name, zone_data in PURDUE_ZONES.items():
        assert len(zone_data["tags"]) > 0, f"{zone_name} has no tags"
        assert "description" in zone_data


def test_field_devices_in_level0():
    """Motor and sensor tags belong to Level0_Field."""
    tags = PURDUE_ZONES["Level0_Field"]["tags"]
    assert "Mixer_Motor.Running"  in tags
    assert "Filler_Temp.EUValue"  in tags
    assert "Mixer_Pressure.EUValue" in tags


def test_control_tags_in_level1():
    """Command tags in Level1_Control."""
    tags = PURDUE_ZONES["Level1_Control"]["tags"]
    assert "Mixer_Motor.RunCmd"   in tags
    assert "Line.E_Stop_Active"   in tags


def test_kpi_tags_in_level2():
    """Production KPIs in Level2_Supervisory."""
    tags = PURDUE_ZONES["Level2_Supervisory"]["tags"]
    assert "Line.BatchCount"        in tags
    assert "Line.CurrentBatch_kg"   in tags
    assert "Line.UnplannedDowntime_min" in tags


# ── NetworkHealthMonitor Tests ────────────────────────────────────────────────

async def test_network_monitor_checks_all_devices():
    """check_all() returns result for every device."""
    monitor = NetworkHealthMonitor()
    results = await monitor.check_all()
    assert len(results) == len(monitor.DEVICES)


async def test_network_monitor_result_structure():
    """Each result has required fields."""
    monitor = NetworkHealthMonitor()
    results = await monitor.check_all()
    required = {"name", "ip", "zone", "type", "online",
                "latency_ms", "packet_loss", "timestamp"}
    for r in results:
        missing = required - set(r.keys())
        assert not missing, f"Missing fields: {missing}"


async def test_network_summary_structure():
    """get_summary() returns expected keys."""
    monitor = NetworkHealthMonitor()
    await monitor.check_all()
    summary = monitor.get_summary()
    for key in ["total_devices", "online", "offline",
                "avg_latency_ms", "zones"]:
        assert key in summary


async def test_network_topology_count():
    """get_topology() returns all devices."""
    monitor = NetworkHealthMonitor()
    topology = monitor.get_topology()
    assert len(topology) == len(monitor.DEVICES)


async def test_network_all_zones_covered():
    """All Purdue zones represented in device list."""
    monitor = NetworkHealthMonitor()
    zones = {d["zone"] for d in monitor.DEVICES}
    assert "Level1_Control"     in zones
    assert "Level2_Supervisory" in zones
    assert "Level3_Operations"  in zones
    assert "DMZ"                in zones


async def test_network_summary_counts_correct():
    """Online + offline = total devices."""
    monitor = NetworkHealthMonitor()
    await monitor.check_all()
    summary = monitor.get_summary()
    assert (summary["online"] + summary["offline"]) == summary["total_devices"]


async def test_network_device_types():
    """Device types include PLC, HMI, Switch, Server, Firewall."""
    monitor  = NetworkHealthMonitor()
    types    = {d["type"] for d in monitor.DEVICES}
    expected = {"PLC", "HMI", "Switch", "Server", "Firewall"}
    assert expected.issubset(types)


async def test_network_history_recorded():
    """check_all() appends to history."""
    monitor = NetworkHealthMonitor()
    await monitor.check_all()
    await monitor.check_all()
    assert len(monitor._history) == 2


# ── Runner ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 60)
    print("M2 OPC-UA Server + Network Monitor — Unit Tests")
    print("=" * 60)

    sync_tests = [
        ("OPCUAServer: writable tags defined",    test_writable_tags_defined),
        ("OPCUAServer: endpoint format",          test_endpoint_format),
        ("OPCUAServer: stats structure",          test_stats_structure),
        ("Purdue zones: 3 levels defined",        test_purdue_zones_defined),
        ("Purdue zones: each has tags",           test_purdue_zones_have_tags),
        ("Purdue zones: field devices in L0",     test_field_devices_in_level0),
        ("Purdue zones: control tags in L1",      test_control_tags_in_level1),
        ("Purdue zones: KPI tags in L2",          test_kpi_tags_in_level2),
    ]

    async_tests = [
        ("OPCUAServer: starts in dry-run",        test_server_starts_dry_run),
        ("OPCUAServer: all 44 tags in addr space",test_address_space_has_all_tags),
        ("OPCUAServer: tag updates propagate",    test_tag_updates_propagate),
        ("OPCUAServer: update count increments",  test_update_count_increments),
        ("NetworkMonitor: checks all devices",    test_network_monitor_checks_all_devices),
        ("NetworkMonitor: result structure",      test_network_monitor_result_structure),
        ("NetworkMonitor: summary structure",     test_network_summary_structure),
        ("NetworkMonitor: topology count",        test_network_topology_count),
        ("NetworkMonitor: all zones covered",     test_network_all_zones_covered),
        ("NetworkMonitor: online+offline=total",  test_network_summary_counts_correct),
        ("NetworkMonitor: device types complete", test_network_device_types),
        ("NetworkMonitor: history recorded",      test_network_history_recorded),
    ]

    passed = failed = 0

    for name, fn in sync_tests:
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

    for name, fn in async_tests:
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

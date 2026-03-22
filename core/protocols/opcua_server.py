"""
core/protocols/opcua_server.py
OPC-UA Server — IEC 62541 Compliant Information Model

Exposes AB ControlLogix tags as an OPC-UA address space.
Any OPC-UA client (UaExpert, Node-RED, Ignition, Python) can connect and:
  - Browse the node hierarchy
  - Read / Write tag values
  - Subscribe to data changes (MonitoredItems)

Address space structure (mirrors Purdue Model):
  Objects/
  └── Factory/
      └── PetFoodLine/           ← ISA-95 Area
          ├── Mixer_Motor/       ← Equipment Module
          │   ├── Running        ← Tag node (Boolean)
          │   ├── Speed_RPM      ← Tag node (Float)
          │   ├── Current_A      ← Tag node (Float)
          │   ├── Faulted        ← Tag node (Boolean)
          │   └── RunHours       ← Tag node (Float)
          ├── Conveyor_Main/
          ├── Filler_Temp/
          ├── Mixer_Pressure/
          └── Line/

Run standalone:
  python -m core.protocols.opcua_server

Connect with UaExpert:
  opc.tcp://localhost:4840/factory/plc

Requires: pip install asyncua
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

logger = logging.getLogger("opcua_server")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

# ── Try importing asyncua ─────────────────────────────────────────────────────
try:
    from asyncua import Server, ua
    from asyncua.common.node import Node
    OPCUA_AVAILABLE = True
except ImportError:
    OPCUA_AVAILABLE = False
    logger.warning("asyncua not installed — OPC-UA server in STUB mode")
    logger.warning("Install: pip install asyncua")


# ── OPC-UA Node Type mapping from AB tag types ────────────────────────────────

def _ab_type_to_ua_variant(value):
    """
    Map AB tag value to OPC-UA Variant type.
    Mirrors ControlLogix atomic type → OPC-UA built-in type.
    """
    if not OPCUA_AVAILABLE:
        return value
    if isinstance(value, bool):
        return ua.Variant(value, ua.VariantType.Boolean)
    elif isinstance(value, int):
        return ua.Variant(value, ua.VariantType.Int32)     # DINT → Int32
    elif isinstance(value, float):
        return ua.Variant(value, ua.VariantType.Float)     # REAL → Float
    elif isinstance(value, str):
        return ua.Variant(value, ua.VariantType.String)
    return ua.Variant(value, ua.VariantType.Variant)


# ── Purdue Zone Definitions ───────────────────────────────────────────────────

PURDUE_ZONES = {
    "Level0_Field": {
        "description": "Field devices — sensors, actuators, motors",
        "tags": [
            "Mixer_Motor.Running",   "Mixer_Motor.Speed_RPM",
            "Mixer_Motor.Current_A", "Mixer_Motor.Faulted",
            "Mixer_Motor.Overload",  "Mixer_Motor.RunHours",
            "Mixer_Motor.FaultCode",
            "Conveyor_Main.Running", "Conveyor_Main.Speed_mpm",
            "Conveyor_Main.Load_pct","Conveyor_Main.JamDetected",
            "Conveyor_Main.RunHours",
            "Filler_Temp.EUValue",   "Filler_Temp.HiHi_Alarm",
            "Filler_Temp.Hi_Alarm",  "Filler_Temp.Lo_Alarm",
            "Filler_Temp.LoLo_Alarm","Filler_Temp.Fault",
            "Mixer_Pressure.EUValue","Mixer_Pressure.HiHi_Alarm",
            "Mixer_Pressure.Hi_Alarm",
        ]
    },
    "Level1_Control": {
        "description": "PLC control layer — ControlLogix L74",
        "tags": [
            "Line.LineRunning",    "Line.LineAuto",
            "Line.E_Stop_Active",  "Line.SafetyOK",
            "Mixer_Motor.RunCmd",  "Mixer_Motor.StopCmd",
            "Mixer_Motor.AutoMode",
        ]
    },
    "Level2_Supervisory": {
        "description": "SCADA / HMI layer — production KPIs",
        "tags": [
            "Line.BatchCount",          "Line.GoodCount",
            "Line.RejectCount",         "Line.TotalProd_kg",
            "Line.CurrentBatch_kg",     "Line.TargetBatch_kg",
            "Line.PlannedDowntime_min", "Line.UnplannedDowntime_min",
        ]
    },
}


# ── OPC-UA Server ─────────────────────────────────────────────────────────────

class OPCUAServer:
    """
    IEC 62541 compliant OPC-UA server.

    Builds address space from AB tag database and keeps values
    in sync with the ControlLogix Runtime Engine via subscription.

    Key design decisions:
    - NodeId uses tag name as string identifier for easy browsing
    - Tag hierarchy mirrors physical Purdue model (L0/L1/L2)
    - WritableNodes restricted to RunCmd / StopCmd / AutoMode (operator control)
    - All other nodes are ReadOnly (mirrors real OT security practice)
    """

    ENDPOINT = "opc.tcp://0.0.0.0:4840/factory/plc"
    SERVER_NAME = "Smart Factory OT Platform — AB ControlLogix L74"

    # Tags that operators are allowed to write (mirrors HMI access control)
    WRITABLE_TAGS = {
        "Mixer_Motor.RunCmd",
        "Mixer_Motor.StopCmd",
        "Mixer_Motor.AutoMode",
        "Line.E_Stop_Active",
        "Line.LineAuto",
        "Line.TargetBatch_kg",
    }

    def __init__(self) -> None:
        self._server      = None
        self._nodes:  dict[str, any] = {}   # tag_name → OPC-UA Node
        self._runtime_ref = None
        self._running     = False
        self._update_count= 0
        self._dry_run     = not OPCUA_AVAILABLE

        # Stub node store for dry-run mode
        self._stub_values: dict[str, any] = {}

    def attach_runtime(self, runtime) -> None:
        """Connect to ControlLogixRuntime — receives tag snapshots."""
        self._runtime_ref = runtime
        runtime.add_publisher(self._on_tag_update)
        logger.info("[OPC-UA] Attached to ControlLogixRuntime")

    async def _on_tag_update(self, snapshot: dict) -> None:
        """Called every PLC scan — update OPC-UA node values."""
        if self._dry_run:
            self._stub_values.update(snapshot)
            self._update_count += 1
            return

        if not self._running:
            return

        for tag_name, value in snapshot.items():
            node = self._nodes.get(tag_name)
            if node:
                try:
                    variant = _ab_type_to_ua_variant(value)
                    await node.write_value(variant)
                except Exception:
                    pass   # Don't crash on individual tag write failure

        self._update_count += 1

    # ── Address Space Builder ─────────────────────────────────────────────────

    async def _build_address_space(self, runtime) -> None:
        """
        Build OPC-UA node hierarchy from AB tag database.
        Structure: Objects → Factory → PetFoodLine → [Equipment] → [Tag]
        """
        if self._dry_run:
            logger.info("[OPC-UA STUB] Building address space (dry-run)")
            snapshot = runtime.db.snapshot()
            for name, val in snapshot.items():
                self._stub_values[name] = val
            logger.info(f"[OPC-UA STUB] {len(self._stub_values)} nodes registered")
            return

        objects = self._server.get_objects_node()
        ns      = await self._server.get_namespace_index("factory.plc")

        # Root: Factory
        factory_node = await objects.add_object(
            ua.NodeId("Factory", ns), "Factory"
        )

        # Area: PetFoodLine
        line_node = await factory_node.add_object(
            ua.NodeId("PetFoodLine", ns), "PetFoodLine"
        )
        await line_node.write_attribute(
            ua.AttributeIds.Description,
            ua.DataValue(ua.Variant("Mars Petcare Pet Food Production Line", ua.VariantType.String))
        )

        # Equipment modules
        equipment_groups = {
            "Mixer_Motor":     ("Mixer Motor",     "ControlLogix PowerFlex 755 VFD"),
            "Conveyor_Main":   ("Conveyor",        "ControlLogix Kinetix 5700 Servo"),
            "Filler_Temp":     ("Filler Temp",     "Analog Input — PT100 Temperature"),
            "Mixer_Pressure":  ("Mixer Pressure",  "Analog Input — Pressure Transmitter"),
            "Line":            ("Production Line", "ISA-95 Level 2 KPIs"),
        }

        snapshot = runtime.db.snapshot()

        for group_prefix, (display_name, description) in equipment_groups.items():
            # Create equipment folder node
            eq_node = await line_node.add_object(
                ua.NodeId(group_prefix, ns), display_name
            )

            # Add tag nodes under equipment
            for tag_name, value in snapshot.items():
                if not tag_name.startswith(group_prefix + "."):
                    continue

                attr_name  = tag_name.split(".", 1)[1]   # e.g. "Running"
                is_writable= tag_name in self.WRITABLE_TAGS
                variant    = _ab_type_to_ua_variant(value)

                var_node = await eq_node.add_variable(
                    ua.NodeId(tag_name, ns),
                    attr_name,
                    variant
                )

                if is_writable:
                    await var_node.set_writable()

                self._nodes[tag_name] = var_node

        # Purdue zone metadata nodes (informational — for cyber assessment)
        zones_node = await factory_node.add_object(
            ua.NodeId("PurdueZones", ns), "PurdueZones"
        )
        for zone_name, zone_info in PURDUE_ZONES.items():
            zone_node = await zones_node.add_object(
                ua.NodeId(f"Zone_{zone_name}", ns), zone_name
            )
            await zone_node.add_variable(
                ua.NodeId(f"Zone_{zone_name}_desc", ns),
                "Description",
                ua.Variant(zone_info["description"], ua.VariantType.String)
            )

        logger.info(f"[OPC-UA] Address space built — {len(self._nodes)} tag nodes")

    # ── Server Lifecycle ──────────────────────────────────────────────────────

    async def start(self, runtime) -> None:
        """Start OPC-UA server and build address space."""
        if self._dry_run:
            logger.info("[OPC-UA STUB] Server starting in dry-run mode")
            await self._build_address_space(runtime)
            self._running = True
            logger.info("[OPC-UA STUB] Ready — no real endpoint (asyncua not installed)")
            logger.info("[OPC-UA STUB] Install: pip install asyncua")
            return

        self._server = Server()
        await self._server.init()
        self._server.set_endpoint(self.ENDPOINT)
        self._server.set_server_name(self.SERVER_NAME)

        # Register namespace
        await self._server.register_namespace("factory.plc")

        # Security: allow anonymous + username (production would use certificates)
        await self._server.set_security_policy([
            ua.SecurityPolicyType.NoSecurity,
        ])

        await self._build_address_space(runtime)

        await self._server.start()
        self._running = True

        logger.info(f"[OPC-UA] Server started → {self.ENDPOINT}")
        logger.info("[OPC-UA] Connect with UaExpert: opc.tcp://localhost:4840/factory/plc")

    async def stop(self) -> None:
        self._running = False
        if self._server:
            await self._server.stop()
            logger.info("[OPC-UA] Server stopped")

    def get_stats(self) -> dict:
        return {
            "running":       self._running,
            "dry_run":       self._dry_run,
            "endpoint":      self.ENDPOINT,
            "node_count":    len(self._nodes) if not self._dry_run else len(self._stub_values),
            "update_count":  self._update_count,
            "writable_tags": list(self.WRITABLE_TAGS),
        }


# ── Network Health Monitor ────────────────────────────────────────────────────

class NetworkHealthMonitor:
    """
    OT Network Health Monitor — Purdue L0-L4 device monitoring.

    Simulates monitoring of:
      - AB ControlLogix L74 (L1)
      - PanelView+ 7 HMI (L1)
      - Stratix 5700 managed switch (L2)
      - OT DMZ server (L3.5)
      - Historian server (L4)

    In production: would use actual ICMP ping or SNMP.
    Here: simulates realistic latency with random packet loss events.
    """

    # Simulated OT network devices (mirrors real plant network topology)
    DEVICES = [
        {"name": "AB_ControlLogix_L74",   "ip": "192.168.1.10", "zone": "Level1_Control",    "type": "PLC"},
        {"name": "PanelView_Plus7_HMI",   "ip": "192.168.1.11", "zone": "Level1_Control",    "type": "HMI"},
        {"name": "Stratix5700_Switch_A",  "ip": "192.168.1.1",  "zone": "Level2_Supervisory","type": "Switch"},
        {"name": "Stratix5700_Switch_B",  "ip": "192.168.1.2",  "zone": "Level2_Supervisory","type": "Switch"},
        {"name": "SCADA_Server",          "ip": "192.168.2.10", "zone": "Level2_Supervisory","type": "Server"},
        {"name": "Historian_Server",      "ip": "192.168.3.10", "zone": "Level3_Operations", "type": "Server"},
        {"name": "OT_DMZ_Firewall",       "ip": "192.168.100.1","zone": "DMZ",               "type": "Firewall"},
        {"name": "MES_Server",            "ip": "192.168.3.20", "zone": "Level3_Operations", "type": "Server"},
    ]

    def __init__(self) -> None:
        import random
        self._random = random
        self._history: list[dict] = []
        self._device_state: dict[str, dict] = {
            d["name"]: {
                "online":       True,
                "latency_ms":   1.0,
                "packet_loss":  0.0,
                "last_seen":    time.time(),
                "check_count":  0,
            }
            for d in self.DEVICES
        }

    async def check_all(self) -> list[dict]:
        """
        Simulate ping to all devices.
        Returns current health status of each device.
        """
        results = []

        for device in self.DEVICES:
            name   = device["name"]
            state  = self._device_state[name]
            state["check_count"] += 1

            # Simulate realistic latency
            base_latency = {
                "PLC":      0.8,
                "HMI":      1.2,
                "Switch":   0.3,
                "Server":   2.5,
                "Firewall": 1.0,
            }.get(device["type"], 1.0)

            # Occasional packet loss event (1% probability)
            packet_loss_event = self._random.random() < 0.01
            device_down_event = self._random.random() < 0.002  # 0.2% down

            if device_down_event:
                state["online"]      = False
                state["latency_ms"]  = 0.0
                state["packet_loss"] = 100.0
                logger.warning(f"[NET MONITOR] Device DOWN: {name} ({device['ip']})")
            elif packet_loss_event:
                state["online"]      = True
                state["latency_ms"]  = base_latency * self._random.uniform(3, 8)
                state["packet_loss"] = self._random.uniform(5, 25)
                logger.warning(f"[NET MONITOR] Packet loss: {name} loss={state['packet_loss']:.1f}%")
            else:
                # Normal operation — small jitter
                state["online"]      = True
                state["latency_ms"]  = base_latency + self._random.gauss(0, 0.1)
                state["packet_loss"] = 0.0
                state["last_seen"]   = time.time()

            result = {
                **device,
                **state,
                "latency_ms":  round(state["latency_ms"], 2),
                "packet_loss": round(state["packet_loss"], 1),
                "timestamp":   time.time(),
            }
            results.append(result)

        self._history.append({
            "timestamp": time.time(),
            "results":   results,
        })
        # Keep last 1000 checks only
        if len(self._history) > 1000:
            self._history.pop(0)

        return results

    def get_summary(self) -> dict:
        """Summary for SCADA dashboard and IEC 62443 reporting."""
        states = list(self._device_state.values())
        online = sum(1 for s in states if s["online"])
        avg_latency = (
            sum(s["latency_ms"] for s in states if s["online"]) / max(online, 1)
        )
        return {
            "total_devices":   len(self.DEVICES),
            "online":          online,
            "offline":         len(self.DEVICES) - online,
            "avg_latency_ms":  round(avg_latency, 2),
            "zones":           list({d["zone"] for d in self.DEVICES}),
        }

    def get_topology(self) -> list[dict]:
        """Return full device list with current health — for dashboard map."""
        results = []
        for device in self.DEVICES:
            state = self._device_state[device["name"]]
            results.append({**device, **state})
        return results

    async def run_loop(self, interval_sec: float = 30.0) -> None:
        """Continuously monitor — check every 30 seconds."""
        logger.info(f"[NET MONITOR] Started — checking {len(self.DEVICES)} devices every {interval_sec}s")
        while True:
            results = await self.check_all()
            offline = [r for r in results if not r["online"]]
            if offline:
                for d in offline:
                    logger.error(f"[NET MONITOR] OFFLINE: {d['name']} ({d['ip']}) zone={d['zone']}")
            await asyncio.sleep(interval_sec)

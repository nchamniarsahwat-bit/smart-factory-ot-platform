"""
core/protocols/mqtt_publisher.py
MQTT Publisher — Tag Snapshot Pipeline

Publishes AB ControlLogix tag snapshots every 100ms to MQTT broker.
Downstream subscribers (SCADA, ML pipeline, historian) all read from here.

Topic structure:
  factory/plc/tags/snapshot        ← full snapshot every 100ms
  factory/plc/tags/{tag_name}      ← individual tag on change only
  factory/plc/alarms               ← alarm events
  factory/plc/faults               ← fault injection events
  factory/plc/status               ← runtime health (scan times, task stats)

Run standalone:
  python -m core.protocols.mqtt_publisher

Requires Mosquitto broker running:
  Windows:  mosquitto -v
  Docker:   docker run -p 1883:1883 eclipse-mosquitto
"""
from __future__ import annotations

import asyncio
import json
import time
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable

logger = logging.getLogger("mqtt_publisher")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

# ── Try importing paho-mqtt, give clear error if missing ─────────────────────
try:
    import paho.mqtt.client as mqtt
    MQTT_AVAILABLE = True
except ImportError:
    MQTT_AVAILABLE = False
    logger.warning("paho-mqtt not installed — running in DRY RUN mode")
    logger.warning("Install:  pip install paho-mqtt")


# ── Config ────────────────────────────────────────────────────────────────────

@dataclass
class MQTTConfig:
    host:            str   = "localhost"
    port:            int   = 1883
    keepalive:       int   = 60
    publish_interval_ms: float = 100.0    # snapshot every 100ms
    qos:             int   = 0            # QoS 0 for high-frequency tags
    alarm_qos:       int   = 1            # QoS 1 for alarms (at-least-once)
    client_id:       str   = "plc-runtime-publisher"
    username:        str   = ""
    password:        str   = ""

    # Topic prefixes
    topic_base:      str   = "factory/plc"

    @property
    def topic_snapshot(self) -> str:
        return f"{self.topic_base}/tags/snapshot"

    @property
    def topic_alarms(self) -> str:
        return f"{self.topic_base}/alarms"

    @property
    def topic_faults(self) -> str:
        return f"{self.topic_base}/faults"

    @property
    def topic_status(self) -> str:
        return f"{self.topic_base}/status"

    def topic_tag(self, tag_name: str) -> str:
        # Replace dots with slashes for MQTT hierarchy
        # e.g. Mixer_Motor.Running → factory/plc/tags/Mixer_Motor/Running
        return f"{self.topic_base}/tags/{tag_name.replace('.', '/')}"


# ── Alarm Detector ────────────────────────────────────────────────────────────

class AlarmDetector:
    """
    Detects alarm conditions from tag snapshot.
    Mirrors ISA-18.2 alarm management concept.
    Published separately to factory/plc/alarms with QoS 1.
    """

    # Tags that trigger alarms when True
    ALARM_TAGS = {
        "Mixer_Motor.Faulted":       {"priority": 1, "message": "Mixer motor fault"},
        "Mixer_Motor.Overload":      {"priority": 1, "message": "Mixer motor overload"},
        "Conveyor_Main.JamDetected": {"priority": 2, "message": "Conveyor jam detected"},
        "Conveyor_Main.E_Stop":      {"priority": 1, "message": "Conveyor E-Stop active"},
        "Line.E_Stop_Active":        {"priority": 1, "message": "Line E-Stop active"},
        "Filler_Temp.HiHi_Alarm":    {"priority": 1, "message": "Filler temp HIHI alarm"},
        "Filler_Temp.Hi_Alarm":      {"priority": 2, "message": "Filler temp HIGH alarm"},
        "Filler_Temp.Lo_Alarm":      {"priority": 2, "message": "Filler temp LOW alarm"},
        "Filler_Temp.LoLo_Alarm":    {"priority": 1, "message": "Filler temp LOLO alarm"},
        "Filler_Temp.Fault":         {"priority": 2, "message": "Filler temp sensor fault"},
        "Mixer_Pressure.HiHi_Alarm": {"priority": 1, "message": "Mixer pressure HIHI"},
        "Mixer_Pressure.Hi_Alarm":   {"priority": 2, "message": "Mixer pressure HIGH"},
    }

    def __init__(self) -> None:
        self._active: dict[str, dict] = {}   # tag → alarm record
        self._history: list[dict]     = []

    def check(self, snapshot: dict) -> list[dict]:
        """
        Compare snapshot against alarm conditions.
        Returns list of new alarm events (rising edge only).
        """
        new_alarms = []

        for tag_name, alarm_def in self.ALARM_TAGS.items():
            val = snapshot.get(tag_name)
            is_active = bool(val)

            if is_active and tag_name not in self._active:
                # New alarm — rising edge
                event = {
                    "alarm_id":  f"ALM-{int(time.time()*1000)}",
                    "tag":       tag_name,
                    "priority":  alarm_def["priority"],
                    "message":   alarm_def["message"],
                    "value":     val,
                    "timestamp": time.time(),
                    "state":     "ACTIVE",
                    "acked":     False,
                }
                self._active[tag_name] = event
                self._history.append(event)
                new_alarms.append(event)

            elif not is_active and tag_name in self._active:
                # Alarm cleared
                cleared = dict(self._active.pop(tag_name))
                cleared["state"]       = "CLEARED"
                cleared["cleared_at"]  = time.time()
                self._history.append(cleared)

        return new_alarms

    def get_active(self) -> list[dict]:
        return list(self._active.values())

    def get_summary(self) -> dict:
        by_priority = {1: 0, 2: 0, 3: 0, 4: 0}
        for a in self._active.values():
            by_priority[a["priority"]] = by_priority.get(a["priority"], 0) + 1
        return {
            "total_active": len(self._active),
            "priority_1":   by_priority[1],
            "priority_2":   by_priority[2],
            "priority_3":   by_priority[3],
            "priority_4":   by_priority[4],
        }


# ── Change Detector ───────────────────────────────────────────────────────────

class ChangeDetector:
    """
    Detects tag value changes between snapshots.
    Only changed tags are published to individual topics (bandwidth efficient).
    """

    def __init__(self) -> None:
        self._prev: dict[str, any] = {}

    def get_changes(self, snapshot: dict) -> dict[str, any]:
        """Return only tags whose value changed since last snapshot."""
        changes = {}
        for name, val in snapshot.items():
            if name not in self._prev or self._prev[name] != val:
                changes[name] = val
        self._prev = dict(snapshot)
        return changes


# ── MQTT Publisher ────────────────────────────────────────────────────────────

class MQTTPublisher:
    """
    Async MQTT publisher — connects to broker and publishes:
      1. Full tag snapshot every 100ms   → factory/plc/tags/snapshot
      2. Changed tags only               → factory/plc/tags/{name}
      3. New alarm events                → factory/plc/alarms
      4. Fault events from injector      → factory/plc/faults
      5. Runtime status every 5s         → factory/plc/status
    """

    def __init__(self, config: MQTTConfig | None = None) -> None:
        self.cfg             = config or MQTTConfig()
        self.alarm_detector  = AlarmDetector()
        self.change_detector = ChangeDetector()
        self._client         = None
        self._connected      = False
        self._dry_run        = not MQTT_AVAILABLE
        self._publish_count  = 0
        self._last_snapshot: dict = {}
        self._runtime_ref    = None    # set by attach_runtime()

        # Callbacks registered by downstream modules
        self._on_snapshot_callbacks: list[Callable] = []

    def attach_runtime(self, runtime) -> None:
        """Connect publisher to a ControlLogixRuntime instance."""
        self._runtime_ref = runtime
        runtime.add_publisher(self._on_new_snapshot)
        logger.info("[MQTT] Attached to ControlLogixRuntime")

    async def _on_new_snapshot(self, snapshot: dict) -> None:
        """Called by runtime every scan — cache latest snapshot."""
        self._last_snapshot = snapshot

    def on_snapshot(self, cb: Callable) -> None:
        """Register callback for downstream modules (e.g. historian)."""
        self._on_snapshot_callbacks.append(cb)

    # ── MQTT Connection ───────────────────────────────────────────────────────

    def _setup_client(self) -> None:
        if self._dry_run:
            return

        self._client = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION1,
        client_id=self.cfg.client_id,
        protocol=mqtt.MQTTv311
        )
        if self.cfg.username:
            self._client.username_pw_set(self.cfg.username, self.cfg.password)

        self._client.on_connect    = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_publish    = self._on_publish

        # Last-will: publish offline status if client disconnects unexpectedly
        will_payload = json.dumps({
            "status": "OFFLINE",
            "timestamp": time.time()
        })
        self._client.will_set(
            topic=self.cfg.topic_status,
            payload=will_payload,
            qos=1,
            retain=True
        )

    def _on_connect(self, client, userdata, flags, rc) -> None:
        if rc == 0:
            self._connected = True
            logger.info(f"[MQTT] Connected to {self.cfg.host}:{self.cfg.port}")
            # Publish online status
            client.publish(
                self.cfg.topic_status,
                json.dumps({"status": "ONLINE", "timestamp": time.time()}),
                qos=1, retain=True
            )
        else:
            logger.error(f"[MQTT] Connection failed — rc={rc}")

    def _on_disconnect(self, client, userdata, rc) -> None:
        self._connected = False
        logger.warning(f"[MQTT] Disconnected — rc={rc}")

    def _on_publish(self, client, userdata, mid) -> None:
        pass   # silent — high frequency

    # ── Publish Helpers ───────────────────────────────────────────────────────

    def _publish(self, topic: str, payload: dict,
                 qos: int = 0, retain: bool = False) -> None:
        """Serialize to JSON and publish."""
        msg = json.dumps(payload, default=str)
        if self._dry_run:
            return   # no-op in dry-run
        if self._connected and self._client:
            self._client.publish(topic, msg, qos=qos, retain=retain)
            self._publish_count += 1

    # ── Main Publish Loop ─────────────────────────────────────────────────────

    async def run(self) -> None:
        """Start publish loop — call after attach_runtime()."""
        if not self._dry_run:
            self._setup_client()
            try:
                self._client.connect(
                    self.cfg.host,
                    self.cfg.port,
                    self.cfg.keepalive
                )
                self._client.loop_start()
                logger.info(f"[MQTT] Connecting to {self.cfg.host}:{self.cfg.port}...")
                await asyncio.sleep(1)   # wait for connection
            except Exception as e:
                logger.warning(f"[MQTT] Cannot connect to broker: {e}")
                logger.warning("[MQTT] Switching to DRY RUN mode")
                self._dry_run = True

        asyncio.create_task(self._snapshot_loop())
        asyncio.create_task(self._status_loop())
        logger.info(f"[MQTT] Publisher running — interval={self.cfg.publish_interval_ms}ms")

    async def _snapshot_loop(self) -> None:
        """Publish full snapshot + changed tags every 100ms."""
        interval = self.cfg.publish_interval_ms / 1000.0

        while True:
            t0 = time.perf_counter()

            if self._last_snapshot:
                snapshot = self._last_snapshot
                ts       = time.time()

                # 1. Full snapshot
                envelope = {
                    "timestamp": ts,
                    "source":    "AB_ControlLogix_L74",
                    "scan_ms":   round((time.perf_counter() - t0) * 1000, 3),
                    "tags":      snapshot,
                }
                self._publish(self.cfg.topic_snapshot, envelope)

                # 2. Changed tags → individual topics
                changes = self.change_detector.get_changes(snapshot)
                for tag_name, val in changes.items():
                    self._publish(
                        self.cfg.topic_tag(tag_name),
                        {"value": val, "timestamp": ts},
                        qos=0
                    )

                # 3. Alarm detection
                new_alarms = self.alarm_detector.check(snapshot)
                for alarm in new_alarms:
                    self._publish(self.cfg.topic_alarms, alarm, qos=self.cfg.alarm_qos)
                    logger.warning(f"[ALARM P{alarm['priority']}] {alarm['message']}")

                # 4. Notify downstream callbacks (historian, ML pipeline)
                for cb in self._on_snapshot_callbacks:
                    asyncio.create_task(cb(snapshot, ts))

            elapsed = time.perf_counter() - t0
            await asyncio.sleep(max(0, interval - elapsed))

    async def _status_loop(self) -> None:
        """Publish runtime status every 5 seconds."""
        while True:
            await asyncio.sleep(5)

            status = {
                "timestamp":      time.time(),
                "publish_count":  self._publish_count,
                "connected":      self._connected,
                "dry_run":        self._dry_run,
                "active_alarms":  self.alarm_detector.get_summary(),
            }

            if self._runtime_ref:
                status["task_stats"] = self._runtime_ref.get_task_stats()

            self._publish(self.cfg.topic_status, status, qos=1, retain=True)

            if self._dry_run:
                # Print to console in dry-run mode so you can see it working
                active = self.alarm_detector.get_active()
                snap   = self._last_snapshot
                logger.info(
                    f"[STATUS] publishes={self._publish_count} | "
                    f"active_alarms={len(active)} | "
                    f"motor={'RUN' if snap.get('Mixer_Motor.Running') else 'STOP'} | "
                    f"line={'RUN' if snap.get('Line.LineRunning') else 'STOP'} | "
                    f"batch_kg={snap.get('Line.CurrentBatch_kg', 0):.1f}"
                )

    def disconnect(self) -> None:
        if self._client and self._connected:
            self._client.loop_stop()
            self._client.disconnect()

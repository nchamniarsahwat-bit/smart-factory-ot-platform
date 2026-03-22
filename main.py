"""
main.py
Smart Factory OT Platform — Main Entry Point

Boots AB ControlLogix Runtime + MQTT Publisher together.

Usage:
  python main.py                  # with MQTT broker
  python main.py --dry-run        # no broker needed (console output)
  python main.py --host 192.168.1.10 --port 1883

Subscribe to tags (test in another terminal):
  mosquitto_sub -h localhost -t "factory/plc/tags/snapshot" -v
  mosquitto_sub -h localhost -t "factory/plc/alarms" -v
  mosquitto_sub -h localhost -t "factory/plc/#" -v
"""
from __future__ import annotations

import asyncio
import argparse
import logging
import signal
import sys


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger("main")


async def run(host: str = "localhost", port: int = 1883,
              dry_run: bool = False) -> None:

    from core.plc.runtime import ControlLogixRuntime
    from core.protocols.mqtt_publisher import MQTTPublisher, MQTTConfig

    # ── Boot runtime ──────────────────────────────────────────────────────────
    runtime = ControlLogixRuntime()

    # ── Boot MQTT publisher ───────────────────────────────────────────────────
    cfg = MQTTConfig(host=host, port=port)
    publisher = MQTTPublisher(cfg)
    publisher.attach_runtime(runtime)

    if dry_run:
        publisher._dry_run = True
        logger.info("[Main] DRY RUN mode — no broker needed")

    await publisher.run()

    # ── Graceful shutdown ─────────────────────────────────────────────────────
    loop = asyncio.get_running_loop()

    def _shutdown():
        logger.info("[Main] Shutdown signal received")
        publisher.disconnect()
        loop.stop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _shutdown)
        except NotImplementedError:
            pass   # Windows doesn't support add_signal_handler

    # ── Start PLC runtime (blocks until stopped) ──────────────────────────────
    try:
        await runtime.start()
    except KeyboardInterrupt:
        logger.info("[Main] KeyboardInterrupt — stopping")
        publisher.disconnect()
        await runtime.stop()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Smart Factory OT Platform — AB ControlLogix Runtime + MQTT"
    )
    parser.add_argument("--host",     default="localhost", help="MQTT broker host")
    parser.add_argument("--port",     default=1883, type=int, help="MQTT broker port")
    parser.add_argument("--dry-run",  action="store_true",
                        help="Run without MQTT broker (console output)")
    args = parser.parse_args()

    asyncio.run(run(host=args.host, port=args.port, dry_run=args.dry_run))


if __name__ == "__main__":
    main()

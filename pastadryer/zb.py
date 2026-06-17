"""Zigbee-Geräteschicht über MQTT / zigbee2mqtt (ersetzt die HomeKit-Schicht).

- Hört auf `zigbee2mqtt/<gerät>` und hält die letzten Werte im Cache (Push,
  kein Polling → keine Sensor-Batterie-Kosten).
- Schaltet Relais via `zigbee2mqtt/<gerät>/set`  {"state_l1": "ON"/"OFF"}.
"""
from __future__ import annotations

import asyncio
import json
import logging

import aiomqtt

log = logging.getLogger(__name__)


class Zigbee:
    def __init__(self, host: str = "localhost", port: int = 1883, base: str = "zigbee2mqtt"):
        self.host = host
        self.port = port
        self.base = base
        self.cache: dict[str, dict] = {}     # friendly_name -> letztes Payload
        self.last_at: dict[str, float] = {}  # friendly_name -> monotonic der letzten Meldung
        self._client: aiomqtt.Client | None = None
        self._running = False
        self._task: asyncio.Task | None = None
        self._connected = asyncio.Event()

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._run())
        try:
            await asyncio.wait_for(self._connected.wait(), timeout=10)
            log.info("MQTT verbunden (%s:%s)", self.host, self.port)
        except asyncio.TimeoutError:
            log.warning("MQTT noch nicht verbunden — versuche im Hintergrund weiter")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _run(self) -> None:
        while self._running:
            try:
                async with aiomqtt.Client(self.host, port=self.port) as client:
                    self._client = client
                    self._connected.set()
                    await client.subscribe(f"{self.base}/+")
                    async for msg in client.messages:
                        name = msg.topic.value[len(self.base) + 1:]
                        if "/" in name:          # /set, /get, bridge/... ignorieren
                            continue
                        try:
                            self.cache[name] = json.loads(msg.payload)
                            self.last_at[name] = asyncio.get_event_loop().time()
                        except Exception:
                            pass
            except aiomqtt.MqttError as e:
                log.warning("MQTT getrennt: %s — reconnect in 5s", e)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.exception("MQTT-Schleife: %s", e)
            self._connected.clear()
            self._client = None
            if self._running:
                await asyncio.sleep(5)

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    # ---- Lesen (aus dem Push-Cache) ----
    def get(self, friendly_name: str) -> dict:
        return self.cache.get(friendly_name) or {}

    def age(self, friendly_name: str) -> float | None:
        t = self.last_at.get(friendly_name)
        return (asyncio.get_event_loop().time() - t) if t else None

    # ---- Schalten ----
    async def set_state(self, device: str, prop: str, on: bool) -> None:
        if not self._client:
            raise RuntimeError("MQTT nicht verbunden")
        await self._client.publish(f"{self.base}/{device}/set",
                                   json.dumps({prop: "ON" if on else "OFF"}))

    async def request(self, device: str, props: list[str]) -> None:
        """Aktuellen Zustand anfragen (z.B. Relais beim Start)."""
        if not self._client:
            return
        await self._client.publish(f"{self.base}/{device}/get",
                                   json.dumps({p: "" for p in props}))

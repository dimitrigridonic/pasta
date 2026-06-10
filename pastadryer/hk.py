"""Dünne HomeKit-Schicht über aiohomekit.

Lädt ein bestehendes Pairing (siehe cli.py) und bietet einfache
get/set-Methoden auf einzelne Characteristics (aid/iid).
"""
from __future__ import annotations

import logging
from contextlib import AsyncExitStack
from pathlib import Path

from aiohomekit import Controller
from aiohomekit.characteristic_cache import CharacteristicCacheFile
from aiohomekit.zeroconf import ZeroconfServiceListener
from zeroconf.asyncio import AsyncServiceBrowser, AsyncZeroconf

log = logging.getLogger(__name__)

# aiohomekit erwartet, dass ein mDNS-Browser für diese Typen läuft.
HAP_TYPES = ["_hap._tcp.local.", "_hap._udp.local."]


class HomeKit:
    def __init__(self, pairing_file: str, alias: str):
        self.pairing_file = pairing_file
        self.alias = alias
        self._stack = AsyncExitStack()
        self.controller: Controller | None = None
        self.pairing = None

    async def start(self) -> None:
        azc = await self._stack.enter_async_context(AsyncZeroconf())
        # HAP-Browser muss laufen, BEVOR der Controller startet
        browser = AsyncServiceBrowser(azc.zeroconf, HAP_TYPES, listener=ZeroconfServiceListener())
        self._stack.push_async_callback(browser.async_cancel)
        charmap = Path(self.pairing_file).resolve().parent / "charmap.json"
        self.controller = await self._stack.enter_async_context(
            Controller(async_zeroconf_instance=azc, char_cache=CharacteristicCacheFile(charmap))
        )
        # load_data liest pairing.json und füllt controller.aliases
        self.controller.load_data(self.pairing_file)
        if self.alias not in self.controller.aliases:
            raise RuntimeError(
                f"Pairing-Alias '{self.alias}' nicht in {self.pairing_file} gefunden. "
                f"Erst pairen: python -m pastadryer.cli pair"
            )
        self.pairing = self.controller.aliases[self.alias]
        log.info("HomeKit-Pairing '%s' geladen", self.alias)

    async def stop(self) -> None:
        await self._stack.aclose()

    async def get_values(self, points: list[tuple[int, int]]) -> dict[tuple[int, int], object]:
        """Liest mehrere Characteristics. points: [(aid, iid), ...]
        Rückgabe: {(aid, iid): value}.
        """
        raw = await self.pairing.get_characteristics(points)
        out: dict[tuple[int, int], object] = {}
        for key, payload in raw.items():
            out[key] = payload.get("value") if isinstance(payload, dict) else payload
        return out

    async def get_value(self, aid: int, iid: int):
        res = await self.get_values([(aid, iid)])
        return res.get((aid, iid))

    async def set_value(self, aid: int, iid: int, value) -> None:
        """Schreibt eine Characteristic (z.B. Switch On = True/False)."""
        result = await self.pairing.put_characteristics([(aid, iid, value)])
        # put_characteristics liefert nur Einträge für FEHLER zurück (status != 0)
        if result:
            problems = {k: v for k, v in result.items()}
            raise RuntimeError(f"Schreiben fehlgeschlagen ({aid},{iid}): {problems}")

    async def list_accessories(self):
        return await self.pairing.list_accessories_and_characteristics()

"""Kommandozeilen-Helfer für Einrichtung des HomeKit-Pairings.

  python -m pastadryer.cli discover            # Geräte im Netz finden
  python -m pastadryer.cli pair                # mit dem M2 pairen
  python -m pastadryer.cli dump                # alle aid/iid auflisten

Das Pairing wird in pairing.json gespeichert; daraus liest die App später.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from aiohomekit import Controller
from aiohomekit.characteristic_cache import CharacteristicCacheFile
from aiohomekit.zeroconf import ZeroconfServiceListener
from zeroconf.asyncio import AsyncServiceBrowser, AsyncZeroconf

HAP_TYPES = ["_hap._tcp.local.", "_hap._udp.local."]


@asynccontextmanager
async def open_controller():
    """Controller mit laufendem HAP-mDNS-Browser (wie aiohomekit's eigenes CLI)."""
    azc = AsyncZeroconf()
    charmap = Path(PAIRING_FILE).resolve().parent / "charmap.json"
    controller = Controller(
        async_zeroconf_instance=azc, char_cache=CharacteristicCacheFile(charmap)
    )
    async with azc:
        browser = AsyncServiceBrowser(azc.zeroconf, HAP_TYPES, listener=ZeroconfServiceListener())
        async with controller:
            yield controller
        await browser.async_cancel()

PAIRING_FILE = "pairing.json"
DEFAULT_ALIAS = "pastadryer"

# Bekannte HomeKit-UUIDs (Kurzform) -> lesbarer Name, nur fürs Anzeigen.
KNOWN = {
    "00000011": "Aktuelle Temperatur",
    "00000010": "Aktuelle Luftfeuchte",
    "00000025": "An/Aus (Switch)  <-- schaltbar",
    "00000023": "Name",
    "00000068": "Batterie",
    "00000026": "Outlet in Use",
    "0000008a": "» Service: Temperatursensor",
    "00000082": "» Service: Feuchtesensor",
    "00000049": "» Service: Schalter",
    "00000047": "» Service: Steckdose",
    "0000003e": "» Service: Accessory Info",
    "000000a2": "» Service: Bridge/Firmware",
}


def _short(uuid: str) -> str:
    """Erste 8 Hex-Zeichen einer HomeKit-UUID (klein), für Lookup."""
    return str(uuid).split("-")[0].lower().zfill(8)[-8:]


def _label(uuid: str) -> str:
    return KNOWN.get(_short(uuid), uuid)


async def cmd_discover(args) -> None:
    print("Suche HomeKit-Geräte…  Der M2 muss im selben Netz und HomeKit aktiviert sein.")
    async with open_controller() as controller:
        await asyncio.sleep(8)  # Browser muss die Geräte erst per mDNS sammeln
        found = False
        async for d in controller.async_discover():
            found = True
            desc = d.description
            print(f"  Gerät-ID : {getattr(desc, 'id', desc)}")
            print(f"  Name     : {getattr(desc, 'name', '?')}")
            print(f"  Modell   : {getattr(desc, 'model', '?')}")
            print(f"  Adresse  : {getattr(desc, 'address', '?')}:{getattr(desc, 'port', '?')}")
            sf = int(getattr(desc, "status_flags", 0) or 0)
            print(f"  Status   : {'UNGEPAIRT (pairbar) ✓' if sf & 1 else 'bereits gepairt → erst zurücksetzen'}")
            print("  " + "-" * 40)
        if not found:
            print("Nichts gefunden. HomeKit am M2 aktiviert? Pi im selben (W)LAN/Subnetz?")


async def cmd_pair(args) -> None:
    device_id = args.device or input("Gerät-ID des M2 (aus 'discover', Form AA:BB:CC:DD:EE:FF): ").strip()
    pin = args.pin or input("HomeKit Setup-Code (Form 123-45-678, aus der Aqara-App): ").strip()
    async with open_controller() as controller:
        if os.path.exists(PAIRING_FILE):
            controller.load_data(PAIRING_FILE)  # bestehende Pairings nicht überschreiben
        if args.alias in controller.aliases:
            print(f"Alias '{args.alias}' existiert schon. Erst entfernen oder anderen Alias wählen.")
            return
        discovery = await controller.async_find(device_id)
        finish_pairing = await discovery.async_start_pairing(args.alias)
        pairing = await finish_pairing(pin)
        # aiohomekit 3.2.20 trägt finish_pairing nur in controller.pairings ein,
        # save_data liest aber aus controller.aliases -> explizit registrieren:
        controller.aliases[args.alias] = pairing
        controller.save_data(PAIRING_FILE)

    # Verifizieren, dass die Schlüssel wirklich auf der Platte sind
    with open(PAIRING_FILE) as fh:
        saved = json.load(fh)
    if args.alias not in saved:
        print(f"\n❌ Pairing NICHT gespeichert ({PAIRING_FILE} ohne '{args.alias}').")
        return
    print(f"\n✅ Gepairt als '{args.alias}', gespeichert in {PAIRING_FILE}.")
    print("Jetzt:  python -m pastadryer.cli dump")


async def cmd_dump(args) -> None:
    async with open_controller() as controller:
        if not os.path.exists(PAIRING_FILE):
            print(f"{PAIRING_FILE} fehlt. Erst 'pair'.")
            return
        controller.load_data(PAIRING_FILE)
        if args.alias not in controller.aliases:
            print(f"Alias '{args.alias}' nicht gefunden. Erst 'pair'.")
            return
        pairing = controller.aliases[args.alias]
        data = await pairing.list_accessories_and_characteristics()

    print("Gefundene Geräte (aid/iid für config.yaml):\n")
    for acc in data:
        aid = acc.get("aid")
        print(f"Accessory aid={aid}")
        for svc in acc.get("services", []):
            print(f"  {_label(svc.get('type'))}  (service iid={svc.get('iid')})")
            for ch in svc.get("characteristics", []):
                perms = ",".join(ch.get("perms", []))
                val = ch.get("value", "")
                print(
                    f"      iid={ch.get('iid'):<4} {_label(ch.get('type')):<28}"
                    f" wert={val!s:<10} perms={perms}"
                )
        print()
    print("Notiere dir die aid + iid von: Temperatur, Luftfeuchte, und den beiden")
    print("'An/Aus (Switch)'-Einträgen (Lüfter & Heizung) für die config.yaml.")


async def cmd_switch(args) -> None:
    async with open_controller() as controller:
        if not os.path.exists(PAIRING_FILE):
            print(f"{PAIRING_FILE} fehlt. Erst 'pair'.")
            return
        controller.load_data(PAIRING_FILE)
        if args.alias not in controller.aliases:
            print(f"Alias '{args.alias}' nicht gefunden.")
            return
        pairing = controller.aliases[args.alias]
        value = str(args.state).lower() in ("on", "1", "true", "an", "ein")
        res = await pairing.put_characteristics([(args.aid, args.iid, value)])
        if res:
            print(f"❌ Fehler beim Schalten: {res}")
        else:
            print(f"✅ aid={args.aid} iid={args.iid} -> {'AN' if value else 'AUS'}")


async def cmd_sensors(args) -> None:
    """Liest live alle Temperatur-/Feuchte-Sensoren (zum Identifizieren)."""
    async with open_controller() as controller:
        controller.load_data(PAIRING_FILE)
        pairing = controller.aliases[args.alias]
        data = await pairing.list_accessories_and_characteristics()
        # Temp (00000011) + Feuchte (00000010) je Accessory finden
        points, meta = [], {}
        for acc in data:
            aid = acc.get("aid")
            for svc in acc.get("services", []):
                for ch in svc.get("characteristics", []):
                    s = _short(ch.get("type"))
                    if s in ("00000011", "00000010"):
                        iid = ch.get("iid")
                        points.append((aid, iid))
                        meta[(aid, iid)] = "T" if s == "00000011" else "rF"
        live = await pairing.get_characteristics(points)
    # nach aid gruppieren
    rows = {}
    for (aid, iid), payload in live.items():
        val = payload.get("value") if isinstance(payload, dict) else payload
        rows.setdefault(aid, {})[meta[(aid, iid)]] = (val, iid)
    print("Live-Sensorwerte:")
    for aid in sorted(rows):
        t = rows[aid].get("T"); h = rows[aid].get("rF")
        ts = f"{t[0]}°C (iid {t[1]})" if t else "-"
        hs = f"{h[0]}% (iid {h[1]})" if h else "-"
        print(f"  aid={aid:<4} T={ts:<22} rF={hs}")


async def cmd_monitor(args) -> None:
    """Liest Sensoren wiederholt und hebt Änderungen hervor (Anhauch-Test)."""
    async with open_controller() as controller:
        controller.load_data(PAIRING_FILE)
        pairing = controller.aliases[args.alias]
        data = await pairing.list_accessories_and_characteristics()
        points, meta = [], {}
        for acc in data:
            aid = acc.get("aid")
            for svc in acc.get("services", []):
                for ch in svc.get("characteristics", []):
                    s = _short(ch.get("type"))
                    if s in ("00000011", "00000010"):
                        points.append((aid, ch.get("iid")))
                        meta[(aid, ch.get("iid"))] = "T" if s == "00000011" else "rF"

        def read_grouped(live):
            rows = {}
            for k, payload in live.items():
                val = payload.get("value") if isinstance(payload, dict) else payload
                rows.setdefault(k[0], {})[meta[k]] = val
            return rows

        base = read_grouped(await pairing.get_characteristics(points))
        print(f"Baseline: {base}", flush=True)
        for r in range(args.rounds):
            await asyncio.sleep(args.interval)
            now = read_grouped(await pairing.get_characteristics(points))
            changed = []
            for aid in sorted(now):
                dt = (now[aid].get("T") or 0) - (base[aid].get("T") or 0)
                dh = (now[aid].get("rF") or 0) - (base[aid].get("rF") or 0)
                if abs(dt) >= 0.3 or abs(dh) >= 2:
                    changed.append(f"aid={aid} T={now[aid].get('T')}({dt:+.1f}) rF={now[aid].get('rF')}({dh:+.0f})")
            ts = int((r + 1) * args.interval)
            print(f"[{ts:>4}s] " + (" ÄNDERUNG: " + " | ".join(changed) if changed else "-"), flush=True)


async def cmd_get(args) -> None:
    async with open_controller() as controller:
        controller.load_data(PAIRING_FILE)
        pairing = controller.aliases[args.alias]
        res = await pairing.get_characteristics([(args.aid, args.iid)])
        print(res)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="pastadryer.cli")
    p.add_argument("--alias", default=DEFAULT_ALIAS, help="Pairing-Alias (Default: pastadryer)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("discover", help="Geräte im Netz finden")

    pp = sub.add_parser("pair", help="mit dem M2 pairen")
    pp.add_argument("-d", "--device", help="Gerät-ID (sonst Abfrage)")
    pp.add_argument("-p", "--pin", help="Setup-Code 123-45-678 (sonst Abfrage)")

    sub.add_parser("dump", help="alle aid/iid auflisten")

    sw = sub.add_parser("switch", help="Schalten: switch <aid> <iid> <on|off>")
    sw.add_argument("aid", type=int)
    sw.add_argument("iid", type=int)
    sw.add_argument("state", help="on|off")

    g = sub.add_parser("get", help="Wert lesen: get <aid> <iid>")
    g.add_argument("aid", type=int)
    g.add_argument("iid", type=int)

    sub.add_parser("sensors", help="alle Temp/Feuchte-Sensoren live lesen")

    mon = sub.add_parser("monitor", help="Sensoren wiederholt lesen (Anhauch-Test)")
    mon.add_argument("--rounds", type=int, default=15)
    mon.add_argument("--interval", type=int, default=4)

    args = p.parse_args(argv)
    fn = {
        "discover": cmd_discover, "pair": cmd_pair, "dump": cmd_dump,
        "switch": cmd_switch, "get": cmd_get, "sensors": cmd_sensors,
        "monitor": cmd_monitor,
    }[args.cmd]
    try:
        asyncio.run(fn(args))
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())

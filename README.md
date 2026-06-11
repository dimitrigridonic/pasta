# 🍝 Pasta-Trockner

Lokale Steuerung eines selbstgebauten Pasta-Trockners (essiccatoio) – rein lokal,
keine Cloud. Eine eigene Regel-Engine + Browser-Dashboard ersetzen die mühsamen
Aqara-Automationen. Läuft auf einem Raspberry Pi direkt am Trockner und spricht die
Sensoren und Relais **direkt über Zigbee** an (Sonoff-Funkstick + zigbee2mqtt).

## Was es kann

- **6 Temp/Feuchte-Sensoren** (Aqara) live per Push, Verlaufs-Aufzeichnung (SQLite)
  mit Chart & CSV-Export – inkl. Batterieanzeige je Sensor
- **2 Doppel-Relais** = 4 Kanäle: Heizung links/rechts, Lüfter links/rechts –
  mit **Leistungsmessung** (Watt/Volt/Energie)
- **Regelung** nach dem Prinzip *„Feuchte gewinnt"* (siehe unten)
- **Vorheizen** des leeren Kastens vor jedem Programm
- **Trocken-Programme** (z.B. *Maccheroni* 16-20-20-12 h, *Caserecce* 10-10-5 h) –
  im Browser editierbar
- **Sicherheits-Not-Aus** (verriegelnd) bei Heizungs-Dauerlauf
- **Dashboard**: schwarzes, fluid-responsives Web-UI (Handy + Desktop), Tabs für
  Home / Trockner-Schema / Programme
- **Zugriff von überall** über Tailscale (privates VPN), ohne offene Ports

## Regel-Modell („Feuchte gewinnt")

Jede Programm-Phase definiert eine **Ideallinie** (Feuchte über Zeit, als Rampe).
Die Feuchte soll ihr folgen und nie darunter fallen:

- **Über der Linie:** Heizung hält das Temperatur-Band (≈30–32 °C, mit Trägheit/
  Mindestlaufzeiten) und trocknet. Heizung **und** Lüfter wechseln dabei im Takt die
  Seite (links/rechts).
- **An/unter der Linie:** dynamische **Ruhephase** – alles aus, bis sich die Feuchte
  wieder über die Linie (+ Reserve) erholt hat. Dauer dynamisch, nicht fix.
- **Lüfter = Notnagel:** nur bei echtem Stillstand (Feuchte fällt über Stunden nicht).
- **Temperatur ist die Konstante**, die Feuchte das Ziel.

## Architektur

```
Aqara-Sensoren + 2 Sonoff/Aqara-Doppelrelais
   │  Zigbee 3.0
Sonoff ZBDongle-E (am Pi)
   │  zigbee2mqtt  ──►  Mosquitto (MQTT, localhost:1883)
   │                       │
pastadryer/                │ aiomqtt (Push-Cache + schalten)
   ├─ zb.py        ◄───────┘  Geräteschicht (MQTT / zigbee2mqtt)
   ├─ control.py   Regel-Engine (Ideallinie, Ruhephasen, Vorheizen, Not-Aus)
   ├─ history.py   SQLite-Verlauf
   ├─ programs.py  editierbare Programme (programs.json)
   ├─ config.py    Konfiguration (config.yaml)
   ├─ web.py       FastAPI + JSON-API
   └─ static/      Dashboard (HTML/CSS/JS, kein Build-Step)
run.py             Uvicorn-Entry

Zugriff:  Handy ──Tailscale──► pasta-pi:8000   (von überall, verschlüsselt)
```

> Geräte werden über ihre **zigbee2mqtt friendly_names** adressiert
> (z.B. `Sensor 1`, `Relais links` mit Kanal `state_l1` = Heizung, `state_l2` = Lüfter).
> Intern heissen die Felder weiter `aid`/`iid` (= Gerät/Property) – historisch.

## Hardware / Infrastruktur (auf dem Pi)

| Dienst | Zweck |
|---|---|
| `mosquitto` | MQTT-Broker (localhost:1883) |
| `zigbee2mqtt` | Zigbee-Koordinator → MQTT (Sonoff ZBDongle-E, `ember`-Adapter) |
| `pastadryer` | die App selbst (FastAPI, Port 8000) |
| `tailscaled` | Fernzugriff (Tailnet `gridonic.ch`, Pi = `pasta-pi`) |

Wichtig: Der Zigbee-Koordinator muss **nah an den Geräten** sein (≈≤10 m) – der Pi
steht deshalb am Trockner und hängt am Ethernet.

## Setup (Raspberry Pi)

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp config.example.yaml config.yaml      # Geräte-Namen/Programme anpassen
python run.py                           # Server (Port 8000)
```

Voraussetzung: Mosquitto + zigbee2mqtt laufen und alle Geräte sind in z2m angelernt
(friendly_names wie in `config.yaml`). Als Dienst:
`deploy/pastadryer.service` → `/etc/systemd/system/`, dann
`sudo systemctl enable --now pastadryer`.

> Bei SSH-Aufrufen `PYTHONUTF8=1` voranstellen, falls die Locale latin-1 ist.
> z2m mit `device_options: { retain: true }` betreiben → die App hat nach jedem
> Neustart sofort die letzten Werte.

## Zugriff

- **Zuhause:** `http://raspberrypi.local:8000`
- **Von überall:** Tailscale-App an → `http://pasta-pi:8000` (bzw. die 100.x-IP).
  Nur eigene Geräte im Tailnet kommen ran – das ersetzt einen Passwortschutz.

## Sicherheit

`max_temp` in `config.yaml` schaltet die Heizungen in **jedem** Modus hart ab, und
ein verriegelnder Not-Aus stoppt das Programm, wenn eine Heizung länger als
`heater_max_on` Minuten am Stück läuft (bleibt aus bis zum Quittieren im UI).

Das ist eine Komfort-Lösung, **kein** zertifizierter Übertemperaturschutz – ein
unabhängiger Hardware-Thermostat in Reihe zur Heizung bleibt Pflicht.

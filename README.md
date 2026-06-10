# 🍝 Pasta-Trockner

Lokale Steuerung eines Pasta-Trockners über einen **Aqara Hub M2** (HomeKit, rein
lokal – keine Cloud), gebaut in Python. Ersetzt die mühsamen Aqara-Automationen
durch eine eigene Regel-Engine + Browser-Dashboard.

## Was es kann

- **6 Temp/Feuchte-Sensoren** live + Verlaufs-Aufzeichnung (SQLite) mit Chart & CSV-Export
- **4 Relais-Kanäle** (Heizung links/rechts, Lüfter links/rechts)
- **Regelung:** Heizungen halten ein Temperatur-Band (z.B. 30–32 °C); Lüfter regeln
  auf eine Feuchte-Kurve, abwechselnd links/rechts im einstellbaren Takt
- **Trocken-Programme** (z.B. *maccheroni* 20-20-15 h, *caserecce* 10-10-5 h) –
  im Browser editierbar
- **Dashboard**: schwarzes, fluid-responsives Web-UI (Handy + Desktop)

## Architektur

```
Aqara M2 (HomeKit-Bridge, lokal)
   │  aiohomekit
pastadryer/
   ├─ hk.py        HomeKit-Layer (lesen/schalten)
   ├─ control.py   Regel-Engine (Thermostat + Hygrostat + Kurven)
   ├─ history.py   SQLite-Verlauf
   ├─ programs.py  editierbare Programme (programs.json)
   ├─ web.py       FastAPI + JSON-API
   ├─ static/      Dashboard (HTML/CSS/JS)
   └─ cli.py       Setup/Debug (discover, pair, dump, switch, sensors, monitor)
run.py             Uvicorn-Entry
```

## Setup (Raspberry Pi)

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp config.example.yaml config.yaml
python -m pastadryer.cli discover            # M2 finden
python -m pastadryer.cli pair                # mit Setup-Code pairen
python -m pastadryer.cli dump                # Geräte/Adressen auflisten
python run.py                                # Server (Port 8000)
```

Als Dienst: `deploy/pastadryer.service` → `/etc/systemd/system/`, dann
`sudo systemctl enable --now pastadryer`.

> Bei SSH-Aufrufen `PYTHONUTF8=1` voranstellen, falls die Locale latin-1 ist.

## Sicherheit

`max_temp` in `config.yaml` schaltet die Heizungen in **jedem** Modus hart ab.
Das ist eine Komfort-Lösung, **kein** zertifizierter Übertemperaturschutz – ein
unabhängiger Hardware-Thermostat in Reihe zur Heizung bleibt Pflicht.

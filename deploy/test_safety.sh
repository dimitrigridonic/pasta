#!/usr/bin/env bash
# Testet den Heizung-Dauerlauf-Wächter: setzt heater_max_on kurz auf ~4s,
# schaltet die linke Heizung an und prüft, ob verriegelt wird. Stellt danach wieder her.
set -e
cd ~/pasta
cp config.yaml config.yaml.bak

PYTHONUTF8=1 .venv/bin/python - <<'PY'
import yaml
d = yaml.safe_load(open("config.yaml"))
c = d.setdefault("control", {})
c["heater_max_on"] = 0.07     # ~4.2 s
c["poll_interval"] = 2
yaml.safe_dump(d, open("config.yaml", "w"), allow_unicode=True, sort_keys=False)
print("Test-Config: heater_max_on=0.07min, poll_interval=2s")
PY

sudo systemctl restart pastadryer
for i in $(seq 1 15); do curl -s localhost:8000/api/state -o /dev/null 2>/dev/null && break; sleep 1; done

curl -s -X POST localhost:8000/api/manual -H "Content-Type: application/json" \
  -d '{"aid":72,"iid":262147,"on":true}' -o /dev/null
echo ">> Heizung links manuell AN — beobachte Verriegelung:"

for i in $(seq 1 8); do
  sleep 2
  curl -s localhost:8000/api/state | PYTHONUTF8=1 .venv/bin/python -c "
import sys,json
d=json.load(sys.stdin)
hl=[h for h in d['heaters'] if h['aid']==72][0]['on']
print(f\"  t+{$i*2:>2}s  fault={d['fault']}  reason={d['fault_reason']}  HeizungLinks_an={hl}  mode={d['mode']}\")
"
done

echo ">> Test fertig, stelle Original-Config wieder her"
cp config.yaml.bak config.yaml && rm config.yaml.bak
sudo systemctl restart pastadryer
for i in $(seq 1 15); do curl -s localhost:8000/api/state -o /dev/null 2>/dev/null && break; sleep 1; done
echo ">> wiederhergestellt (fan_cycle_min/heater_max_on normal)"

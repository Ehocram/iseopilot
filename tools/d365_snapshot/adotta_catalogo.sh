#!/usr/bin/env bash
# Sostituisce il catalogo Dynamics di ISEOPilot con quello generato da
# d365_snapshot. Va eseguito SULLA MACCHINA CHE OSPITA IL CONTAINER.
#
#   ./adotta_catalogo.sh /percorso/di/d365_snapshot [nome_container]
#
# Ritorno indietro:
#   docker exec iseopilot cp /data/dynamics/catalog.json.bak \
#       /data/dynamics/catalog.json && docker restart iseopilot

set -euo pipefail

SNAP="${1:-}"
CONT="${2:-iseopilot}"

[ -n "$SNAP" ] || { echo "Uso: $0 <cartella-snapshot> [container]"; exit 1; }
[ -f "$SNAP/catalog_iseopilot.json" ] || {
    echo "Manca $SNAP/catalog_iseopilot.json"; exit 1; }
docker inspect "$CONT" >/dev/null 2>&1 || {
    echo "Container '$CONT' non trovato."; exit 1; }

echo "→ Catalogo da adottare:"
python3 - "$SNAP/catalog_iseopilot.json" <<'PY'
import json, sys
c = json.load(open(sys.argv[1]))
print(f"   versione {c['versione']} · {c['count']} entita' · "
      f"{c['relazioni']} archi dichiarati · "
      f"{c.get('relazioni_inferite_verificate', 0)} dedotti verificati · "
      f"{len(c.get('famiglie', []))} famiglie equivalenti")
PY

echo "→ Copia di sicurezza del catalogo in uso"
docker exec "$CONT" sh -c \
    '[ -f /data/dynamics/catalog.json ] &&
     cp /data/dynamics/catalog.json /data/dynamics/catalog.json.bak &&
     echo "   salvato in catalog.json.bak" ||
     echo "   nessun catalogo precedente"'

echo "→ Copia del nuovo catalogo"
docker cp "$SNAP/catalog_iseopilot.json" "$CONT:/data/dynamics/catalog.json"

if [ -d "$SNAP/schema" ]; then
    N=$(ls -1 "$SNAP/schema" | wc -l | tr -d ' ')
    echo "→ Copia di $N schede schema (puo' richiedere un minuto)"
    docker exec "$CONT" mkdir -p /data/dynamics/schema
    docker cp "$SNAP/schema/." "$CONT:/data/dynamics/schema/"
fi

echo "→ Riavvio (il catalogo viene letto e messo in cache all'avvio)"
docker restart "$CONT" >/dev/null

echo "→ Verifica"
sleep 5
docker exec "$CONT" python3 - <<'PY'
import json
try:
    c = json.load(open("/data/dynamics/catalog.json"))
    print(f"   catalogo attivo: versione {c.get('versione')} · "
          f"{c.get('count')} entita'")
except Exception as e:
    print(f"   ATTENZIONE: non leggibile ({e}). Valuta il ritorno indietro.")
PY
echo "Fatto."

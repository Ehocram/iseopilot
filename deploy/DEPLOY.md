# Deploy ISEOPilot sul server di Flusso-AI (10.1.10.139)

Procedura per affiancare ISEOPilot a Flusso-AI sullo stesso server, su
`https://iseopilot.iseo.com`, riusando Docker, il Caddy esistente e lo stesso
certificato PKI. Non modifica il compose di Flusso-AI; tocca solo il Caddyfile
(un blocco in più) e collega il Caddy a una rete dedicata.

Prerequisito: il record DNS interno `iseopilot.iseo.com → 10.1.10.139` esiste.

---

## 0) Ricognizione (scopri i nomi del tuo setup)

```bash
# nome del CONTAINER Caddy di Flusso-AI
sudo docker ps --format '{{.Names}}\t{{.Image}}' | grep -i caddy

# dove si trova il Caddyfile e i certificati (di solito sotto /opt/Flusso-AI)
sudo find /opt/Flusso-AI -iname 'Caddyfile' -o -iname '*.pem' 2>/dev/null
```

Annota: NOME_CADDY (es. `flusso-ai-caddy-1`) e il percorso del Caddyfile.
Guarda nel Caddyfile la riga `tls ...` del blocco `flussoai.iseo.com`: ti serve
identica per ISEOPilot.

---

## 1) Porta il codice sul server (git)

```bash
cd /opt
sudo git clone https://github.com/Ehocram/iseopilot.git
cd /opt/iseopilot
ls -la        # verifica: Dockerfile, docker-compose.prod.yml, app/ devono essere QUI
```

> Se nel repo i file applicativi stanno in una sottocartella (es. `iseo-chat-web/`)
> invece che nella radice, entraci: `cd /opt/iseopilot/iseo-chat-web`. Tutti i
> comandi successivi vanno eseguiti dalla cartella che contiene il `Dockerfile`.

---

## 2) Crea il file .env (segreti dell'app)

```bash
sudo tee /opt/iseopilot/.env >/dev/null <<EOF
APP_SECRET_KEY=$(python3 -c 'from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())')
APP_DATA_DIR=/data
SESSION_HTTPS_ONLY=1
BOOTSTRAP_ADMIN_USER=marco.bonometti
BOOTSTRAP_ADMIN_PASSWORD=CAMBIA-QUESTA-PASSWORD
BOOTSTRAP_ADMIN_DEPARTMENT=IT
EOF
sudo chmod 600 /opt/iseopilot/.env
```

> Cambia la password admin. Dopo il primo accesso, la chiave Claude si imposta
> dalla UI (Admin), non qui.

---

## 3) Crea la rete dedicata e collega il Caddy

```bash
# rete condivisa SOLO tra Caddy e ISEOPilot (isola ISEOPilot dalla rete dati di Flusso-AI)
sudo docker network create proxy-net 2>/dev/null || true

# collega il Caddy esistente a questa rete (sostituisci NOME_CADDY)
sudo docker network connect proxy-net NOME_CADDY
```

> Nota: se in futuro ricrei il container Caddy di Flusso-AI (es. con
> `--force-recreate`), ripeti SOLO l'ultimo comando per riconnetterlo.

---

## 4) Avvia ISEOPilot (build + up, solo il suo container)

```bash
cd /opt/iseopilot
sudo docker compose -f docker-compose.prod.yml up -d --build
```

Verifica che sia sano:

```bash
sudo docker compose -f docker-compose.prod.yml ps
sudo docker compose -f docker-compose.prod.yml logs --tail 40 iseopilot
```

---

## 5) Aggiungi il blocco al Caddyfile e ricarica

1. Apri `deploy/Caddyfile-iseopilot.txt`, copia il blocco, e incollalo in fondo
   al Caddyfile di Flusso-AI. Sostituisci la riga `tls ...` con quella IDENTICA
   del blocco `flussoai.iseo.com`.

2. Backup + validazione + reload (NON riavviare il container Caddy):

```bash
sudo cp /PERCORSO/Caddyfile /PERCORSO/Caddyfile.bak.$(date +%F)

# valida la sintassi PRIMA di ricaricare (evita di far cadere anche Flusso-AI)
sudo docker exec NOME_CADDY caddy validate --config /etc/caddy/Caddyfile

# ricarica a caldo (zero downtime per Flusso-AI)
sudo docker exec NOME_CADDY caddy reload --config /etc/caddy/Caddyfile
```

> Il percorso `/etc/caddy/Caddyfile` è quello tipico dentro il container; se nel
> tuo setup è diverso, usa quello giusto (lo vedi con
> `sudo docker exec NOME_CADDY ls -la /etc/caddy`).

---

## 6) Verifica finale

```bash
curl -k https://iseopilot.iseo.com/healthz      # deve rispondere ok
```

Poi apri `https://iseopilot.iseo.com` dal browser e fai il primo login con le
credenziali admin del .env. Flusso-AI resta su `https://flussoai.iseo.com`,
intatto.

---

## Aggiornamenti futuri di ISEOPilot (git)

```bash
cd /opt/iseopilot
sudo git fetch --depth 1 origin main && sudo git reset --hard origin/main
sudo docker compose -f docker-compose.prod.yml up -d --build iseopilot
```

> Sostituisci `main` con il nome del tuo branch se diverso. Agisce solo sul
> container `iseopilot`. Non usare `--force-recreate` qui: non serve, e su un
> compose condiviso rischierebbe di toccare altri servizi.

## Rollback

```bash
cd /opt/iseopilot
sudo docker compose -f docker-compose.prod.yml down
# e rimuovi il blocco iseopilot dal Caddyfile, poi caddy reload
```

I dati (DB, token, catalogo) restano nel volume `iseopilot_data` finché non lo
rimuovi esplicitamente.

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

---

## Presidio (sidecar `iseopilot-presidio`)

Container separato che riconosce, nel testo che la modalità libera manda a
Claude, i nomi di persona (modello spaCy `it_core_news_md` 3.8.0, licenza
CC BY-NC-SA accettata dal CISO per uso interno), i telefoni e i documenti
d'identità italiani. Restituisce solo intervalli: dizionario e segnaposto
restano in `iseopilot`. Nessuno stato, nessuna porta pubblicata, rete
`presidio-net` con `internal: true` (niente uscita su internet), nessun `.env`
dentro il container: riceve solo `PRESIDIO_TOKEN`. Tetto 1 GB di RAM senza
swap e 1 CPU.

**Se Presidio non risponde la chat NON si ferma.** Se il servizio non è
configurato, è giù, va in errore o in timeout, `iseopilot` sostituisce comunque
i nomi del dizionario e i dati tecnici e mostra in chat la nota
«⚠️ Riconoscimento automatico dei dati personali (Presidio) non disponibile».

**Regole:** sempre il nome del servizio nei comandi compose. Mai `up -d` senza
servizio, `down`, `--force-recreate`, `--remove-orphans`: toccherebbero anche il
dubworker. `iseopilot` non ha `depends_on` su `presidio`, quindi
`up -d iseopilot` non costruisce né avvia Presidio per caso.

### P1) Token e indirizzo nel .env (PRIMA del git reset)

Il token si genera sul server e non passa da git né dalla chat (lo genera la
shell di root dentro `sudo sh -c`, quindi non compare nell'argv di sudo né nel
suo log). Prima una copia del `.env` e l'a capo finale, se manca: senza, la
riga nuova si attaccherebbe all'ultima variabile e la rovinerebbe. Il comando
aggiunge le due righe solo se mancano:

```bash
cd /opt/iseopilot
sudo cp -p .env /root/iseopilot.env.bak-presidio                     # fuori dal repo: git status resta vuoto
sudo sh -c '[ -z "$(tail -c1 /opt/iseopilot/.env)" ] || echo >> /opt/iseopilot/.env'
sudo grep -q '^PRESIDIO_TOKEN=' .env || sudo sh -c 'printf "PRESIDIO_TOKEN=%s\n" "$(openssl rand -hex 32)" >> /opt/iseopilot/.env'
sudo grep -q '^PRESIDIO_URL=' .env   || sudo sh -c 'printf "PRESIDIO_URL=http://iseopilot-presidio:3000\n" >> /opt/iseopilot/.env'
sudo stat -c '%a %U' .env                                            # atteso: 600 root
sudo grep -c '^PRESIDIO_' .env                                       # atteso: 2 (non stampare i valori)
diff <(sudo cut -d= -f1 /root/iseopilot.env.bak-presidio) <(sudo cut -d= -f1 .env)   # atteso: solo «> PRESIDIO_TOKEN» e «> PRESIDIO_URL»
```

> Compose legge `PRESIDIO_TOKEN` dal `.env` per il servizio `presidio`
> (`${PRESIDIO_TOKEN:-}`); `iseopilot` riceve entrambe le variabili con il suo
> `env_file`. Un token assente o più corto di 32 caratteri lascia Presidio
> «non pronto» (`/ready` 503): la chat funziona, con l'avviso.

### P2) Codice e build del sidecar

```bash
cd /opt/iseopilot
sudo git status --porcelain                                          # deve essere vuoto
df -h / /var/lib/docker                                              # almeno 5 GB liberi dove scrive il build
TAG=prima-presidio-$(date +%Y%m%d-%H%M); sudo docker tag iseopilot:latest iseopilot:$TAG; echo $TAG
sudo git fetch --depth 1 origin main && sudo git reset --hard origin/main
sudo docker compose -f docker-compose.prod.yml build presidio 2>&1 | tee /tmp/build-presidio.log
grep -n "it_core_news_md 3.8.0" /tmp/build-presidio.log             # la prova di caricamento del modello è passata
```

> Il build installa `requirements-presidio.txt` con `--require-hashes`:
> versioni e hash fissati per ogni pacchetto e per il wheel del modello. Se un
> hash non torna il build si ferma; il container in esercizio non viene toccato.

### P3) Test dentro l'immagine, senza rete, con gli stessi limiti della produzione

```bash
sudo docker run --rm --network none --read-only --tmpfs /tmp --cap-drop ALL \
     --security-opt no-new-privileges:true --memory 1g --memory-swap 1g --cpus 1 \
     iseopilot-presidio:latest python -m presidio_svc.tests.test_riconoscitori --misure
```

Atteso: «27/27 test superati» (persone, telefoni, documenti solo con
contesto, allow-list, token, limiti, timeout delle regex, scadenza della
richiesta, vocabolario che non cresce, nessun accesso alla rete), poi le
misure: avvio, RSS, latenza su 2, 30 e 200 KB. Riferimento sul Mac: RSS circa
400-450 MB, 2 KB 40 ms, 30 KB 0,6 s, 200 KB 4-4,7 s. Sul 139 con 1 CPU i tempi
saranno più alti: se 200 KB supera 13 s (il timeout del client per quella
dimensione) quei testi andranno con l'avviso ⚠️; annotare il valore. **Se un
test fallisce non si va avanti.**

### P4) Avvio del sidecar

```bash
sudo docker compose -f docker-compose.prod.yml up -d presidio
sudo docker inspect -f '{{.State.Health.Status}}' iseopilot-presidio          # ripetere finché «healthy» (start_period 90 s)
sudo docker logs --tail 20 iseopilot-presidio                                  # «self-test: ok», poi solo contatori
cat /sys/fs/cgroup/system.slice/docker-$(sudo docker inspect -f '{{.Id}}' iseopilot-presidio).scope/memory.swap.max   # atteso 0
sudo docker exec iseopilot-presidio python -c "import socket; socket.create_connection(('pypi.org', 443), 3)" \
  && echo "ATTENZIONE: il sidecar esce su internet" || echo "ok: nessuna uscita"
# Anche il DNS: i Docker senza la correzione di CVE-2024-29018 (prima di 26.0.0,
# 25.0.5, 23.0.11) inoltrano le query DNS esterne anche dalle reti «internal».
sudo docker version --format '{{.Server.Version}}'                            # atteso: >= 26.0.0 (o 25.0.5 / 23.0.11)
sudo docker exec iseopilot-presidio python -c "import socket; socket.getaddrinfo('pypi.org', 443)" \
  && echo "ATTENZIONE: DNS esterno risolto" || echo "ok: DNS esterno bloccato"
```

### P5) iseopilot su presidio-net

`iseopilot` si ricostruisce con la procedura di sempre (solo codice: il
livello pip deve risultare CACHED e `pip list` identico prima e dopo). L'`up`
lo ricrea collegato anche a `presidio-net`: 10-40 s di fermo della chat.

```bash
cd /opt/iseopilot
sudo docker exec iseopilot pip list --format=freeze > /tmp/pip-prima.txt
sudo docker compose -f docker-compose.prod.yml build iseopilot 2>&1 | tee /tmp/build.log
grep -n "CACHED" /tmp/build.log                                      # i passi apt e pip devono essere CACHED
sudo docker run --rm --entrypoint pip iseopilot:latest list --format=freeze > /tmp/pip-dopo.txt
diff /tmp/pip-prima.txt /tmp/pip-dopo.txt                            # atteso: nessuna differenza
sudo docker compose -f docker-compose.prod.yml up -d iseopilot
sudo docker exec iseopilot python -c "import urllib.request; print(urllib.request.urlopen('http://iseopilot-presidio:3000/ready', timeout=5).read().decode())"
sudo docker stats --no-stream iseopilot iseopilot-presidio
```

> Se il passo pip non è CACHED o il diff non è vuoto, **non fare l'`up`**:
> `sudo docker tag iseopilot:$TAG iseopilot:latest` e si ricontrolla dal Mac.
> Presidio può restare acceso: senza `iseopilot` nuovo nessuno lo chiama.

### P6) Verifica in chat (CISO)

1. In modalità libera scrivere una frase con un nome che **non** è nel
   dizionario e un cellulare («Mario Rossi, 333 1234567»): la nota 🔒 conta i
   dati sostituiti e non compare la nota ⚠️.
2. `sudo docker stop iseopilot-presidio`, poi un altro messaggio: la risposta
   arriva lo stesso, con la nota ⚠️ «Riconoscimento automatico dei dati
   personali (Presidio) non disponibile…».
3. `sudo docker start iseopilot-presidio`; quando è `healthy` la nota ⚠️ sparisce.
4. `sudo docker logs --tail 50 iseopilot-presidio`: solo righe
   `analyze segmenti=… caratteri=… ms=… esito=…`, mai testo.

### Cambio del token

```bash
cd /opt/iseopilot
sudo sh -c 'T=$(openssl rand -hex 32); sed -i "s/^PRESIDIO_TOKEN=.*/PRESIDIO_TOKEN=$T/" /opt/iseopilot/.env'
sudo docker compose -f docker-compose.prod.yml up -d presidio        # si ricrea con il token nuovo
sudo docker compose -f docker-compose.prod.yml up -d iseopilot       # rilegge il .env (10-40 s di fermo)
```

Fra i due comandi le richieste a Presidio ricevono 403: la chat procede con
dizionario e regole e mostra l'avviso.

### Rollback di Presidio

Non c'è fail-closed, quindi per «spegnere» Presidio basta fermarlo: la chat
procede con dizionario e regole e mostra l'avviso.

```bash
sudo docker compose -f docker-compose.prod.yml stop presidio
```

Rollback completo, in quest'ordine:

```bash
cd /opt/iseopilot
sudo docker tag iseopilot:prima-presidio-<data> iseopilot:latest
sudo docker compose -f docker-compose.prod.yml up -d --no-build iseopilot
sudo docker compose -f docker-compose.prod.yml stop presidio          # finché il compose contiene ancora il servizio
sudo git reset --hard <commit precedente>
sudo docker rm iseopilot-presidio
```

> La rete `iseopilot_presidio-net` resta (vuota di servizi, senza uscita) e
> `iseopilot` vi resta collegato finché non viene ricreato: compose non la
> toglie da solo. Per rimuoverla, dopo il `git reset`:
> `sudo docker compose -f docker-compose.prod.yml up -d --no-build iseopilot`
> (10-40 s di fermo) e poi `sudo docker network rm iseopilot_presidio-net`. Le righe `PRESIDIO_*` nel `.env` si possono lasciare: senza il
> servizio non fanno nulla. **Non usare il «Rollback» generale qui sopra**
> (`compose down`): fermerebbe anche il dubworker.

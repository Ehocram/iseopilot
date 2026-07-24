# ISEOPilot

Assistente AI interno multi-utente per ISEO Serrature. Porting web dell'app
desktop "Chat Assistant" (PyQt6). Motore **Claude** (Anthropic) o **modello
locale LM Studio**. Server-side: FastAPI + Jinja2 + JS vanilla + SSE.
**Nessuna dipendenza da CDN esterni** (scelta di sicurezza).

> Stato: **Incremento 7** completato (feedback Carlos: recall, leggibilità, template personali). Vedi *Roadmap* in fondo.

---

## Cosa fa (oggi)

- **Chat in streaming** con Claude o LM Studio locale, con tono e lingua selezionabili.
- **Autenticazione locale gestita dall'admin**: gli account li crei e gestisci tu.
  Ogni utente appartiene a un **dipartimento/area**.
- **Anonimizzazione** dei dati tecnici sensibili (IP, email, codici fiscali,
  P.IVA, hash, token + dizionario nomi) **prima** dell'invio a Claude, con
  ripristino nella risposta. Il modello locale riceve il testo in chiaro (resta in casa).
- **Pannello admin**: chiave Claude + modello, endpoint LM Studio, dizionario
  anonimizzazione, gestione **utenti** e **dipartimenti**. Segreti **cifrati a riposo**.
- **Connessioni personali** per utente (OneDrive/Dynamics): preferenze salvate,
  token isolato per identità (collegamento effettivo: Incremento 3).

### Perché l'autenticazione (e non solo il proxy)
Con dati per-utente (chat) e soprattutto la **conoscenza progressiva** in
ChromaDB sul server, senza login la conoscenza diventerebbe di tutta l'azienda.
L'app ora autentica e associa ogni utente a un **dipartimento**, che è il
compartimento di conoscenza isolato (vedi sotto).

---

## Dipartimenti e conoscenza (ChromaDB)

Ogni dipartimento corrisponde a una **collezione ChromaDB isolata**:

| Dipartimento  | Collezione         |
|---------------|--------------------|
| IT            | `kb_it`            |
| Infosec       | `kb_infosec`       |
| ESG           | `kb_esg`           |
| Privacy       | `kb_privacy`       |
| Sales         | `kb_sales`         |
| Operations    | `kb_operations`    |
| Finance       | `kb_finance`       |
| HR            | `kb_hr`            |
| Supply Chain  | `kb_supply_chain`  |
| R&D           | `kb_r_d`           |

I 10 dipartimenti sono creati al primo avvio. Dalla pagina **Dipartimenti**
puoi crearne altri (es. Legal, Quality…): diventano subito assegnabili.
Il **wiring effettivo** di ChromaDB e la schermata di upload file/cartelle
(con container = dipartimento dell'utente) arrivano nell'Incremento 3.

---

## Avvio in locale (sviluppo)

```bash
cd iseo-chat-web
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Chiave di cifratura + firma sessioni (genera UNA volta)
export APP_SECRET_KEY="$(python -c 'from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())')"
export APP_DATA_DIR="./data"

# Admin iniziale (creato solo al primo avvio, se non ci sono utenti)
export BOOTSTRAP_ADMIN_USER="marco.bonometti"
export BOOTSTRAP_ADMIN_PASSWORD="scegli-una-password-robusta"
export BOOTSTRAP_ADMIN_DEPARTMENT="IT"

uvicorn app.main:app --reload --port 8000
```

Apri http://127.0.0.1:8000 → verrai rediretto al login. Entra con l'admin di
bootstrap, poi crea gli utenti dalla pagina **Utenti**.

> Dopo il primo accesso: cambia la password dalla pagina Utenti e **rimuovi**
> le variabili `BOOTSTRAP_ADMIN_*`.

---

## Avvio con Docker (server srv-hq-ai-01)

```bash
cp .env.example .env        # compila APP_SECRET_KEY e BOOTSTRAP_ADMIN_*
sudo docker compose up -d --build
sudo docker compose logs -f web
```

L'app resta in ascolto su `127.0.0.1:8000` (vedi `docker-compose.yml`).
Mettere **sempre** un reverse proxy con TLS davanti.

> Ricorda (note operative ISEO): usare `sudo` per git/docker; per ricaricare il
> codice Python dopo un aggiornamento, `sudo docker compose up -d --force-recreate`.

---

## Sicurezza (sintesi per CISO)

- **Login applicativo**: password con hash **scrypt** (memory-hard, libreria
  standard — niente dipendenze), confronto a tempo costante. Le password **non**
  sono mai in chiaro su disco.
- **Sessioni**: cookie **firmato** (itsdangerous), `HttpOnly`, `SameSite=Lax`,
  scadenza 8h. In produzione impostare `SESSION_HTTPS_ONLY=1` → cookie `Secure`.
  **L'app tratta credenziali: va esposta solo via HTTPS.**
- **Revoca immediata**: disattivare un utente invalida subito la sua sessione.
- **Almeno un admin attivo**: l'app impedisce di rimuovere/disattivare l'ultimo amministratore.
- **Segreti cifrati a riposo** (Fernet) con `APP_SECRET_KEY`. Se assente, l'app
  **non parte** (fail loudly).
- **Chiave Claude**: inserita dal pannello admin, cifrata. La chiave hardcoded
  della versione desktop è da considerarsi **compromessa → ruotarla**.
- **Anonimizzazione** isolata per richiesta (nessuna contaminazione tra utenti).
- **Container non-root** (uid 10001), bind loopback, healthcheck.
- **Isolamento conoscenza**: collezione ChromaDB per dipartimento (Inc.3 per il wiring).
- **Chat**: attualmente vivono solo nel browser (non salvate sul server) → già
  private. La cronologia persistente, quando aggiunta, sarà per-utente.

---

## Struttura

```
app/
  main.py            FastAPI: login, chat (SSE), admin, utenti, dipartimenti
  auth.py            hash scrypt, identità da sessione, bootstrap admin
  store.py           SQLite + Fernet: settings, utenti, dipartimenti, token path
  anonymizer.py      mascheramento/ripristino dati sensibili (per-richiesta)
  orchestrator.py    streaming Claude / LM Studio, toni, lingue
  engines/           connettori desktop (ChromaDB, OneDrive, Dynamics) — Inc.3
templates/           base, login, chat, admin, admin_users, admin_departments, user
static/              app.css (design dark "Secure Access Console"), chat.js
tests/test_smoke.py  14 test (auth, sessioni, utenti, dipartimenti, anonimizzazione)
```

## Test

```bash
PYTHONPATH=. APP_DATA_DIR=./data_test \
APP_SECRET_KEY="$(python -c 'from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())')" \
python tests/test_smoke.py
```

---

## Roadmap

- **Incremento 1** ✓ — Nucleo chat Claude/LM, anonimizzazione, pannello admin, Docker.
- **Incremento 2** ✓ — Login locale gestito dall'admin, utenti, dipartimenti, UI dark.
- **Incremento 3** ✓ — **Livello conoscenza**: upload documenti ChromaDB per
  dipartimento + ricerca cartelle locali/di rete (FTS5), scoping per area, RAG.
- **Incremento 4** ✓ — **Controllo fonti + lingue**:
  - **Toggle ON/OFF** per ogni fonte (conoscenza dipartimento, cartelle,
    OneDrive, Dynamics), per-utente, cablati nel recupero (pagina *Connessioni*);
  - **Browser del filesystem del server** e **più cartelle per dipartimento**
    selezionabili dall'admin (pagina *Dipartimenti* → *Sfoglia / aggiungi*);
  - **Configurazione OneDrive/Dynamics auto-compilata** con i valori ISEO
    (client/tenant/URL), modificabile dall'admin (pagina *Motore*);
  - **Interfaccia bilingue IT/EN** con selettore in topbar e login; la lingua è
    una preferenza per-utente, indipendente dalla lingua di risposta dell'AI;
  - **Logo ISEO ufficiale**.
- **Incremento 5** ✓ (codice) — **Sign-in OneDrive/Dynamics per-utente** (device
  code Microsoft): l'utente autorizza col proprio account dalla pagina
  *Connessioni* ("Connetti il mio account" → codice + link → polling automatico),
  token isolato per identità sul server (`user_token_path`), ricerca cablata nei
  toggle. Flusso non bloccante (start + poll separati). **Da validare sul tenant
  Azure**: in sandbox il round-trip Microsoft non è raggiungibile; la logica, gli
  endpoint, l'isolamento del token e il degrado pulito sono testati.
- **Incremento 6** ✓ (codice) — **Fonte Power BI per-utente** (quinta fonte,
  connettore separato da OneDrive e Dynamics). **Disattivato di default**:
  kill-switch admin in *Motore* ("Abilita il connettore Power BI"); da spento la
  superficie utente è zero (niente pannello in Connessioni, niente pill in chat,
  rotte rifiutate) e la piattaforma si comporta come prima dell'incremento.
  Da acceso: sign-in device code dedicato con
  scope `https://analysis.windows.net/powerbi/api/.default` (token proprio,
  audience distinta), toggle `use_powerbi` e pill "Power BI" in chat. Ogni query
  gira con l'identità dell'utente: valgono i suoi workspace, i permessi
  Lettura+**Build** sul dataset e la sua Row-Level Security. **Catalogo
  PER-UTENTE** (workspace/dataset/tabelle/colonne via `EVALUATE
  COLUMNSTATISTICS()` sull'endpoint JSON `executeQueries`; misure via endpoint
  Arrow `executeDaxQueries`+INFO solo su capacità dedicata e con `pyarrow`,
  altrimenti nota esplicita), generato in **background** dalla pagina
  Connessioni con polling di stato. Planner DAX agentico bounded (max 6 passi,
  solo `EVALUATE`, tetto 50 righe mostrate), FONTI con link ad app.powerbi.com,
  diagnostica admin dedicata (token → audience → workspace → catalogo → query di
  prova). Prerequisiti tenant: permessi delegati Power BI Service
  (Dataset.Read.All, Workspace.Read.All, Report.Read.All) con admin consent
  sull'app registration esistente + tenant setting **"Dataset Execute Queries
  REST API"** (Integration settings). **Da validare sul tenant**: in sandbox il
  round-trip Microsoft non è raggiungibile; logica, endpoint, isolamento
  per-utente e messaggi d'errore sono coperti dai test (`tests/test_powerbi.py`).
- **Incremento 7** ✓ — **Feedback Carlos** (tre interventi):
  (1) *Recall Conoscenza*: più documenti per argomento (tetto per-nome 8→12,
  budget 5800→9000, semantico 12 chunk con max 2 per fonte; budget contesto in
  chat 8000→12000) e **INVENTARIO di copertura** — sulle domande di
  enumerazione/copertura IT+EN ("tutto quello che abbiamo su…", "do we have…",
  "list all…") il modello riceve l'elenco dei NOMI documento del dipartimento
  con conteggio dichiarato (completo/troncato), anche quando nessun estratto è
  pertinente: mai un muto "nessun risultato" su una domanda di copertura.
  (2) *Leggibilità a schermo*: renderer Markdown completo e SICURO in chat
  (escape-first, blocchi codice estratti a monte con pulsante copia, titoli,
  liste, tabelle, citazioni, link solo http/https) + tipografia delle bolle
  (interlinea, spaziature, tabelle con bordi, h1–h3). Verificato con harness
  Node (16 casi, inclusi vettori XSS) in fase di build.
  (3) *Template personali Word/PowerPoint*: pulsante 📐 nel composer — l'utente
  carica un proprio `.docx`/`.pptx` **o modello `.dotx`/`.potx`** (normalizzato
  al salvataggio: stesso package, content-type riscritto) che, finché presente
  (chip visibile con ✕), **bypassa il template ISEO** nella generazione
  (il PDF usa il template Word personale). Validazione: solo `.docx`/`.pptx`,
  **niente macro** (`.docm`/`.pptm`, `vbaProject`, content-type macroEnabled
  rifiutati con motivo), max 15 MB, storage isolato per identità, audit
  (`template_caricato`/`template_rifiutato`/`template_rimosso`). Un template
  corrotto interrompe la generazione con errore parlante: nessun ripiego
  silenzioso sul default. Sui template personali si parla la lingua del file:
  Title/Subtitle/**Heading 1**/List Paragraph se definiti e il **numbering
  bullet del template stesso** (numId reale rilevato da `numbering.xml`, con
  glifo • come ripiego dichiarato); in PowerPoint si usano tema e layout del file.
- **Successivi** — Cronologia chat persistente per-utente; account Dynamics 365
  in sola lettura per-utente (migrazione dal System Administrator); re-rank semantico.

---

## Allegati e generazione documenti

**Allegati (fino a 20, drag&drop).** Nella chat, con la graffetta o trascinando i file, si allegano documenti (txt, md, csv, json, xml, py, pdf, docx, xlsx, pptx). Il testo viene estratto e usato con priorità nel contesto, **sia in Documentale sia in AI libera** (es. "fammi la sintesi del file allegato").

**Generazione file scaricabili.** Chiedendo in chat "creami un Word/Excel/PowerPoint/PDF…", ISEOPilot rileva il formato, fa generare il contenuto al modello e costruisce il file, offrendolo in **download** sotto la risposta. Word e PowerPoint usano i **template aziendali** (`app/doc_templates/`); Excel è generato da zero con intestazioni e formule di totale. Il PDF è prodotto dal Word sul template via LibreOffice (incluso nell'immagine Docker); in mancanza di LibreOffice ricade su un layout in stile ISEO. I file sono serviti via token legato all'utente (download riservato al proprietario).

> Nota: la generazione del contenuto usa Claude (chiamata non in streaming), quindi richiede la chiave API configurata, anche se il motore di chat è LM Studio.

---

## Cronologia conversazioni e feedback

Ogni conversazione viene **salvata automaticamente** dopo ogni risposta, per-utente, nella barra laterale della chat: si può riaprire, rinominare, eliminare e iniziare una **Nuova chat**. Titoli e contenuti sono **cifrati a riposo** (Fernet), come i segreti.

Le ultime conversazioni alimentano la **memoria**: vengono riassunte e passate al modello come contesto di continuità tra sessioni. Inoltre ogni risposta dell'assistente ha **pollice su / pollice giù**: il pollice su promuove la coppia domanda/risposta a *esempio eccellente*, che guida qualità e stile delle risposte successive (few-shot). Il pollice giù è una segnalazione e non genera apprendimento negativo. Memoria ed esempi sono **scoping per-utente**.

---

## Modalità risposta: Documentale / AI libera

Nella chat, accanto al motore, c'è il selettore **Modalità** (porting del pulsante "🤖 AI" desktop):

- **Documentale** (predefinita): l'assistente cerca nelle fonti attive (conoscenza del dipartimento, cartelle, OneDrive/Dynamics se connessi) e risponde basandosi su quanto recuperato.
- **AI libera**: nessuna ricerca nelle fonti aziendali; l'assistente risponde dalla propria conoscenza generale. L'anonimizzazione (per Claude) resta attiva.

La barra di stato in alto mostra la modalità corrente.

---

## Connettori personali Microsoft (Incrementi 5–6)

L'amministratore imposta la configurazione (auto-compilata con i valori ISEO) in
*Motore*: client ID, tenant ID e URL della risorsa Dynamics. Non sono segreti.

Ogni utente collega il **proprio** account in *Connessioni*:
1. preme "Connetti il mio account" → il server avvia il device flow Microsoft;
2. apre il link mostrato e inserisce il codice;
3. il browser interroga automaticamente il server finché l'autorizzazione non è
   completata; il token viene salvato isolato per identità.

A quel punto il toggle del connettore diventa attivabile.

**Power BI (Incremento 6).** Il connettore compare solo se l'amministratore lo
ha abilitato in *Motore* (kill-switch, spento di default). Stessa procedura,
pannello dedicato: dopo la
connessione l'utente genera il **proprio catalogo** ("Genera catalogo": elenco
dei workspace e dataset che la sua utenza può interrogare, con tabelle, colonne
e — dove la capacità lo consente — misure). Le domande con fonte *Power BI*
vengono tradotte dal planner in query DAX di sola lettura eseguite con il token
dell'utente: Row-Level Security e permessi (serve **Build** sul dataset) si
applicano per costruzione. Errori di accesso o configurazione (Build mancante,
tenant setting "Dataset Execute Queries REST API" spento, token scaduto)
producono messaggi espliciti in chat e nella diagnostica admin.

**Mappa relazioni Dynamics (catalogo + schema .md).** Per query OData affidabili, l'admin genera una volta il *catalogo entità* dalla pagina *Motore* (scarica il `$metadata` dell'istanza: tutte le entità F&O, i campi e il grafo delle relazioni con navigation property + referential constraint) e i relativi file schema `.md` per entità, che alimentano il planner. Catalogo e schema vivono sotto il volume dati (`<APP_DATA_DIR>/dynamics/`), condivisi da tutti gli utenti, e si rigenerano con un click. Con il toggle acceso,
le query in chat vengono arricchite con OneDrive (Graph Search) e/o Dynamics 365
(OData, sola lettura), riusando i moduli di ricerca dell'app desktop. Il token
non è mai condiviso fra utenti; il refresh è automatico alla scadenza.

---

## Conoscenza per dipartimento (Incremento 3)

Due meccanismi distinti, entrambi isolati per area e agganciati alla chat:

1. **Upload in ChromaDB** (pagina *Conoscenza*). L'utente carica file (o un'intera
   cartella di file dal browser) nella collezione del proprio dipartimento
   (`kb_<area>`). I documenti vengono spezzati e indicizzati; le query in chat
   pescano solo dalla collezione dell'area dell'utente. Infrastruttura ChromaDB
   (modello embedding, locale o istanza remota centralizzata) impostata dall'admin
   nella pagina *Motore*.

2. **Ricerca cartelle** locali o di rete (pagina *Conoscenza* → cartella del
   dipartimento). L'admin assegna a ciascuna area un percorso (locale al server o
   share di rete raggiungibile) nella pagina *Dipartimenti*. Il server indicizza
   la cartella con FTS5 (indice incrementale: ri-legge solo i file nuovi o
   modificati) e cerca sul posto — **i documenti non si caricano, restano dove
   sono**. L'indice eredita la riservatezza della sorgente.

Le cartelle vengono **indicizzate automaticamente** all'avvio del server e appena agganciate (in background, anche le share di rete), come l'app desktop; il pulsante *Aggiorna indice* resta per i refresh manuali. Il contesto recuperato (KB + cartella) viene iniettato nel *system prompt*; con
Claude attraversa anche l'anonimizzazione, coerentemente con il modello privacy.

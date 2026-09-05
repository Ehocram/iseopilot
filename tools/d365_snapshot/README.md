# d365_snapshot — fotografia dello schema Dynamics 365 F&O

Estrae da `isd365-prod` un ritratto completo e **misurato sul dato reale** dello
schema gestionale, in un formato che ISEOPilot può leggere: entità, campi con
etichette italiane, chiavi, enumerazioni, relazioni (dichiarate *e* inferite),
volumi di righe, percentuali di riempimento e verifica dei join.

Solo libreria standard di Python: nessun `pip install`.

## Cosa risolve

L'API OData espone il `$metadata` EDMX, che elenca entità e campi ma non dice
**quali entità ISEO usa davvero**, non traduce le etichette, non elenca i valori
degli enum e lascia isolate le entità senza navigation property dichiarata.
Il catalogo attualmente in uso da IseoPilot
(`~/.chat_assistant_dyn_catalog.json`) si ferma a quel livello: 4.704 entità,
solo campi `string`/`date` e le relazioni dichiarate.

Questo strumento aggiunge:

| | catalogo attuale | questo snapshot |
|---|---|---|
| Entità | 4.704 | 4.707 + 1.294 solo-DMF censite |
| Tipi di campo | string, date | tutti (numerici, enum, dimensioni) |
| Chiavi primarie | no | sì |
| Etichette in italiano | no | sì |
| Enumerazioni | no | 2.320 con valori e significato |
| Relazioni | 7.646 dichiarate | dichiarate + inferite + **verificate sui dati** |
| Volumi reali | no | conteggio righe per entità |
| Riempimento campi | no | % per campo su campione |
| Società coinvolte | no | sì, per entità |

## Uso

```bash
cd ~/IseoPilot/tools
python3 -m d365_snapshot login        # una volta: device code
python3 -m d365_snapshot whoami       # verifica accesso
```

### Copertura completa (impostazione predefinita)

La ricerca di ISEOPilot deve poter arrivare ovunque, quindi **ogni fase copre
per default l'intero ERP**: tutte le entità, tutte le società, nessun tetto.

```bash
python3 -m d365_snapshot harvest                     # 4.707 entità + etichette IT
python3 -m d365_snapshot profile --verify-relations  # conteggi, campi, join
python3 -m d365_snapshot build
```

Oppure in un colpo solo:

```bash
python3 -m d365_snapshot all --verify-relations
```

Tempi sull'ambiente ISEO — le prime due misurate, le altre stimate:

| Fase | Durata | Note |
|---|---|---|
| Metadati strutturali | ~1 min | `/metadata/PublicEntities` restituisce già l'entità completa nella risposta di lista: 4.707 entità, 86.597 campi, 7.646 relazioni in una sola paginazione. Nessuna GET per entità. |
| Etichette italiane | ~40-60 min | 29.611 etichette distinte, una chiamata ciascuna: è l'unico collo di bottiglia, l'API non accetta filtri. Cache definitiva. |
| Conteggi righe | 1-2 h | tocca il piano dati, tenuto a bassa concorrenza |
| Profilo campi + verifica join | 1-3 h | |

Conviene lanciare le ultime due fasi fuori orario.

Ogni fase è **ripartibile**: la cache è su disco, un'interruzione non fa perdere
lavoro e basta rilanciare lo stesso comando.

### Varianti per andare più veloce

Utili per un primo giro o per una verifica mirata, **al costo della copertura**:

```bash
# solo struttura, etichette rimandate
python3 -m d365_snapshot harvest --no-labels

# etichette solo per ciò che risulta popolato (richiede un profile --counts-only)
python3 -m d365_snapshot harvest --labels-for-populated

# un solo dominio
python3 -m d365_snapshot profile --domains Acquisti Magazzino

# una sola società: le entità globali restano contate per intero
python3 -m d365_snapshot --company IT1 profile --counts-only

# tetti espliciti (0 = nessun tetto, default)
python3 -m d365_snapshot profile --max-entities 400 --max-checks 300
```

Le misure di società diverse non si sovrascrivono: finiscono in
`counts_IT1.json`, `field_profile_IT1.json` e così via.

## Cosa produce

In `~/IseoPilot/data/d365_snapshot/`:

- `REPORT.md` — la fotografia leggibile: volumi, copertura per dominio, le 60
  entità più voluminose, relazioni confermate e relazioni sospette, e una
  sezione **Copertura** che dichiara esplicitamente cosa è ricercabile e quali
  lacune restano (entità non esposte su OData, entità solo-DMF, dettagli non
  recuperati). Le lacune vanno lette: sono i punti ciechi di ISEOPilot.
- `snapshot.sqlite` — tabelle `entita`, `campo`, `relazione`, `enum_valore`,
  `societa`. Interrogabile:
  ```bash
  python3 -m d365_snapshot query \
    "SELECT entity_set, etichetta, righe FROM entita WHERE dominio='Acquisti' AND popolata=1 ORDER BY righe DESC"
  ```
- `schema/<Entita>.md` — una scheda per entità, pensata per essere indicizzata
  da un RAG: etichette, chiavi, riempimento, relazioni verificate, esempio di
  query OData già pronto.
- `erd/*.mmd` — diagrammi Mermaid per dominio, più `_trasversale.mmd`.
- `catalog_iseopilot.json` — stesso formato di `~/.chat_assistant_dyn_catalog.json`,
  retrocompatibile, arricchito. Per adottarlo:
  ```bash
  cp ~/IseoPilot/data/d365_snapshot/catalog_iseopilot.json \
     ~/IseoPilot/data/dynamics/catalog.json
  cp ~/IseoPilot/data/d365_snapshot/schema/*.md ~/IseoPilot/data/dynamics/schema/
  ```
  (`app/connectors.py` punta già `dynamics_search.CATALOG_FILE` e `SCHEMA_DIR`
  in quella cartella.)

## Prudenza verso la produzione

Tutte le chiamate sono **GET**. Nessuna scrittura, in nessuna fase.
La concorrenza è volutamente bassa (6 in raccolta, 3 in profilazione), con
backoff esponenziale e rispetto di `Retry-After`. Il download dei dettagli entità fa **due giri**: i fallimenti del primo sono
quasi sempre throttling transitorio, il secondo li recupera e ciò che resta
finisce in `raw/entities_failed.json` e nella sezione Copertura del report.

La fase `profile` è la più pesante: conviene lanciarla fuori orario. Regolabile:

```bash
D365_PROFILE_CONCURRENCY=1 python3 -m d365_snapshot profile --counts-only
```

Il token è salvato in `~/.d365_snapshot_token.json`, **separato** da quello di
IseoPilot: un rinnovo qui non invalida il refresh token del connettore in
produzione. `--borrow-token` riusa quello esistente come punto di partenza, ma
il rinnovo finisce comunque nel file dedicato.

## Livello tabella: cosa questo strumento NON può dare

Lo snapshot fotografa il **livello entità** di D365. Le tabelle fisiche AOT
(`PurchReqTable`, `InventTrans`, `VendTable`…) e le loro relazioni **non sono
esposte da alcuna API in produzione**, e non sono nemmeno ricavabili dal
database: in F&O le relazioni fra tabelle vivono nei metadati AOT, non come
foreign key SQL. Quindi neppure un accesso diretto al DB le restituirebbe.

Le tre strade reali per scendere a quel livello:

1. **Metadati AOT da un ambiente Tier-2 o dev** — i file
   `PackagesLocalDirectory\**\AxTable\*.xml` contengono tabelle, campi, indici,
   EDT, gruppi e **relazioni complete**, incluse le personalizzazioni ISEO.
   È la sorgente più fedele in assoluto, ma richiede un ambiente non-produzione.
2. **Azure Synapse Link for Dataverse** (ex Export to Data Lake) — materializza
   le tabelle F&O reali con i manifesti CDM, che portano attributi e relazioni.
   Supportato in produzione, ed è anche la strada giusta se il volume dati per
   ISEOPilot dovesse crescere.
3. **Browser tabelle** (`?mi=SysTableBrowser`) — utile per ispezionare una
   tabella alla volta, inservibile per un censimento.

Se serve davvero il livello tabella, la 1 è un'estrazione una tantum su un
sandbox e si integra in questo stesso modello: il parser XML è un'aggiunta
contenuta. Dimmelo e lo aggiungo come sottocomando `harvest-aot`.

## Variabili d'ambiente

| Variabile | Default |
|---|---|
| `D365_RESOURCE` | `https://isd365-prod.operations.eu.dynamics.com` |
| `D365_TENANT_ID` / `D365_CLIENT_ID` | app già usata dal connettore IseoPilot |
| `D365_SNAPSHOT_OUT` | `~/IseoPilot/data/d365_snapshot` |
| `D365_HARVEST_CONCURRENCY` | 6 |
| `D365_PROFILE_CONCURRENCY` | 3 |
| `D365_COMPANY` | vuoto = tutte le società |

#!/usr/bin/env python3
"""
Dynamics 365 Finance & Operations Search — OData REST API
Sviluppato da Marco Bonometti
Ricerca (sola lettura) nei Data Entity di D365 F&O tramite OData,
con autenticazione Microsoft OAuth2 Device Code Flow (come OneDrive).
"""
import json
import os
import re
import threading
import time
import datetime
from urllib.parse import quote
from pathlib import Path

TOKEN_FILE = Path.home() / ".chat_assistant_dyn_token.json"

# Relazioni dedotte dai nomi e confermate misurando il join sul dato reale
# (catalogo v3.0). Sono subordinate a quelle dichiarate nei metadati: entrano
# in gioco solo dove i metadati non dichiarano alcun arco.
# Per disattivarle: DYN_USE_INFERRED_RELATIONS=0
USE_INFERRED_RELATIONS = os.environ.get("DYN_USE_INFERRED_RELATIONS", "1") != "0"
INFERRED_MIN_RATE = int(os.environ.get("DYN_INFERRED_MIN_RATE", "80"))
# Path del catalogo entità e degli schema .md. Costanti di modulo così la
# web app può reindirizzarli sotto il volume dati (come TOKEN_FILE).
CATALOG_FILE = Path.home() / ".chat_assistant_dyn_catalog.json"
SCHEMA_DIR   = Path.home() / ".chat_assistant_dyn_schema"
_DBG_LOG = Path.home() / "chat_assistant_debug.txt"


def _dbg(msg):
    """Scrive una riga di diagnostica sullo stesso log di chat_assistant.
    Serve a tracciare l'esito per-entità delle query Dynamics (status,
    n. record, errori) altrimenti invisibili."""
    try:
        with open(_DBG_LOG, "a", encoding="utf-8") as f:
            f.write(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] [DYN] {msg}\n")
    except Exception:
        pass


def _re_split_camel(name: str) -> str:
    """Spezza un nome di entità CamelCase/PascalCase in parole separate, per
    indicizzazione semantica e ricerca. Es. 'PurchaseRequisitionHeaders' ->
    'Purchase Requisition Headers'. Mantiene i blocchi di maiuscole (es. 'CDS')."""
    s = re.sub(r'(?<=[a-z0-9])(?=[A-Z])', ' ', name)
    s = re.sub(r'(?<=[A-Z])(?=[A-Z][a-z])', ' ', s)
    return s


# ── Helpers per ricerche quantitative / temporali ────────────
# Parole che indicano richiesta di CONTEGGIO (case-insensitive).
_COUNT_TRIGGERS = [
    "quanti", "quante", "numero di", "numero degli", "numero delle",
    "totale ", "conta ", "contami", "count", "how many",
]

# Nomi di campi data comunemente presenti nei Data Entity F&O.
# Si prova nell'ordine: il primo presente nell'entità viene usato.
_COMMON_DATE_FIELDS = [
    "OrderDate", "PurchOrderDate", "PurchaseOrderDate",
    "RequestedReceiptDate", "ConfirmedReceiptDate", "DeliveryDate",
    "DocumentDate", "TransDate", "CreatedDateTime", "ModifiedDateTime",
    "InvoiceDate", "PostingDate", "AccountingDate",
]

# Nomi di campi che identificano il RICHIEDENTE / autore in F&O.
# Si prova nell'ordine: il primo presente nell'entità viene usato.
_COMMON_REQUESTER_FIELDS = [
    "RequisitionerPersonnelNumber",
    "OrderingWorkerPersonnelNumber",
    "PreparerPersonnelNumber",
    "BuyerPersonnelNumber",
    "PurchaseAgent",
    "PurchaserCode",
    "PreparedBy",
    "Originator",
    "EnteredBy",
    "Worker",
]

# Entità anagrafiche dei dipendenti (per risolvere Nome Cognome -> Personnel Number).
# Si prova nell'ordine quale è selezionata dall'utente; se nessuna, si suggerisce.
_WORKER_ENTITIES = [
    "Workers", "WorkersV2", "Employees", "EmployeesV2",
    "HcmWorkers", "PersonalContacts",
]

# Campi nome/cognome/numero personale tipici nelle entità Worker.
_WORKER_NAME_FIELDS = ["Name", "FullName", "PersonName", "WorkerName"]
_WORKER_FIRST_FIELDS = ["FirstName", "GivenName"]
_WORKER_LAST_FIELDS  = ["LastName", "SurName", "Surname", "FamilyName"]
_WORKER_PN_FIELDS    = ["PersonnelNumber", "WorkerPersonnelNumber",
                        "EmployeeId", "Worker", "Number"]

# Campi tipici per i FORNITORI (codice + ragione sociale).
_COMMON_VENDOR_CODE_FIELDS = [
    "VendorAccount", "VendorAccountNumber", "Vendor", "VendAccount",
]
_COMMON_VENDOR_NAME_FIELDS = [
    "VendorName", "VendorOrganizationName", "Name", "OrganizationName",
]

# ── NOTE DI DOMINIO — DEFAULT DI FABBRICA ────────────────────────────────────
# Conoscenza che il $metadata di Dynamics NON contiene (quale entità per quale
# domanda, dove stanno richiedente/importi/stati, convenzioni ISEO). Essendo una
# costante nel sorgente, è SEMPRE inclusa nel pacchetto compilato: nessuna copia
# manuale sui Mac dei colleghi. Valgono per qualsiasi LINGUA della domanda.
#
# COME PERSONALIZZARLE:
#  - Per cambiare il default di TUTTI: modifica questo testo e ricompila.
#  - Per sovrascrivere su UNA macchina: crea ~/.chat_assistant_dyn_schema/_hints.md
#    (quel file ha la precedenza su questa costante; vedi _domain_hints).
# Tieni le note CORTE (ne vengono lette ~1500 caratteri): una regola per riga.
_DEFAULT_DOMAIN_HINTS = """\
- RdA / richieste d'acquisto / purchase requisitions -> PurchaseRequisitionLines (righe) + PurchaseRequisitionHeaders (testata, aggancio RequisitionNumber).
- Richiedente RdA = RequisitionerPersonnelNumber (sulle RIGHE, non in testata). Risolvi il nome in PersonnelNumber dall'anagrafica dipendenti.
- Descrizione = LineDescription; importo = LineAmount; data richiesta = RequestedDate; stato riga = LineStatus; stato testata = RequisitionStatus.
- Stato RdA (PurchReqRequisitionStatus): Draft=0, InReview=10, Rejected=20, Approved=30, Cancelled=40, Closed=50.
"""


def _detect_quantitative_intent(query: str) -> bool:
    """Ritorna True se la domanda chiede un conteggio / un totale."""
    q = (query or "").lower()
    return any(kw in q for kw in _COUNT_TRIGGERS)


def _detect_person_filter(query: str, current_user_name: str = ""):
    """Estrae dalla domanda chi è la persona di cui si vogliono i record.

    Ritorna (person_name, is_self) oppure (None, False).
    - is_self=True  → l'utente parla di sé ("mie RdA", "ho fatto", ecc.):
                      person_name è current_user_name (se disponibile).
    - is_self=False → l'utente chiede di un'altra persona ("di Mario Rossi"):
                      person_name è il nome estratto.

    Esempi:
        "quante RdA di Mario Rossi nel 2026" -> ("Mario Rossi", False)
        "quante RdA ho fatto io"             -> (current_user_name, True)
        "ordini fatti da Anna Bianchi"       -> ("Anna Bianchi", False)
        "mostrami gli ordini"                -> (None, False)
    """
    q = (query or "").strip()
    ql = q.lower()

    # Pronomi/forme che indicano "io" / "me stesso"
    self_markers = [
        "mie ", "miei ", "io ho ", "ho fatto", "io faccio",
        "miei ordini", "mie rda", "le mie", "i miei",
        "fatte da me", "fatti da me", "creati da me", "create da me",
        "by me",
    ]
    if any(m in ql for m in self_markers):
        return (current_user_name or None, True)

    # Pattern "di NOME COGNOME" / "fatto/i/e da NOME COGNOME"
    # Cerca 2-3 parole capitalizzate consecutive (es. "Mario Rossi", "Anna Maria Bianchi")
    # dopo i marker. La regex è permissiva sulle preposizioni.
    patterns = [
        r"(?:fatt[aoie] da|creat[aoie] da|preparat[aoie] da|emess[aoie] da|by)\s+([A-ZÀ-Ý][\wÀ-ÿ'\.\-]+(?:\s+[A-ZÀ-Ý][\wÀ-ÿ'\.\-]+){1,2})",
        r"\bdi\s+([A-ZÀ-Ý][\wÀ-ÿ'\.\-]+\s+[A-ZÀ-Ý][\wÀ-ÿ'\.\-]+(?:\s+[A-ZÀ-Ý][\wÀ-ÿ'\.\-]+)?)",
        r"\bdel(?:l[aeoi])?\s+(?:utente|dipendente|collega|sig\.?|sig\.?ra|dott\.?)\s+([A-ZÀ-Ý][\wÀ-ÿ'\.\-]+(?:\s+[A-ZÀ-Ý][\wÀ-ÿ'\.\-]+)*)",
    ]
    for pat in patterns:
        m = re.search(pat, q)
        if m:
            name = m.group(1).strip()
            # Filtro grezzo: scarta se contiene parole comuni che non sono nomi
            stop = {"fornitore", "cliente", "ordine", "rda", "fattura",
                    "documento", "azienda", "periodo", "anno", "mese", "settimana"}
            tokens_lc = {t.lower() for t in name.split()}
            if tokens_lc & stop:
                continue
            return (name, False)

    return (None, False)


def _detect_vendor_filter(query: str):
    """Estrae il nome o codice fornitore dalla domanda.
    Ritorna stringa (nome/codice) o None.

    Esempi:
        "ordini del fornitore ACME"            -> "ACME"
        "RdA fornitore Rossi Componenti S.r.l." -> "Rossi Componenti S.r.l."
        "ordini al fornitore V-0042"           -> "V-0042"
        "ordini di Mario Rossi"                -> None   (è una persona, non un fornitore)
    """
    q = (query or "").strip()
    # Match: "fornitore X" / "vendor X" / "al fornitore X" / "del fornitore X"
    pat = r"(?:fornitor[ei]|vendor|supplier)\s+([A-Za-z0-9ÀÁÂÃÄÅÆÇÈÉÊËÌÍÎÏÑÒÓÔÕÖØÙÚÛÜàáâãäåæçèéêëìíîïñòóôõöøùúûü'\.\-]+(?:\s+[A-Za-z0-9ÀÁÂÃÄÅÆÇÈÉÊËÌÍÎÏÑÒÓÔÕÖØÙÚÛÜàáâãäåæçèéêëìíîïñòóôõöøùúûü'\.\-&]+){0,4})"
    m = re.search(pat, q, re.IGNORECASE)
    if m:
        name = m.group(1).strip()
        # Tronca a prima di parole-stop (per evitare di catturare frasi tipo
        # "fornitore ACME nel 2026" -> non vogliamo "ACME nel")
        stop_re = re.compile(r"\b(nel|del|dell[aoi]?|negli|dal|al|alla|allo|in|per|con|degli|delle|dei|nello|nella|sui|sul|sulla|sullo)\b", re.IGNORECASE)
        m2 = stop_re.search(name)
        if m2:
            name = name[:m2.start()].strip()
        return name or None
    return None


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║  MODULO: VERIFICA ORDINI IN RITARDO                          [INIZIO 1/2]  ║
# ║  Connettore Dynamics 365 — funzionalità "ordini di vendita in ritardo"     ║
# ╠══════════════════════════════════════════════════════════════════════════╣
# ║  COSA FA                                                                   ║
# ║    Trova gli ordini di vendita scaduti (data consegna passata + ancora da  ║
# ║    evadere) e, su richiesta, aggancia in modo DETERMINISTICO:              ║
# ║      • la nuova data prevista (da PlannedOrders, per articolo)             ║
# ║      • l'ordine reale di acquisto (AppendingPurchaseOrderNumber)           ║
# ║      • l'ordine reale di produzione (per DemandSalesOrderNumber)           ║
# ║      • il codice articolo dell'ordine a monte (può ≠ articolo venduto)     ║
# ║    Genera anche un report HTML colorato (vedi _write_overdue_html).        ║
# ║                                                                            ║
# ║  ENTITÀ DYNAMICS NECESSARIE (da caricare in Impostazioni → Entità)         ║
# ║    • CDSSalesOrderLinesV2     righe ordini vendita (date + stato)  [BASE]  ║
# ║    • PlannedOrders            proposte MRP + nuova data           [+DATA]  ║
# ║    • ProductionOrderHeaders   ordini di produzione reali        [+PROD.]   ║
# ║    (solo CDSSalesOrderLinesV2 è obbligatoria; le altre due abilitano       ║
# ║     l'arricchimento con nuova data e ordini reali)                         ║
# ║                                                                            ║
# ║  PRINCIPIO: il CODICE fa i join (intento, query, accoppiamento); l'AI      ║
# ║  si limita a presentare. Vietato lasciare l'accoppiamento all'AI.          ║
# ║                                                                            ║
# ║  COMPONENTI (in quest'ordine nel file):                                    ║
# ║    parte 1/2 → _detect_overdue_intent, _detect_planning_enrichment         ║
# ║    parte 2/2 → find_overdue (+ _planning_map_for_products,                 ║
# ║                _production_orders_for_sales, _write_overdue_html)          ║
# ╚══════════════════════════════════════════════════════════════════════════╝
def _looks_like_targeted_lookup(query: str) -> bool:
    """Rileva se la domanda cita un identificativo/codice specifico (es. un
    codice cliente C040182, un codice fornitore F012345) o chiede dati legati a
    un'anagrafica (cliente/fornitore) che richiedono un filtro mirato. In questi
    casi conviene la modalità dinamica anziché un elenco generico."""
    import re as _r
    q = (query or "")
    # Codice tipo lettera+cifre (C040182, F013653, 2D70016514...) o cifre lunghe
    if _r.search(r"\b[A-Za-z]{1,3}\d{4,}\b", q):
        return True
    # Riferimento esplicito ad anagrafiche che le righe ordine non contengono
    ql = q.lower()
    anagrafiche = ["cliente", "clienti", "fornitore", "fornitori", "vendor",
                   "customer", "anagrafica"]
    if any(a in ql for a in anagrafiche):
        return True
    return False


def _detect_overdue_intent(query: str) -> bool:
    """Rileva richieste su ordini SCADUTI / IN RITARDO.
    Es: 'ordini scaduti', 'consegne in ritardo', 'ordini overdue',
    'cosa è in ritardo'. Ritorna True/False.
    """
    q = (query or "").lower()
    markers = [
        "scadut", "in ritardo", "ritardat", "overdue", "in arretrat",
        "arretrat", "non consegnat", "consegne mancate", "sforati",
        "oltre la data", "fuori tempo",
    ]
    return any(m in q for m in markers)


def _detect_planning_enrichment(query: str) -> bool:
    """Rileva se, oltre al ritardo, l'utente chiede anche la NUOVA DATA
    pianificata o il MOTIVO. In tal caso arricchiamo con gli ordini
    pianificati (PlannedOrders), agganciati per articolo.
    Es: '...e la nuova data di spedizione pianificata', '...e il motivo',
    '...quando arriverà', '...nuova consegna prevista'.
    """
    q = (query or "").lower()
    markers = [
        "nuova data", "nuova consegna", "data pianificata", "data prevista",
        "pianificat", "quando arriv", "quando sar", "ripianificat",
        "riprogrammat", "motivo", "perch", "causa", "nuova spedizione",
    ]
    return any(m in q for m in markers)
# ── MODULO VERIFICA ORDINI IN RITARDO ─────────────────────────── [FINE 1/2] ─


# Campi candidati per la DATA DI CONSEGNA su cui valutare il ritardo.
# Ordine di preferenza: la data confermata di spedizione è la più indicativa.
_DELIVERY_DATE_FIELDS = [
    "ConfirmedShippingDate", "ConfirmedReceiptDate",
    "RequestedShippingDate", "RequestedReceiptDate",
]
# Campi candidati per lo STATO di evasione (enum SalesStatus).
_LINE_STATUS_FIELDS = ["SalesOrderLineStatus", "SalesOrderStatus", "SalesStatus"]
# Valori dell'enum SalesStatus che indicano "ANCORA DA EVADERE" (in ritardo se
# la data è passata). 'Backorder' = ordine aperto non ancora consegnato.
_OPEN_STATUS_VALUES = ["Backorder"]
# Prefisso completo del tipo enum nei filtri OData di F&O.
_SALESSTATUS_ENUM_PREFIX = "Microsoft.Dynamics.DataEntities.SalesStatus"


def _fmt_date_it(v):
    """Converte una data ISO ('2025-01-24T12:00:00Z') nel formato italiano
    '24/01/2025'. Lascia invariati i valori non-data o vuoti."""
    if not v or v in ("—", "NON DISPONIBILE", "?", "n/d"):
        return v or "—"
    s = str(v)
    try:
        if len(s) >= 10 and s[4] == "-" and s[7] == "-":
            return f"{s[8:10]}/{s[5:7]}/{s[0:4]}"
    except (ValueError, IndexError):
        pass
    return s

# Entità degli ordini pianificati (output del master planning) e relativi campi.
_PLANNED_ORDER_ENTITIES = ["PlannedOrders", "ReqPlannedOrderBiEntities"]
# Modulo "Verifica ordini in ritardo": entità FISSE (deterministiche).
# Il modulo NON filtra da dyn_entities — usa esattamente queste, in ordine di
# preferenza. La prima disponibile per categoria viene usata.
_OVERDUE_SALES_ENTITIES = ["CDSSalesOrderLinesV2", "CDSSalesOrderLines",
                           "D365SalesOrderLines", "SalesOrderLines"]
_OVERDUE_PLANNED_ENTITY = "PlannedOrders"
_OVERDUE_PRODUCTION_ENTITY = "ProductionOrderHeaders"
# Campo articolo per agganciare ordine in ritardo <-> ordine pianificato.
_PRODUCT_KEY_FIELDS = ["ProductNumber", "ItemNumber"]
# Campi data della nuova consegna/fabbisogno pianificato.
_PLANNED_DATE_FIELDS = ["DeliveryDate", "RequirementDate", "OrderDate", "ScheduledEndDate"]
# Campi quantità candidati per entità (il primo presente viene usato).
_SALES_QTY_FIELDS = ["OrderedSalesQuantity", "SalesQuantity", "OrderedQuantity",
                     "Quantity", "ConfirmedQuantity"]
_PROD_QTY_FIELDS = ["ScheduledQuantity", "EstimatedQuantity", "StartedQuantity",
                    "ProductionQuantity", "InventoryQuantity", "Quantity"]
# Date dell'ordine di produzione, in ordine di preferenza, usate come "nuova data"
# SPECIFICA PER ORDINE (l'ordine di produzione è agganciato all'ordine di vendita
# via DemandSalesOrderNumber). La data di consegna è la più pertinente; in mancanza
# si ripiega sulla fine pianificata.
_PROD_DATE_FIELDS = ["DeliveryDate", "ScheduledEndDate", "ScheduledDate", "EstimatedDate"]
_PURCH_QTY_FIELDS = ["PurchaseQuantity", "RequirementQuantity", "ProcurementQuantity",
                     "OrderedPurchaseQuantity", "Quantity"]
# Stati ordine di produzione da ESCLUDERE (solo chiusi/finiti definitivi).
# NB: 'Completed' NON è incluso: nell'istanza è lo stato normale degli ordini lavorati.
_PROD_STATUS_EXCLUDE = ["ended", "reportedasfinished"]


def _detect_list_intent(query: str):
    """Rileva richieste di LISTA/MOSTRA (es. 'mostrami le mie RdA recenti',
    'ultime 10 fatture', 'elencami gli ordini').

    Ritorna (is_list, n) dove n è il numero esplicitamente richiesto
    (default 10 se non specificato). is_list=False se non è una richiesta
    di lista.

    A differenza della ricerca testuale (contains), una lista vuole solo
    i record recenti ordinati per data, senza filtri di parola.
    """
    q = (query or "").lower()
    list_markers = [
        "mostrami", "mostra ", "elencami", "elenca ", "lista ", "list ",
        "ultime ", "ultimi ", "ultim'", "recent",
        "fammi vedere", "vedere le", "vedere gli", "vedere i ",
        "show me", "show ",
    ]
    if not any(m in q for m in list_markers):
        return (False, 0)
    m = re.search(r"\b(?:ultim[ie]|prim[ie]|top)\s+(\d{1,3})", q)
    n = int(m.group(1)) if m else 10
    return (True, max(1, min(n, 50)))


def _detect_date_range(query: str):
    """Estrae un intervallo temporale dalla domanda (lingua italiana).
    Ritorna (start_iso, end_iso, label) o (None, None, None).

    Riconosce:
    - "nel YYYY" / "del YYYY" / "anno YYYY"  → intero anno
    - "ultimi N giorni" / "ultime N settimane" / "ultimi N mesi"
    - "oggi"
    - "questo mese" / "mese corrente" / "questo anno"
    - "mese scorso" / "ultimo mese"
    - "anno scorso" / "ultimo anno"
    """
    q = (query or "").lower()
    today = datetime.date.today()

    # Anno esplicito (es. "nel 2026" / "del 2026" / "2026")
    m = re.search(r"\b(20\d{2})\b", q)
    if m:
        y = int(m.group(1))
        return (f"{y}-01-01", f"{y}-12-31", f"anno {y}")

    # Ultimi N giorni/settimane/mesi
    m = re.search(r"ultim[ie]\s+(\d{1,3})\s*(giorn|settiman|mes)", q)
    if m:
        n = int(m.group(1)); unit = m.group(2)
        if unit.startswith("giorn"):
            start = today - datetime.timedelta(days=n)
            return (start.isoformat(), today.isoformat(), f"ultimi {n} giorni")
        if unit.startswith("settiman"):
            start = today - datetime.timedelta(weeks=n)
            return (start.isoformat(), today.isoformat(), f"ultime {n} settimane")
        if unit.startswith("mes"):
            start = today - datetime.timedelta(days=30 * n)
            return (start.isoformat(), today.isoformat(), f"ultimi {n} mesi")

    if "oggi" in q:
        return (today.isoformat(), today.isoformat(), "oggi")

    if "questo mese" in q or "mese corrente" in q:
        start = today.replace(day=1)
        return (start.isoformat(), today.isoformat(), "questo mese")

    if "questo anno" in q or "anno corrente" in q or "anno in corso" in q:
        start = today.replace(month=1, day=1)
        return (start.isoformat(), today.isoformat(), "questo anno")

    if "mese scorso" in q or "ultimo mese" in q:
        first_this = today.replace(day=1)
        last_prev = first_this - datetime.timedelta(days=1)
        first_prev = last_prev.replace(day=1)
        return (first_prev.isoformat(), last_prev.isoformat(), "mese scorso")

    if "anno scorso" in q or "ultimo anno" in q:
        y = today.year - 1
        return (f"{y}-01-01", f"{y}-12-31", f"anno {y}")

    return (None, None, None)


def extract_user_name_from_token(access_token: str) -> str:
    """Estrae il nome dell'utente dal JWT Microsoft (claim 'name', poi
    'given_name'+'family_name' come fallback). Ritorna '' se non riesce.
    Non valida la firma — serve solo a leggere chi è loggato.
    """
    if not access_token:
        return ""
    try:
        import base64
        # JWT: header.payload.signature — prendiamo solo il payload (middle)
        parts = access_token.split(".")
        if len(parts) < 2:
            return ""
        payload = parts[1]
        # Padding base64url
        payload += "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload).decode("utf-8", errors="ignore"))
        name = data.get("name") or ""
        if not name:
            gn = data.get("given_name", "")
            fn = data.get("family_name", "")
            if gn or fn:
                name = (gn + " " + fn).strip()
        if not name:
            name = data.get("preferred_username", "") or data.get("upn", "")
            # Se è una mail (mario.rossi@iseo.com) non è utile come nome — scarta
            if "@" in name:
                name = ""
        return name
    except Exception:
        return ""


# ── Token manager (Device Code Flow per D365 F&O) ────────────
class DynamicsTokenManager:
    """Gestisce access/refresh token Microsoft OAuth2 per D365 F&O.

    A differenza di OneDrive (scope Graph), lo scope qui e':
        <resource_url>/.default
    dove resource_url e' la URL della tua istanza F&O, es.
        https://nomeazienda.operations.dynamics.com
    """

    def __init__(self, client_id: str, tenant_id: str = "common",
                 resource_url: str = ""):
        self.client_id = client_id
        self.tenant_id = tenant_id or "common"
        # Normalizza: niente slash finale (richiesto da F&O)
        self.resource_url = (resource_url or "").rstrip("/")
        self._token_data = None
        self._cancel_poll = False
        self._poll_in_progress = False
        self._load()

    def _scope(self) -> str:
        # Scope OAuth2 v2.0 per F&O: <resource>/.default + offline_access
        if self.resource_url:
            return f"{self.resource_url}/.default offline_access"
        return "offline_access"

    def cancel_polling(self):
        self._cancel_poll = True

    def _load(self):
        try:
            if TOKEN_FILE.exists():
                self._token_data = json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
        except Exception:
            self._token_data = None

    def _save(self, data: dict):
        try:
            self._token_data = data
            TOKEN_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except Exception:
            pass

    def is_authenticated(self) -> bool:
        return bool(self._token_data and self._token_data.get("access_token"))

    def get_access_token(self) -> str:
        if not self._token_data:
            return ""
        expires_at   = self._token_data.get("expires_at", 0)
        access_token = self._token_data.get("access_token", "")
        if access_token and time.time() < expires_at - 300:
            return access_token
        refresh_token = self._token_data.get("refresh_token", "")
        if refresh_token:
            new_token = self._refresh(refresh_token)
            if new_token:
                return new_token
        return access_token

    def _refresh(self, refresh_token: str) -> str:
        try:
            import requests as req
            url = f"https://login.microsoftonline.com/{self.tenant_id}/oauth2/v2.0/token"
            r = req.post(url, data={
                "client_id":     self.client_id,
                "grant_type":    "refresh_token",
                "refresh_token": refresh_token,
                "scope":         self._scope(),
            }, timeout=15)
            resp = r.json()
            if "access_token" in resp:
                resp["expires_at"] = time.time() + resp.get("expires_in", 3600)
                self._save(resp)
                return resp["access_token"]
        except Exception:
            pass
        return ""

    def start_device_flow(self) -> dict:
        """Avvia Device Code Flow — ritorna dict con user_code e device_code."""
        try:
            import requests as req
            url = f"https://login.microsoftonline.com/{self.tenant_id}/oauth2/v2.0/devicecode"
            r = req.post(url, data={
                "client_id": self.client_id,
                "scope":     self._scope(),
            }, timeout=15)
            return r.json()
        except Exception as e:
            return {"error": str(e)}

    def poll_device_flow(self, device_code: str, interval: int = 5) -> tuple:
        """Polling token con cancellazione esterna e protezione re-entry."""
        if self._poll_in_progress:
            return False, "Polling gia in corso — attendi o riavvia l'app"
        self._poll_in_progress = True
        self._cancel_poll = False

        try:
            import requests as req
            url = f"https://login.microsoftonline.com/{self.tenant_id}/oauth2/v2.0/token"
            poll_interval = max(interval, 3)

            for attempt in range(80):
                slept = 0.0
                while slept < poll_interval:
                    if self._cancel_poll:
                        return False, "Polling annullato dall'utente"
                    time.sleep(0.5)
                    slept += 0.5

                if self._cancel_poll:
                    return False, "Polling annullato dall'utente"

                try:
                    r = req.post(url, data={
                        "client_id":   self.client_id,
                        "grant_type":  "urn:ietf:params:oauth:grant-type:device_code",
                        "device_code": device_code,
                    }, timeout=10)
                    resp = r.json()
                    if "access_token" in resp:
                        resp["expires_at"] = time.time() + resp.get("expires_in", 3600)
                        self._save(resp)
                        return True, "Autenticato con successo"
                    err = resp.get("error", "")
                    if err == "authorization_pending":
                        continue
                    elif err == "slow_down":
                        poll_interval += 5
                        continue
                    elif err == "expired_token":
                        return False, "Codice scaduto — riprova il login"
                    elif err == "access_denied":
                        return False, "Accesso negato dall'utente"
                    else:
                        return False, resp.get("error_description", f"Errore: {err}")
                except Exception:
                    continue
            return False, "Timeout — il codice e scaduto. Riprova il login."
        except Exception as e:
            return False, str(e)
        finally:
            self._poll_in_progress = False
            self._cancel_poll = False

    def logout(self):
        self.cancel_polling()
        self._token_data = None
        try:
            if TOKEN_FILE.exists():
                TOKEN_FILE.unlink()
        except Exception:
            pass


# ── Ricerca OData su D365 F&O ────────────────────────────────
# ── Indice semantico: CACHE DI PROCESSO (v2.0 web) ─────────────────────────
# Sul web ogni ricerca crea una nuova istanza di DynamicsSearch: la cache
# per-istanza veniva quindi ricostruita a ogni domanda (70-120 s su ~4700
# entità, visibile nei log). Qui l'indice vive a livello di modulo: UNA
# costruzione per processo, ricostruita solo se il catalogo cambia.
_SEM_LOCK = threading.Lock()
_SEM_MODEL = None
_SEM_MATRIX = None
_SEM_NAMES: list = []
_SEM_SIG = None


def _semantic_ensure(full_catalog: dict, cfg: dict | None = None):
    """Costruisce (una volta per processo) l'indice embeddings delle entità.
    Ritorna (names, matrix, model) oppure None se librerie/modello mancano."""
    try:
        from sentence_transformers import SentenceTransformer
    except Exception:
        return None
    global _SEM_MODEL, _SEM_MATRIX, _SEM_NAMES, _SEM_SIG
    names = list(full_catalog.keys())
    if not names:
        return None
    sig = (len(names), hash(tuple(names)))
    with _SEM_LOCK:
        if _SEM_SIG != sig or _SEM_MATRIX is None:
            if _SEM_MODEL is None:
                model_name = (cfg or {}).get("dyn_semantic_model",
                                             "paraphrase-multilingual-MiniLM-L12-v2")
                try:
                    from vector_db import resolve_embedding_model
                    model_name = resolve_embedding_model(model_name)
                except Exception:
                    pass
                try:
                    _SEM_MODEL = SentenceTransformer(model_name)
                except Exception:
                    try:
                        _SEM_MODEL = SentenceTransformer("all-MiniLM-L6-v2")
                    except Exception as e:
                        _dbg(f"semantic: modello non caricabile ({e})")
                        return None
            # Testo indicizzato: nome entità "spezzato" in parole + alcuni campi
            docs = []
            for n in names:
                spez = _re_split_camel(n)
                campi = " ".join((full_catalog[n].get("string") or [])[:8])
                docs.append(f"{spez} {campi}")
            _SEM_MATRIX = _SEM_MODEL.encode(docs, normalize_embeddings=True,
                                            show_progress_bar=False)
            _SEM_NAMES = names
            _SEM_SIG = sig
            _dbg(f"semantic: indice embeddings costruito su {len(names)} entità (cache di processo)")
    return _SEM_NAMES, _SEM_MATRIX, _SEM_MODEL


class DynamicsSearch:
    """Ricerca in sola lettura nei Data Entity di D365 F&O via OData."""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.resource_url = (cfg.get("dyn_resource_url", "") or "").rstrip("/")
        self.tm = DynamicsTokenManager(
            client_id=cfg.get("dyn_client_id", ""),
            tenant_id=cfg.get("dyn_tenant_id", "common"),
            resource_url=self.resource_url,
        )
        # ── Schema .md (OPZIONALE) ───────────────────────────────────────────
        # Cartella con un file <Entità>.md per entità (prodotta da
        # dyn_schema_map.py --split). Se presente, le CHIAVI (chiave primaria) e
        # i VALORI ENUM di ciascuna entità candidata vengono iniettati (capati)
        # nel planner agentico, così costruisce i $filter con i letterali giusti.
        # Se la cartella non c'è o è vuota -> il connettore lavora come prima
        # (nessun arricchimento, nessun rischio per la ricerca).
        self._schema_dir = self._resolve_schema_dir(cfg)
        self._schema_md_cache = {}
        if self._schema_dir:
            _dbg(f"schema .md: cartella attiva -> {self._schema_dir}")

    def _resolve_schema_dir(self, cfg) -> str:
        """Cartella degli schema .md. Priorità: config 'dyn_schema_dir';
        poi la cartella AUTO-GENERATA accanto al catalogo
        (~/.chat_assistant_dyn_schema, prodotta da 'Rigenera catalogo');
        infine '<kb_dir>/schema_dynamics'. '' se nessuna."""
        try:
            import os
            from pathlib import Path
            d = (cfg.get("dyn_schema_dir", "") or "").strip()
            if d and os.path.isdir(d):
                return d
            auto = str(SCHEMA_DIR)
            if os.path.isdir(auto):
                return auto
            kb = (cfg.get("kb_dir", "") or "").strip()
            if kb:
                cand = os.path.join(kb, "schema_dynamics")
                if os.path.isdir(cand):
                    return cand
        except Exception:
            pass
        return ""

    @staticmethod
    def _safe_entity_filename(name: str) -> str:
        """Stesso schema-nome di dyn_schema_map.safe_filename + estensione .md."""
        keep = "".join(c if (c.isalnum() or c in "-_") else "_" for c in str(name))
        return (keep[:120] or "entita") + ".md"

    def _entity_schema_extra(self, entity: str) -> str:
        """CHIAVI + VALORI ENUM (capati) letti da <entity>.md, per arricchire il
        planner. Ritorna '' se la cartella schema non c'è o il file manca.
        Non solleva MAI: in caso di errore degrada a '' (planner come v2.0)."""
        if not self._schema_dir:
            return ""
        if entity in self._schema_md_cache:
            return self._schema_md_cache[entity]
        extra = ""
        try:
            import os, re as _re
            path = os.path.join(self._schema_dir, self._safe_entity_filename(entity))
            if not os.path.isfile(path):
                self._schema_md_cache[entity] = ""
                return ""
            with open(path, "r", encoding="utf-8", errors="ignore") as fh:
                md = fh.read()
            # Chiavi: righe della tabella **Campi** con il marcatore chiave (3a colonna)
            keys = [m.group(1).strip()
                    for m in _re.finditer(r"^\|\s*([^|]+?)\s*\|[^|]*\|\s*\U0001F511\s*\|", md, _re.M)]
            # Valori enum: blocco **Valori enum** (righe "- `Enum` (campo/i: ...): ...")
            enum_lines = []
            mblock = _re.search(r"\*\*Valori enum\*\*\s*(.+?)(?:\n\*\*|\Z)", md, _re.S)
            if mblock:
                for ln in mblock.group(1).splitlines():
                    ln = ln.strip()
                    if ln.startswith("- "):
                        enum_lines.append(ln[2:].strip())
            parts = []
            if keys:
                parts.append("  Chiavi: " + ", ".join(keys[:6]))
            if enum_lines:
                capped = [(e[:120] + "...") if len(e) > 120 else e for e in enum_lines[:8]]
                parts.append("  Enum: " + " | ".join(capped))
            extra = ("\n" + "\n".join(parts)) if parts else ""
            if len(extra) > 600:
                extra = extra[:600] + "..."
        except Exception:
            extra = ""
        self._schema_md_cache[entity] = extra
        return extra

    def _generate_schema_md(self, root, outdir: str) -> int:
        """Da un $metadata GIÀ parsato (ElementTree 'root') scrive un file
        <Entità>.md per entità in 'outdir': campi+tipi+chiavi, valori enum usati
        dai campi, relazioni reali. È ciò che alimenta l'arricchimento del planner.
        Chiamato da build_full_catalog: nessuna copia manuale. Ritorna il numero
        di file scritti. Solleva al chiamante in caso di errore grave (gestito lì)."""
        import os, datetime
        def _local(tag): return tag.rsplit("}", 1)[-1]

        enums = {}     # EnumType -> [(membro, valore|None)]
        types = {}     # EntityType -> {"fields": [(nome, tipo, nullable, is_key)], "rels": [...]}
        set_to_type = {}
        for el in root.iter():
            t = _local(el.tag)
            if t == "EnumType":
                ename = el.get("Name", "")
                membri = []
                for m in el:
                    if _local(m.tag) == "Member":
                        membri.append((m.get("Name", ""), m.get("Value")))
                if ename:
                    enums[ename] = membri
            elif t == "EntityType":
                name = el.get("Name", "")
                if not name:
                    continue
                keys, fields, rels = [], [], []
                for child in el:
                    ct = _local(child.tag)
                    if ct == "Key":
                        for pr in child:
                            if _local(pr.tag) == "PropertyRef":
                                kn = pr.get("Name", "")
                                if kn:
                                    keys.append(kn)
                    elif ct == "Property":
                        fn = child.get("Name", "")
                        if fn:
                            fields.append((fn, child.get("Type", ""),
                                           child.get("Nullable", "true").lower() != "false"))
                    elif ct == "NavigationProperty":
                        nav = child.get("Name", ""); typ = child.get("Type", "")
                        if not nav or not typ:
                            continue
                        inner, coll = typ, False
                        if inner.startswith("Collection(") and inner.endswith(")"):
                            inner = inner[len("Collection("):-1]; coll = True
                        pairs = []
                        for rc in child:
                            if _local(rc.tag) == "ReferentialConstraint":
                                loc = rc.get("Property", ""); rem = rc.get("ReferencedProperty", "")
                                if loc and rem:
                                    pairs.append((loc, rem))
                        rels.append({"nav": nav, "target_type": inner.rsplit(".", 1)[-1],
                                     "coll": coll, "pairs": pairs})
                keyset = set(keys)
                types[name] = {"fields": [(fn, tp, nu, (fn in keyset)) for (fn, tp, nu) in fields],
                               "rels": rels}
            elif t == "EntitySet":
                sn = el.get("Name", ""); et = el.get("EntityType", "")
                if sn and et:
                    set_to_type[sn] = et.rsplit(".", 1)[-1]

        type_to_set = {}
        for sn, tn in set_to_type.items():
            type_to_set.setdefault(tn, sn)

        def _pretty(typ):
            base, coll = typ or "", False
            if base.startswith("Collection(") and base.endswith(")"):
                base = base[len("Collection("):-1]; coll = True
            local = base.rsplit(".", 1)[-1]
            suffix = "[]" if coll else ""
            if base.startswith("Edm."):
                return local + suffix, None
            if local in enums:
                return "enum " + local + suffix, local
            return local + suffix, None

        def _entity_md(set_name, type_name):
            tinfo = types.get(type_name, {"fields": [], "rels": []})
            fields, rels = tinfo["fields"], tinfo["rels"]
            out = [f"## {set_name}",
                   f"Tipo: `{type_name}` · Campi: {len(fields)} · Relazioni: {len(rels)}", ""]
            if fields:
                out += ["**Campi**", "", "| Campo | Tipo | Chiave |", "|---|---|---|"]
                used = {}
                for (fn, typ, _nu, iskey) in fields:
                    disp, en = _pretty(typ)
                    if en:
                        used.setdefault(en, []).append(fn)
                    out.append(f"| {fn} | {disp} | {'🔑' if iskey else ''} |")
                out.append("")
                if used:
                    out.append("**Valori enum**")
                    for en, campi in sorted(used.items()):
                        membri = enums.get(en, [])
                        vals = ", ".join(f"{m}={v}" if v is not None else m
                                         for (m, v) in membri) or "(nessun membro)"
                        out.append(f"- `{en}` (campo/i: {', '.join(campi)}): {vals}")
                    out.append("")
            else:
                out += ["_(nessun campo nei metadata)_", ""]
            if rels:
                out.append("**Relazioni**")
                for r in rels:
                    tgt = type_to_set.get(r["target_type"], r["target_type"])
                    agg = (" — aggancio: " + ", ".join(f"{a}->{b}" for (a, b) in r["pairs"])) if r["pairs"] else ""
                    coll = " [collezione]" if r["coll"] else ""
                    out.append(f"- `{r['nav']}` -> `{tgt}`{coll}{agg}")
                out.append("")
            return "\n".join(out)

        os.makedirs(outdir, exist_ok=True)
        generato = datetime.datetime.now().isoformat(timespec="seconds")
        n = 0
        for set_name, type_name in set_to_type.items():
            md = (f"<!-- Mappa schema D365 F&O - generato {generato} -->\n\n"
                  + _entity_md(set_name, type_name))
            with open(os.path.join(outdir, self._safe_entity_filename(set_name)),
                      "w", encoding="utf-8") as fh:
                fh.write(md)
            n += 1
        try:
            with open(os.path.join(outdir, "_indice.md"), "w", encoding="utf-8") as fh:
                fh.write(f"# Schema Dynamics 365 F&O - indice\nGenerato: {generato} - Entità: {n}\n")
        except Exception:
            pass
        return n

    def _entity_schema_full(self, entity: str, max_chars: int = 1400) -> str:
        """Scheda RICCA di un'entità per l'azione agentica 'schema': chiavi,
        valori enum E relazioni, letti da <entity>.md. Più completa di
        _entity_schema_extra (pensata per l'iniezione automatica capata): questa
        viene restituita SOLO quando il planner la chiede esplicitamente, quindi
        può permettersi più dettaglio. '' se schema non attivo o file mancante."""
        if not self._schema_dir:
            return ""
        try:
            import os, re as _re
            path = os.path.join(self._schema_dir, self._safe_entity_filename(entity))
            if not os.path.isfile(path):
                return ""
            with open(path, "r", encoding="utf-8", errors="ignore") as fh:
                md = fh.read()
            keys = [m.group(1).strip()
                    for m in _re.finditer(r"^\|\s*([^|]+?)\s*\|[^|]*\|\s*\U0001F511\s*\|", md, _re.M)]
            enum_lines = []
            mblock = _re.search(r"\*\*Valori enum\*\*\s*(.+?)(?:\n\*\*|\Z)", md, _re.S)
            if mblock:
                for ln in mblock.group(1).splitlines():
                    ln = ln.strip()
                    if ln.startswith("- "):
                        e = ln[2:].strip()
                        enum_lines.append((e[:200] + "...") if len(e) > 200 else e)
            rel_lines = []
            rblock = _re.search(r"\*\*Relazioni\*\*\s*(.+?)(?:\n\*\*|\Z)", md, _re.S)
            if rblock:
                for ln in rblock.group(1).splitlines():
                    ln = ln.strip()
                    if ln.startswith("- "):
                        rel_lines.append(ln[2:].strip())
            parts = [f"SCHEDA {entity}:"]
            if keys:
                parts.append("  Chiavi: " + ", ".join(keys[:8]))
            if enum_lines:
                parts.append("  Enum:\n    " + "\n    ".join(enum_lines[:10]))
            if rel_lines:
                parts.append("  Relazioni:\n    " + "\n    ".join(rel_lines[:10]))
            out = "\n".join(parts)
            return out[:max_chars] + ("..." if len(out) > max_chars else "")
        except Exception:
            return ""

    def _domain_hints(self, max_chars: int = 1500) -> str:
        """NOTE DI DOMINIO per il planner (valide in qualsiasi lingua della
        domanda). Ordine di precedenza:
          1) override per-macchina: <schema_dir>/_hints.md (se l'utente lo crea);
          2) file _hints.md impacchettato accanto al codice (se incluso nello spec);
          3) default DI FABBRICA _DEFAULT_DOMAIN_HINTS, SEMPRE presente nel build.
        Così le note sono sempre nel pacchetto (nessuna copia manuale) e restano
        personalizzabili. Non solleva mai."""
        import os, sys
        candidates = []
        if self._schema_dir:
            candidates.append(os.path.join(self._schema_dir, "_hints.md"))
        try:
            base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
            candidates.append(os.path.join(base, "_hints.md"))
        except Exception:
            pass
        for path in candidates:
            try:
                if path and os.path.isfile(path):
                    with open(path, "r", encoding="utf-8", errors="ignore") as fh:
                        txt = fh.read().strip()
                    if txt:
                        return txt[:max_chars] + ("..." if len(txt) > max_chars else "")
            except Exception:
                continue
        txt = (_DEFAULT_DOMAIN_HINTS or "").strip()
        return (txt[:max_chars] + ("..." if len(txt) > max_chars else "")) if txt else ""

    def is_configured(self) -> bool:
        return bool(self.cfg.get("dyn_client_id", "").strip()
                    and self.resource_url)

    def is_authenticated(self) -> bool:
        return self.tm.is_authenticated() and bool(self.tm.get_access_token())

    def _data_url(self, path: str = "") -> str:
        return f"{self.resource_url}/data{path}"

    # ╔══════════════════════════════════════════════════════════════════════╗
    # ║  PROTEZIONE SOLA LETTURA (read-only) verso Dynamics 365               ║
    # ║  Per costruzione il connettore può SOLO leggere: ogni richiesta verso ║
    # ║  l'istanza F&O passa da qui e deve essere GET. Verbi di modifica       ║
    # ║  (POST/PATCH/PUT/DELETE) e azioni OData che cambiano stato sono        ║
    # ║  rifiutati a prescindere. Difesa nel codice, complementare al ruolo    ║
    # ║  di sola lettura sull'utenza F&O.                                     ║
    # ╚══════════════════════════════════════════════════════════════════════╝
    def _dyn_get(self, url: str, token: str, timeout: int = 30, accept: str = "application/json"):
        """UNICO canale per interrogare Dynamics. Consente solo letture (GET).
        Blocca qualunque URL che punti a operazioni di scrittura o azioni OData
        che modificano lo stato. Solleva PermissionError se la richiesta non è
        una lettura sicura."""
        import requests as req
        # 1. Deve essere una URL della nostra istanza Dynamics
        if not self.resource_url or not str(url).startswith(self.resource_url):
            raise PermissionError(f"URL non Dynamics rifiutato: {url}")
        # 2. Blocca azioni/operazioni OData che modificano stato.
        #    Le letture usano /data/Entità con $filter/$top/$select/$count/$orderby/$metadata.
        #    Qualsiasi segmento che invoca azioni (Microsoft.Dynamics... con ())
        #    o $batch è rifiutato.
        low = url.lower()
        vietati = ["$batch", "/action", "microsoft.dynamics.dataentities.action"]
        if any(v in low for v in vietati):
            _dbg(f"[READ-ONLY] richiesta bloccata (azione/batch): {url[:120]}")
            raise PermissionError("Operazione non di sola lettura bloccata dal connettore")
        # 3. Esegui SOLO come GET. Nessun altro verbo è esposto da questo metodo.
        return req.get(url, headers={"Authorization": f"Bearer {token}",
                                     "Accept": accept}, timeout=timeout)

    @staticmethod
    def _is_readonly_filter(filtro: str) -> bool:
        """Controllo extra sulle query costruite dall'AI: il filtro OData deve
        essere una pura espressione di lettura, senza tentativi di azioni o
        sintassi sospette. Ritorna True se è sicuro."""
        if not filtro:
            return True
        low = filtro.lower()
        sospetti = ["$batch", "microsoft.dynamics", "/action", "import", "update",
                    "delete", "insert", "exec", ";"]
        return not any(s in low for s in sospetti)

    def get_user_info(self) -> dict:
        """Verifica connessione interrogando il service root OData.
        F&O non ha /me come Graph; il service root `/data` è l'endpoint
        standard sempre presente, che elenca le entità — basta lui per
        confermare che token, scope e accesso sono validi senza dipendere
        dall'esistenza di una specifica entità (es. CompaniesV2 può non
        essere pubblica in ogni ambiente).
        """
        try:
            import requests as req
            token = self.tm.get_access_token()
            if not token:
                return {}
            # $top=1 sul service root: ritorna 1 sola entità dall'elenco,
            # risposta minima ma sufficiente come ping autenticato.
            url = self._data_url("/?$top=1")
            r = self._dyn_get(url, token, timeout=15)
            if r.status_code == 200:
                # Conta entità totali se è possibile
                count = "?"
                try:
                    count = len(r.json().get("value", []))
                except Exception:
                    pass
                return {"ok": True, "status": 200, "preview": count}
            return {"ok": False, "status": r.status_code, "body": r.text[:300]}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def list_entities(self, max_results: int = 0) -> list:
        """Elenca i Data Entity pubblici disponibili sull'endpoint OData.
        Con max_results=0 restituisce l'elenco completo."""
        token = self.tm.get_access_token()
        if not token:
            return []
        try:
            import requests as req
            r = self._dyn_get(self._data_url(), token, timeout=30)
            if r.status_code != 200:
                return []
            data = r.json()
            names = [e.get("name", "") for e in data.get("value", []) if e.get("name")]
            names = sorted(set(names))
            return names[:max_results] if max_results else names
        except Exception:
            return []

    def fetch_string_fields(self, entities: list = None) -> dict:
        """Ricava, per ogni entità, l'elenco dei campi di tipo Edm.String.

        FONTE PRIMARIA: il catalogo completo su disco (se presente), che evita
        di riscaricare i metadata. Solo se il catalogo manca o non copre tutte
        le entità richieste si scaricano i metadata da F&O.

        Ritorna un dict {entity_set_name: [campo_testuale, ...]}.
        """
        # 1. Prova dal catalogo (veloce, nessuna rete)
        catalog = self.load_catalog()
        if catalog and entities:
            result = {}
            mancanti = []
            for e in entities:
                if e in catalog:
                    result[e] = catalog[e].get("string", [])
                else:
                    mancanti.append(e)
            if not mancanti:
                return result
            # alcune entità non sono nel catalogo: scarica i metadata per quelle
            fetched = self._fetch_string_fields_from_metadata(mancanti)
            result.update(fetched)
            return result
        # 2. Nessun catalogo: scarica dai metadata (comportamento precedente)
        return self._fetch_string_fields_from_metadata(entities)

    def _fetch_string_fields_from_metadata(self, entities: list = None) -> dict:
        """Scarica e parsa /data/$metadata (EDMX XML) per ricavare i campi
        Edm.String. Usato quando il catalogo manca o non copre le entità.
        Nota: il metadata di F&O è grande (~15MB) ma si scarica una volta sola.
        """
        token = self.tm.get_access_token()
        if not token or not self.resource_url:
            return {}
        try:
            import requests as req
            import xml.etree.ElementTree as ET
            r = self._dyn_get(self._data_url("/$metadata"), token, timeout=120, accept="application/xml")
            if r.status_code != 200:
                return {}

            # EDMX/CSDL: namespace-agnostic parsing (i namespace variano)
            root = ET.fromstring(r.content)

            def _local(tag: str) -> str:
                return tag.rsplit("}", 1)[-1]

            # 1. Mappa EntityType -> [campi Edm.String]
            type_fields = {}
            # 2. Mappa EntitySet (nome usato nelle URL) -> EntityType
            set_to_type = {}

            for el in root.iter():
                t = _local(el.tag)
                if t == "EntityType":
                    name = el.get("Name", "")
                    if not name:
                        continue
                    str_fields = []
                    for child in el:
                        if _local(child.tag) == "Property" and \
                           child.get("Type", "") == "Edm.String":
                            fname = child.get("Name", "")
                            if fname:
                                str_fields.append(fname)
                    type_fields[name] = str_fields
                elif t == "EntitySet":
                    set_name = el.get("Name", "")
                    etype = el.get("EntityType", "")
                    if set_name and etype:
                        # EntityType e' del tipo "Namespace.TypeName" -> prendi l'ultimo
                        set_to_type[set_name] = etype.rsplit(".", 1)[-1]

            # Combina: nome dell'entity set (quello delle URL) -> campi string
            result = {}
            wanted = set(entities) if entities else None
            for set_name, type_name in set_to_type.items():
                if wanted is not None and set_name not in wanted:
                    continue
                result[set_name] = type_fields.get(type_name, [])
            return result
        except Exception:
            return {}

    def fetch_enum_fields(self, entities: list = None) -> dict:
        """Come fetch_string_fields ma cattura i campi NON-primitivi: enum e
        altri tipi (escludendo Edm.String, le date e i numerici comuni).
        Gli stati di F&O (SalesStatus, DocumentStatus, DeliveryStatus...) sono
        quasi sempre EnumType, invisibili a fetch_string_fields.

        Ritorna {entity_set_name: [(campo, tipo_breve), ...]}.
        """
        token = self.tm.get_access_token()
        if not token or not self.resource_url:
            return {}
        try:
            import requests as req
            import xml.etree.ElementTree as ET
            r = self._dyn_get(self._data_url("/$metadata"), token, timeout=120, accept="application/xml")
            if r.status_code != 200:
                return {}
            root = ET.fromstring(r.content)

            def _local(tag): return tag.rsplit("}", 1)[-1]

            # Tipi primitivi da ESCLUDERE (vogliamo solo enum e tipi "strani")
            PRIMITIVES = {
                "Edm.String", "Edm.Date", "Edm.DateTimeOffset", "Edm.Time",
                "Edm.Int32", "Edm.Int64", "Edm.Decimal", "Edm.Double",
                "Edm.Boolean", "Edm.Guid", "Edm.Binary", "Edm.Int16",
                "Edm.Byte", "Edm.Single",
            }
            type_fields = {}
            set_to_type = {}
            for el in root.iter():
                t = _local(el.tag)
                if t == "EntityType":
                    name = el.get("Name", "")
                    if not name:
                        continue
                    enum_fields = []
                    for child in el:
                        if _local(child.tag) == "Property":
                            ftype = child.get("Type", "")
                            fname = child.get("Name", "")
                            # Escludi primitivi e collezioni; tieni gli enum
                            base = ftype.replace("Collection(", "").rstrip(")")
                            if fname and base not in PRIMITIVES:
                                # tipo breve: ultimo segmento dopo il punto
                                short = base.rsplit(".", 1)[-1]
                                enum_fields.append((fname, short))
                    type_fields[name] = enum_fields
                elif t == "EntitySet":
                    sn = el.get("Name", ""); et = el.get("EntityType", "")
                    if sn and et:
                        set_to_type[sn] = et.rsplit(".", 1)[-1]

            result = {}
            wanted = set(entities) if entities else None
            for sn, tn in set_to_type.items():
                if wanted is not None and sn not in wanted:
                    continue
                result[sn] = type_fields.get(tn, [])
            return result
        except Exception:
            return {}

    def build_full_catalog(self) -> dict:
        """Scarica UNA volta il $metadata completo e costruisce il catalogo di
        TUTTE le entità con i loro campi (testo e data) E il GRAFO DELLE RELAZIONI
        reali (navigation property + referential constraint). Salva il risultato
        in un file JSON nella home (~/.chat_assistant_dyn_catalog.json) che l'app
        carica all'avvio. Multipiattaforma (usa Path.home()).

        [v2.0] Oltre a string/date, estrae per ogni entità le RELAZIONI verso
        altre entità: nome della navigation property, entità di destinazione e
        coppie di campi di aggancio (chiave locale -> chiave remota). Questo è il
        fondamento dei join affidabili: l'AI propone lungo relazioni REALI e il
        codice usa $expand quando disponibile, invece di indovinare le chiavi.

        Ritorna {"entita": {nome: {"string": [...], "date": [...],
                 "rel": [{"nav": str, "target": str, "pairs": [[loc, rem], ...]}]}},
                 "count": N, "generato": iso, "istanza": url, "versione": "2.0"}."""
        from pathlib import Path
        import json, datetime
        token = self.tm.get_access_token()
        if not token or not self.resource_url:
            return {"errore": "non autenticato o istanza non configurata"}
        try:
            import xml.etree.ElementTree as ET
            r = self._dyn_get(self._data_url("/$metadata"), token, timeout=180, accept="application/xml")
            if r.status_code != 200:
                return {"errore": f"HTTP {r.status_code} sul $metadata"}
            root = ET.fromstring(r.content)

            def _local(tag): return tag.rsplit("}", 1)[-1]
            DATE_TYPES = {"Edm.Date", "Edm.DateTimeOffset"}

            # Un solo passaggio: per ogni EntityType raccogli string, date e relazioni
            type_str, type_date, type_rel = {}, {}, {}
            set_to_type = {}
            for el in root.iter():
                t = _local(el.tag)
                if t == "EntityType":
                    name = el.get("Name", "")
                    if not name:
                        continue
                    sfs, dfs, rels = [], [], []
                    for child in el:
                        ct = _local(child.tag)
                        if ct == "Property":
                            typ = child.get("Type", "")
                            fn = child.get("Name", "")
                            if not fn:
                                continue
                            if typ == "Edm.String":
                                sfs.append(fn)
                            elif typ in DATE_TYPES:
                                dfs.append(fn)
                        elif ct == "NavigationProperty":
                            nav = child.get("Name", "")
                            typ = child.get("Type", "")  # es. Collection(Microsoft...Type) o Microsoft...Type
                            if not nav or not typ:
                                continue
                            # estrai il nome del tipo di destinazione (ultimo segmento)
                            inner = typ
                            if inner.startswith("Collection(") and inner.endswith(")"):
                                inner = inner[len("Collection("):-1]
                            target_type = inner.rsplit(".", 1)[-1]
                            # referential constraint: coppie Property/ReferencedProperty
                            pairs = []
                            for rc in child:
                                if _local(rc.tag) == "ReferentialConstraint":
                                    loc = rc.get("Property", "")
                                    rem = rc.get("ReferencedProperty", "")
                                    if loc and rem:
                                        pairs.append([loc, rem])
                            rels.append({"nav": nav, "target_type": target_type, "pairs": pairs})
                    type_str[name] = sfs
                    type_date[name] = dfs
                    type_rel[name] = rels
                elif t == "EntitySet":
                    sn = el.get("Name", ""); et = el.get("EntityType", "")
                    if sn and et:
                        set_to_type[sn] = et.rsplit(".", 1)[-1]

            # mappa inversa: tipo -> nome del set (per risolvere il target delle relazioni)
            type_to_set = {}
            for set_name, type_name in set_to_type.items():
                type_to_set.setdefault(type_name, set_name)

            entita = {}
            for set_name, type_name in set_to_type.items():
                # risolvi le relazioni: target_type -> set di destinazione
                rels_out = []
                for rel in type_rel.get(type_name, []):
                    target_set = type_to_set.get(rel["target_type"])
                    if target_set:
                        rels_out.append({
                            "nav": rel["nav"],
                            "target": target_set,
                            "pairs": rel["pairs"],
                        })
                entita[set_name] = {
                    "string": type_str.get(type_name, []),
                    "date": type_date.get(type_name, []),
                    "rel": rels_out,
                }
            n_rel = sum(len(v["rel"]) for v in entita.values())
            catalog = {
                "entita": entita,
                "count": len(entita),
                "relazioni": n_rel,
                "generato": datetime.datetime.now().isoformat(timespec="seconds"),
                "istanza": self.resource_url,
                "versione": "2.1",   # 2.1 = catalogo + schema .md per-entità generati insieme
            }
            # ── genera GLI SCHEMA .md accanto al JSON, riusando lo STESSO $metadata
            #    appena scaricato (root): nessun ri-download, nessuna copia manuale.
            #    Il planner li trova da solo (vedi _resolve_schema_dir). Se la
            #    generazione fallisce, il catalogo resta valido lo stesso.
            try:
                schema_dir = str(SCHEMA_DIR)
                n_md = self._generate_schema_md(root, schema_dir)
                self._schema_dir = schema_dir          # attivo subito, senza riavviare
                self._schema_md_cache = {}
                catalog["schema_md"] = {"cartella": schema_dir, "file": n_md}
                _dbg(f"schema .md generati: {n_md} file -> {schema_dir}")
            except Exception as _e:
                catalog["schema_md"] = {"cartella": "", "file": 0}
                _dbg(f"schema .md: generazione fallita (catalogo OK comunque): {_e}")
            path = CATALOG_FILE
            path.write_text(json.dumps(catalog, ensure_ascii=False), encoding="utf-8")
            _dbg(f"catalogo entità v{catalog['versione']} generato: {len(entita)} entità, "
                 f"{n_rel} relazioni, {catalog.get('schema_md', {}).get('file', 0)} schema .md -> {path}")
            return catalog
        except Exception as e:
            _dbg(f"build_full_catalog: errore {e}")
            return {"errore": str(e)}

    def load_catalog(self) -> dict:
        """Carica il catalogo completo da disco, se presente. Ritorna il dict
        {nome_entità: {"string": [...], "date": [...]}} oppure {} se assente.

        CACHE IN MEMORIA: il file (diversi MB, 4704 entità) viene letto e parsato
        UNA sola volta per istanza del connettore. Le chiamate successive usano la
        copia in memoria — essenziale perché _pick_field lo invoca molte volte per
        richiesta. La cache si invalida se il file cambia (controllo su mtime)."""
        from pathlib import Path
        import json
        path = CATALOG_FILE
        try:
            if not path.exists():
                self._catalog_cache = {}
                self._catalog_mtime = None
                return {}
            mtime = path.stat().st_mtime
            # Usa la cache se il file non è cambiato dall'ultima lettura
            if getattr(self, "_catalog_cache", None) is not None \
               and getattr(self, "_catalog_mtime", None) == mtime:
                return self._catalog_cache
            data = json.loads(path.read_text(encoding="utf-8"))
            self._catalog_cache = data.get("entita", {}) or {}
            self._catalog_mtime = mtime
            _dbg(f"load_catalog: caricato da disco ({len(self._catalog_cache)} entità), ora in cache")
            return self._catalog_cache
        except Exception as e:
            _dbg(f"load_catalog: errore {e}")
            return {}

    # ════════════════════════════════════════════════════════════════════════
    #  v2.0 — GRAFO DELLE RELAZIONI E SCOPERTA SEMANTICA DELLE ENTITÀ
    # ════════════════════════════════════════════════════════════════════════
    def _relations_of(self, entity: str, catalog: dict = None) -> list:
        """Ritorna le relazioni reali (da $metadata) di un'entità: lista di
        {nav, target, pairs}. Vuota se il catalogo è vecchio (senza 'rel')."""
        catalog = catalog if catalog is not None else self.load_catalog()
        ent = catalog.get(entity) or {}
        return ent.get("rel", []) or []

    def _inferred_relations_of(self, entity: str, catalog: dict = None) -> list:
        """Relazioni NON dichiarate nei metadati, dedotte dal confronto fra nomi
        campo e chiavi altrui e poi CONFERMATE misurando il join sul dato reale
        (catalogo v3.0, campo 'rel_inferite').

        In F&O moltissimi legami esistono solo come convenzione di naming: non
        c'è navigation property e non c'è foreign key nel database. Ignorarli
        lascia isole scollegate; usarli senza prove sarebbe inventare. Qui si
        usano solo quelli il cui tasso di join misurato supera la soglia.

        Restano comunque subordinati: non sostituiscono mai un arco dichiarato.
        Vuota se il catalogo è precedente alla v3.0.
        """
        if not USE_INFERRED_RELATIONS:
            return []
        catalog = catalog if catalog is not None else self.load_catalog()
        ent = catalog.get(entity) or {}
        return [r for r in (ent.get("rel_inferite") or [])
                if (r.get("tasso") or 0) >= INFERRED_MIN_RATE]

    def _find_relation(self, ent_a: str, ent_b: str, catalog: dict = None) -> dict:
        """Cerca una relazione DIRETTA tra due entità (in entrambe le direzioni).
        Ritorna {nav, target, pairs, direction} oppure {} se non esiste un arco
        reale. 'direction' = 'a_to_b' se la nav è su A verso B, 'b_to_a' altrimenti.
        È la verità di riferimento che rende sicuri i join: se l'arco non esiste,
        il codice NON inventa l'aggancio."""
        catalog = catalog if catalog is not None else self.load_catalog()
        for rel in self._relations_of(ent_a, catalog):
            if rel.get("target") == ent_b:
                return {**rel, "direction": "a_to_b"}
        for rel in self._relations_of(ent_b, catalog):
            if rel.get("target") == ent_a:
                return {**rel, "direction": "b_to_a"}
        # Nessun arco dichiarato: si ricade sulle relazioni dedotte e verificate.
        # L'ordine conta — i metadati hanno sempre la precedenza.
        for rel in self._inferred_relations_of(ent_a, catalog):
            if rel.get("target") == ent_b:
                return {**rel, "direction": "a_to_b", "inferita": True}
        for rel in self._inferred_relations_of(ent_b, catalog):
            if rel.get("target") == ent_a:
                return {**rel, "direction": "b_to_a", "inferita": True}
        return {}

    def _relations_summary(self, entities: list, catalog: dict = None) -> str:
        """Produce un riassunto leggibile delle relazioni TRA le entità candidate,
        da passare all'AI così che proponga join lungo archi REALI. Solo archi
        interni all'insieme candidato (per non gonfiare il prompt)."""
        catalog = catalog if catalog is not None else self.load_catalog()
        cand = set(entities)
        righe = []
        visti = set()
        for e in entities:
            for rel in self._relations_of(e, catalog):
                tgt = rel.get("target")
                if tgt in cand and tgt != e:
                    key = tuple(sorted([e, tgt]) + [rel.get("nav", "")])
                    if key in visti:
                        continue
                    visti.add(key)
                    pairs = rel.get("pairs") or []
                    if pairs:
                        coppie = ", ".join(f"{e}.{lo}={tgt}.{re_}" for lo, re_ in pairs)
                        righe.append(f"{e} ⋈ {tgt} (via {rel['nav']}): {coppie}")
                    else:
                        righe.append(f"{e} ⋈ {tgt} (via {rel['nav']}, chiave non dichiarata)")
        # Archi dedotti e confermati sul dato: elencati DOPO i dichiarati e
        # marcati, così il modello sa che sono deduzioni misurate e non
        # navigation property. Senza questo elenco non verrebbero mai proposti,
        # e la ricaduta in _find_relation resterebbe lettera morta.
        for e in entities:
            for rel in self._inferred_relations_of(e, catalog):
                tgt = rel.get("target")
                if tgt not in cand or tgt == e:
                    continue
                key = tuple(sorted([e, tgt]) + [rel.get("nav", "")])
                if key in visti:
                    continue
                visti.add(key)
                pairs = rel.get("pairs") or []
                if not pairs:
                    continue
                coppie = ", ".join(f"{e}.{lo}={tgt}.{re_}" for lo, re_ in pairs)
                righe.append(f"{e} ⋈ {tgt} ({coppie}) [dedotto, join verificato "
                             f"al {rel.get('tasso')}% sul dato reale]")
        return "\n".join(righe)

    def _semantic_candidates(self, query: str, full_catalog: dict, top_k: int = 15) -> list:
        """v2.0 — SCOPERTA SEMANTICA: usa embeddings + indice vettoriale (la stessa
        tecnologia del modulo ChromaDB) per trovare le entità semanticamente più
        vicine alla domanda, anche quando il NOME non contiene la parola chiave.
        Complementa (non sostituisce) la scrematura per parola chiave.

        Ritorna una lista ordinata di nomi entità, o [] se le librerie non sono
        disponibili (in tal caso il chiamante resta sulla sola ricerca lessicale).
        L'indice è costruito UNA volta per istanza del catalogo e tenuto in cache."""
        try:
            res = _semantic_ensure(full_catalog, self.cfg)
            if not res:
                return []  # librerie/modello assenti: solo ricerca lessicale
            names, matrix, model = res
            q = model.encode([query], normalize_embeddings=True)[0]
            sims = matrix @ q  # cosine (vettori normalizzati)
            idx = sims.argsort()[::-1][:top_k]
            return [names[i] for i in idx]
        except Exception as e:
            _dbg(f"semantic: errore ({e}), uso solo ricerca lessicale")
            return []

    def verify_overdue_relations(self) -> dict:
        """v2.0 — VERIFICA gli agganci del MODULO 1 (ordini in ritardo) contro il
        GRAFO DELLE RELAZIONI reali del catalogo. Per ciascun join usato dal modulo
        controlla: (a) che le entità esistano, (b) che i campi di aggancio esistano,
        (c) se esiste una RELAZIONE dichiarata in $metadata che corrobora l'aggancio,
        e in tal caso quale chiave userebbe. Non modifica nulla: produce un rapporto
        diagnostico (usato anche dal log all'avvio di find_overdue).

        Ritorna {sales_entity, joins:[{nome, ok, dettaglio, relazione_reale, chiavi_reali}], note:[...]}.
        """
        cat = self.load_catalog()
        rep = {"sales_entity": None, "joins": [], "note": []}
        if not cat:
            rep["note"].append("Catalogo assente: impossibile verificare le relazioni. Rigenera il catalogo (v2.0).")
            return rep
        sales = next((e for e in _OVERDUE_SALES_ENTITIES if e in cat), None)
        rep["sales_entity"] = sales
        if not sales:
            rep["note"].append("Nessuna entità righe-vendita nota trovata nel catalogo.")
            return rep
        has_rel = "rel" in (cat.get(sales) or {})
        if not has_rel:
            rep["note"].append("Il catalogo è di una versione precedente alla 2.0 (senza relazioni). "
                               "Rigeneralo per verificare gli agganci contro le relazioni reali.")

        # JOIN A — pianificazione: PlannedOrders agganciata per ARTICOLO (ProductNumber)
        planned = next((e for e in _PLANNED_ORDER_ENTITIES if e in cat), None)
        ja = {"nome": "ordini in ritardo → PlannedOrders (per articolo)", "ok": False,
              "relazione_reale": None, "chiavi_reali": None, "dettaglio": ""}
        if not planned:
            ja["dettaglio"] = "PlannedOrders non presente nel catalogo."
        else:
            pf = self._pick_field(planned, _PRODUCT_KEY_FIELDS, "string")
            ja["ok"] = bool(pf)
            ja["dettaglio"] = (f"aggancio per campo articolo '{pf}' su {planned}." if pf
                               else f"nessun campo articolo trovato su {planned}.")
            rel = self._find_relation(sales, planned, cat)
            if rel:
                ja["relazione_reale"] = rel.get("nav")
                ja["chiavi_reali"] = rel.get("pairs")
            pl_fields = set(cat[planned].get("string", []))
            ref_ordine = [f for f in pl_fields if ("salesorder" in f.lower() or f.lower() in
                          ("refid", "referencenumber", "reference", "peggingreferenceid"))]
            sales_rel = [r for r in (cat[planned].get("rel") or [])
                         if "sales" in (r.get("target", "") or "").lower()]
            if ref_ordine:
                ja["dettaglio"] += (f" ATTENZIONE: {planned} espone anche campi che paiono riferirsi "
                                    f"all'ordine/pegging ({', '.join(ref_ordine[:4])}): un aggancio "
                                    f"per ORDINE sarebbe più preciso del solo articolo.")
            elif not sales_rel:
                ja["dettaglio"] += (f" Confermato: {planned} non espone né un campo né una relazione "
                                    f"verso l'ordine di vendita — l'aggancio per articolo è il massimo "
                                    f"possibile con questa entità. Il modulo segnala come 'da verificare' "
                                    f"le righe il cui articolo è in ritardo su più ordini.")
        rep["joins"].append(ja)

        # JOIN B — produzione: ProductionOrderHeaders per DemandSalesOrderNumber
        prod = next((e for e in [_OVERDUE_PRODUCTION_ENTITY, "ProductionOrderHeaders", "ProdTable"] if e in cat), None)
        jb = {"nome": "ordini in ritardo → ProductionOrderHeaders (per ordine di vendita)", "ok": False,
              "relazione_reale": None, "chiavi_reali": None, "dettaglio": ""}
        if not prod:
            jb["dettaglio"] = "ProductionOrderHeaders non presente nel catalogo."
        else:
            demand = self._pick_field(prod, ["DemandSalesOrderNumber", "SalesId"], "string")
            jb["ok"] = bool(demand)
            jb["dettaglio"] = (f"aggancio per '{demand}' su {prod}." if demand
                               else f"nessun campo legame-vendita trovato su {prod}.")
            rel = self._find_relation(sales, prod, cat)
            if rel:
                jb["relazione_reale"] = rel.get("nav")
                jb["chiavi_reali"] = rel.get("pairs")
        rep["joins"].append(jb)
        return rep

    def fetch_date_fields(self, entities: list = None) -> dict:
        """Ricava i campi data (Edm.Date, Edm.DateTimeOffset) per le entità.
        FONTE PRIMARIA: il catalogo completo su disco; metadata solo se manca.
        """
        catalog = self.load_catalog()
        if catalog and entities:
            result = {}
            mancanti = []
            for e in entities:
                if e in catalog:
                    result[e] = catalog[e].get("date", [])
                else:
                    mancanti.append(e)
            if not mancanti:
                return result
            result.update(self._fetch_date_fields_from_metadata(mancanti))
            return result
        return self._fetch_date_fields_from_metadata(entities)

    def _fetch_date_fields_from_metadata(self, entities: list = None) -> dict:
        """Scarica i metadata ed estrae i campi data. Usato quando il catalogo
        manca o non copre le entità richieste."""
        token = self.tm.get_access_token()
        if not token or not self.resource_url:
            return {}
        try:
            import requests as req
            import xml.etree.ElementTree as ET
            r = self._dyn_get(self._data_url("/$metadata"), token, timeout=120, accept="application/xml")
            if r.status_code != 200:
                return {}
            root = ET.fromstring(r.content)

            def _local(tag): return tag.rsplit("}", 1)[-1]

            type_fields = {}
            set_to_type = {}
            DATE_TYPES = {"Edm.Date", "Edm.DateTimeOffset"}
            for el in root.iter():
                t = _local(el.tag)
                if t == "EntityType":
                    name = el.get("Name", "")
                    if not name: continue
                    df = []
                    for child in el:
                        if _local(child.tag) == "Property" and child.get("Type", "") in DATE_TYPES:
                            fn = child.get("Name", "")
                            if fn: df.append(fn)
                    type_fields[name] = df
                elif t == "EntitySet":
                    sn = el.get("Name", ""); et = el.get("EntityType", "")
                    if sn and et:
                        set_to_type[sn] = et.rsplit(".", 1)[-1]

            result = {}
            wanted = set(entities) if entities else None
            for sn, tn in set_to_type.items():
                if wanted is not None and sn not in wanted:
                    continue
                result[sn] = type_fields.get(tn, [])
            return result
        except Exception:
            return {}

    def _pick_date_field(self, entity: str) -> str:
        """Sceglie il campo data più appropriato per filtri temporali.
        Preferisce nomi 'noti' (OrderDate, PurchOrderDate...); se non ce
        n'è uno, prende il primo campo data disponibile."""
        cache = self.cfg.get("dyn_date_fields", {}) or {}
        fields = cache.get(entity)
        if fields is None:
            fetched = self.fetch_date_fields([entity])
            fields = fetched.get(entity, [])
            # Aggiorna la cache (chi chiama dovrebbe poi salvare cfg, ma
            # almeno la prossima query nella sessione la trova).
            cache[entity] = fields
            self.cfg["dyn_date_fields"] = cache
        if not fields:
            return ""
        for preferred in _COMMON_DATE_FIELDS:
            if preferred in fields:
                return preferred
        return fields[0]

    def _pick_requester_field(self, entity: str) -> str:
        """Sceglie il campo che identifica il richiedente/autore nell'entità.
        Cerca tra i campi stringa dell'entità il primo che corrisponde ai
        nomi noti (_COMMON_REQUESTER_FIELDS). Ritorna '' se non trovato.
        """
        string_fields = (self.cfg.get("dyn_string_fields", {}) or {}).get(entity)
        if string_fields is None:
            fetched = self.fetch_string_fields([entity])
            string_fields = fetched.get(entity, [])
            cache = dict(self.cfg.get("dyn_string_fields", {}) or {})
            cache[entity] = string_fields
            self.cfg["dyn_string_fields"] = cache
        if not string_fields:
            return ""
        # Match esatto sui nomi noti
        for preferred in _COMMON_REQUESTER_FIELDS:
            if preferred in string_fields:
                return preferred
        return ""

    def _pick_vendor_fields(self, entity: str):
        """Sceglie i campi codice/nome fornitore per l'entità.
        Ritorna (codice, nome) o ('','')."""
        string_fields = (self.cfg.get("dyn_string_fields", {}) or {}).get(entity, [])
        code = next((f for f in _COMMON_VENDOR_CODE_FIELDS if f in string_fields), "")
        name = next((f for f in _COMMON_VENDOR_NAME_FIELDS if f in string_fields), "")
        return (code, name)

    def resolve_person_to_personnel_number(self, full_name: str):
        """Risolve un nome ('Mario Rossi') nel suo Personnel Number F&O.

        Interroga la prima entità anagrafica disponibile tra quelle
        selezionate dall'utente (Workers, Employees, …). Cerca:
        1. match esatto su un campo nome completo (Name/FullName/PersonName),
        2. match per nome+cognome composti,
        3. match per contains su FirstName/LastName (fallback più ampio).

        Ritorna (personnel_number, matched_entity, matched_name) oppure
        (None, None, "messaggio_diagnostico").
        """
        token = self.tm.get_access_token()
        if not token or not full_name:
            return (None, None, "")

        # Trova un'entità Worker tra quelle configurate
        configured = self.cfg.get("dyn_entities", []) or []
        if isinstance(configured, str):
            configured = [e.strip() for e in configured.splitlines() if e.strip()]
        configured = [e.split(":", 1)[0].strip() if ":" in e else e.strip()
                      for e in configured if e.strip()]
        worker_entity = next((e for e in configured if e in _WORKER_ENTITIES), None)
        if not worker_entity:
            return (None, None,
                    "nessuna entità Worker/Employees nelle entità configurate "
                    "(aggiungine una in Impostazioni → Seleziona entità)")

        # Scopri quali campi nome/PN ha quell'entità
        sfields = (self.cfg.get("dyn_string_fields", {}) or {}).get(worker_entity)
        if sfields is None:
            fetched = self.fetch_string_fields([worker_entity])
            sfields = fetched.get(worker_entity, [])
        if not sfields:
            return (None, None, f"impossibile leggere i campi dell'entità {worker_entity}")

        pn_field = next((f for f in _WORKER_PN_FIELDS if f in sfields), None)
        if not pn_field:
            return (None, None,
                    f"l'entità {worker_entity} non espone un Personnel Number "
                    f"(campi cercati: {', '.join(_WORKER_PN_FIELDS)})")

        full_field  = next((f for f in _WORKER_NAME_FIELDS  if f in sfields), None)
        first_field = next((f for f in _WORKER_FIRST_FIELDS if f in sfields), None)
        last_field  = next((f for f in _WORKER_LAST_FIELDS  if f in sfields), None)

        safe_name = full_name.replace("'", "''")
        clauses = []
        if full_field:
            # match esatto e contains (per gestire "Bonometti Marco" vs "Marco Bonometti")
            clauses.append(f"{full_field} eq '{safe_name}'")
            clauses.append(f"contains({full_field},'{safe_name}')")
            # variante con ordine invertito
            tokens = full_name.split()
            if len(tokens) >= 2:
                inverted = " ".join(tokens[::-1]).replace("'", "''")
                clauses.append(f"{full_field} eq '{inverted}'")
        if first_field and last_field:
            tokens = full_name.split()
            if len(tokens) >= 2:
                first = tokens[0].replace("'", "''")
                last  = " ".join(tokens[1:]).replace("'", "''")
                clauses.append(f"({first_field} eq '{first}' and {last_field} eq '{last}')")
                # variante: forse l'utente ha scritto "Cognome Nome"
                clauses.append(f"({first_field} eq '{last}' and {last_field} eq '{first}')")

        if not clauses:
            return (None, None,
                    f"l'entità {worker_entity} non ha campi nome riconoscibili")

        filt = " or ".join(clauses)
        # $select per ridurre la risposta a soli campi utili
        select_fields = [pn_field]
        if full_field: select_fields.append(full_field)
        if first_field: select_fields.append(first_field)
        if last_field: select_fields.append(last_field)
        params = [
            f"$filter={quote(filt)}",
            f"$select={','.join(select_fields)}",
            "$top=5",
        ]
        if self.cfg.get("dyn_cross_company", False):
            params.append("cross-company=true")
        url = self._data_url(f"/{worker_entity}?" + "&".join(params))

        try:
            import requests as req
            r = self._dyn_get(url, token, timeout=20)
            if r.status_code != 200:
                return (None, None, f"HTTP {r.status_code} su {worker_entity}")
            records = r.json().get("value", [])
            if not records:
                return (None, None, f"nessun dipendente trovato con nome '{full_name}'")
            rec = records[0]
            pn = rec.get(pn_field, "")
            display = rec.get(full_field, full_name) if full_field else full_name
            note = ""
            if len(records) > 1:
                note = f" (trovati {len(records)} omonimi, uso il primo: {display})"
            return (pn, worker_entity, display + note)
        except Exception as e:
            return (None, None, f"errore risoluzione: {e}")

    def count_records(self, query: str, current_user_name: str = "") -> str:
        """Esegue una ricerca di tipo CONTEGGIO sulle entità configurate.

        Si attiva quando la domanda contiene parole come 'quanti', 'numero di',
        'totale', ecc. Applica (se rilevati dalla domanda) i filtri:
        - temporale  ("nel 2026", "ultimi 30 giorni", …)
        - per persona ("di Mario Rossi", "mie RdA" → usa current_user_name)
        - per fornitore ("del fornitore ACME")

        Costruisce per ogni entità una URL OData del tipo:
            /<Entità>/$count?$filter=<clausole AND>
        e legge il numero restituito (intero in plain text).
        """
        token = self.tm.get_access_token()
        if not token or not self.resource_url:
            return ""
        entities_cfg = self.cfg.get("dyn_entities", []) or []
        if isinstance(entities_cfg, str):
            entities_cfg = [e.strip() for e in entities_cfg.splitlines() if e.strip()]
        entities = [e.split(":", 1)[0].strip() if ":" in e else e.strip()
                    for e in entities_cfg if e.strip()]
        if not entities:
            return ""

        start_iso, end_iso, date_label = _detect_date_range(query)
        person_name, is_self = _detect_person_filter(query, current_user_name)
        vendor_term = _detect_vendor_filter(query)
        cross_company = bool(self.cfg.get("dyn_cross_company", False))

        # Risolvi una sola volta il Personnel Number (vale per tutte le entità)
        person_pn = None
        person_display = None
        person_error = None
        if person_name:
            person_pn, _, person_display = self.resolve_person_to_personnel_number(person_name)
            if not person_pn:
                person_error = person_display  # in caso di errore contiene il messaggio
                person_display = None

        parts = []
        # Header diagnostico se è stato chiesto un filtro persona non risolto
        if person_name and not person_pn:
            tag = "mie" if is_self else f"di {person_name}"
            parts.append(
                f"[D365 — AVVISO] non sono riuscito a risolvere la persona ({tag}): "
                f"{person_error or 'sconosciuto'}. Il conteggio sotto NON è filtrato per persona."
            )

        for entity in entities:
            try:
                clauses = []
                used_date_field = None
                used_requester_field = None
                used_vendor_field = None

                # 1. Filtro temporale
                if start_iso and end_iso:
                    date_field = self._pick_date_field(entity)
                    if date_field:
                        used_date_field = date_field
                        clauses.append(f"{date_field} ge {start_iso} and {date_field} le {end_iso}")

                # 2. Filtro persona (solo se PN risolto)
                if person_pn:
                    req_field = self._pick_requester_field(entity)
                    if req_field:
                        used_requester_field = req_field
                        safe_pn = person_pn.replace("'", "''")
                        clauses.append(f"{req_field} eq '{safe_pn}'")

                # 3. Filtro fornitore
                if vendor_term:
                    code_field, name_field = self._pick_vendor_fields(entity)
                    safe_v = vendor_term.replace("'", "''")
                    vendor_clauses = []
                    if code_field:
                        vendor_clauses.append(f"{code_field} eq '{safe_v}'")
                    if name_field:
                        vendor_clauses.append(f"contains({name_field},'{safe_v}')")
                    if vendor_clauses:
                        used_vendor_field = name_field or code_field
                        clauses.append("(" + " or ".join(vendor_clauses) + ")")

                params = []
                clauses_full = list(clauses)
                if clauses:
                    params.append(f"$filter={quote(' and '.join(clauses))}")
                if cross_company:
                    params.append("cross-company=true")
                qs = ("?" + "&".join(params)) if params else ""
                url = self._data_url(f"/{entity}/$count{qs}")

                import requests as req
                r = self._dyn_get(url, token, timeout=25, accept="text/plain")

                if r.status_code == 401:
                    new_token = self.tm._refresh(
                        self.tm._token_data.get("refresh_token", ""))
                    if new_token:
                        r = self._dyn_get(url, new_token, timeout=25, accept="text/plain")

                # Se 400 e c'era un filtro data, ritenta senza la clausola
                # data: alcune entità F&O non supportano filter su DateTimeOffset.
                date_filter_removed = False
                if r.status_code == 400 and used_date_field:
                    # Ricostruisco le clausole escludendo quella della data
                    other_clauses = [c for c in clauses_full
                                     if not c.startswith(f"{used_date_field} ")]
                    retry_params = []
                    if other_clauses:
                        retry_params.append(f"$filter={quote(' and '.join(other_clauses))}")
                    if cross_company:
                        retry_params.append("cross-company=true")
                    retry_qs = ("?" + "&".join(retry_params)) if retry_params else ""
                    retry_url = self._data_url(f"/{entity}/$count{retry_qs}")
                    r = self._dyn_get(retry_url, token, timeout=25, accept="text/plain")
                    if r.status_code == 200:
                        date_filter_removed = True

                if r.status_code != 200:
                    parts.append(f"[D365 {entity}] errore HTTP {r.status_code} sul conteggio")
                    continue
                try:
                    n = int((r.text or "").strip())
                except ValueError:
                    parts.append(f"[D365 {entity}] risposta inattesa: {r.text[:80]!r}")
                    continue

                # Costruisco la descrizione dei filtri applicati
                desc_bits = []
                if used_date_field and date_label and not date_filter_removed:
                    desc_bits.append(f"periodo {date_label}")
                elif date_filter_removed and date_label:
                    desc_bits.append(f"⚠ filtro periodo '{date_label}' rimosso (non supportato da {entity})")
                if used_requester_field and person_display:
                    desc_bits.append(f"richiedente {person_display}")
                elif person_pn and not used_requester_field:
                    desc_bits.append(f"⚠ richiedente {person_display} ignorato (campo non disponibile in {entity})")
                if used_vendor_field and vendor_term:
                    desc_bits.append(f"fornitore '{vendor_term}'")
                elif vendor_term and not used_vendor_field:
                    desc_bits.append(f"⚠ fornitore '{vendor_term}' ignorato (campi non disponibili in {entity})")

                if desc_bits:
                    parts.append(f"[D365 — CONTEGGIO {entity}] {n} record con filtri: {'; '.join(desc_bits)}.")
                else:
                    parts.append(f"[D365 — CONTEGGIO {entity}] {n} record totali.")
            except Exception as e:
                parts.append(f"[D365 {entity}] errore conteggio: {e}")
                continue

        return "\n\n".join(parts)

    def list_records(self, query: str, top: int = 10, current_user_name: str = "") -> str:
        """Restituisce un elenco di record recenti per le entità configurate.

        Usata per richieste tipo "mostrami le mie RdA recenti" / "ultimi 5 ordini".
        Applica gli stessi filtri di count_records (persona, fornitore, data) ma
        invece di fare /$count interroga l'entità con $top + $orderby DESC sul
        campo data principale, ritornando i record veri.
        """
        try:
            top = max(1, min(int(top), 100))
        except (ValueError, TypeError):
            top = 10
        token = self.tm.get_access_token()
        if not token or not self.resource_url:
            return ""
        entities_cfg = self.cfg.get("dyn_entities", []) or []
        if isinstance(entities_cfg, str):
            entities_cfg = [e.strip() for e in entities_cfg.splitlines() if e.strip()]
        entities = [e.split(":", 1)[0].strip() if ":" in e else e.strip()
                    for e in entities_cfg if e.strip()]
        if not entities:
            return ""

        start_iso, end_iso, date_label = _detect_date_range(query)
        person_name, is_self = _detect_person_filter(query, current_user_name)
        vendor_term = _detect_vendor_filter(query)
        cross_company = bool(self.cfg.get("dyn_cross_company", False))

        person_pn = None
        person_display = None
        person_error = None
        if person_name:
            person_pn, _, person_display = self.resolve_person_to_personnel_number(person_name)
            if not person_pn:
                person_error = person_display
                person_display = None

        parts = []
        list_errors = []
        empty_entities = []
        html_records = []   # per il report HTML cliccabile
        html_titolo = ""
        if person_name and not person_pn:
            tag = "mie" if is_self else f"di {person_name}"
            parts.append(
                f"[D365 — AVVISO] non sono riuscito a risolvere la persona ({tag}): "
                f"{person_error or 'sconosciuto'}. La lista NON è filtrata per persona."
            )

        # Salta entità di sistema (Workers, etc.) per le richieste di lista —
        # l'utente vuole RdA/ordini/fatture, non l'anagrafica dipendenti.
        skip_for_list = set(_WORKER_ENTITIES)
        target_entities = [e for e in entities if e not in skip_for_list] or entities
        _dbg(f"list_records: entità da interrogare = {target_entities}")

        for entity in target_entities:
            try:
                clauses = []
                used_requester_field = None
                used_vendor_field = None
                date_field = self._pick_date_field(entity)

                if start_iso and end_iso and date_field:
                    clauses.append(f"{date_field} ge {start_iso} and {date_field} le {end_iso}")

                if person_pn:
                    req_field = self._pick_requester_field(entity)
                    if req_field:
                        used_requester_field = req_field
                        safe_pn = person_pn.replace("'", "''")
                        clauses.append(f"{req_field} eq '{safe_pn}'")

                if vendor_term:
                    code_field, name_field = self._pick_vendor_fields(entity)
                    safe_v = vendor_term.replace("'", "''")
                    vclauses = []
                    if code_field:
                        vclauses.append(f"{code_field} eq '{safe_v}'")
                    if name_field:
                        vclauses.append(f"contains({name_field},'{safe_v}')")
                    if vclauses:
                        used_vendor_field = name_field or code_field
                        clauses.append("(" + " or ".join(vclauses) + ")")

                params = [f"$top={top}"]
                if clauses:
                    params.append(f"$filter={quote(' and '.join(clauses))}")
                if cross_company:
                    params.append("cross-company=true")
                # L'orderby è separato perché alcune entità F&O non lo
                # supportano sui campi data: se la query con orderby fa 400,
                # ritentiamo senza.
                params_with_order = list(params)
                if date_field:
                    params_with_order.append(f"$orderby={quote(date_field + ' desc')}")
                url = self._data_url(f"/{entity}?" + "&".join(params_with_order))

                import requests as req
                r = self._dyn_get(url, token, timeout=25)

                if r.status_code == 401:
                    new_token = self.tm._refresh(
                        self.tm._token_data.get("refresh_token", ""))
                    if new_token:
                        r = self._dyn_get(url, new_token, timeout=25)

                # Se 400 e c'era un orderby, ritenta senza orderby (alcune
                # entità F&O non supportano orderby sui campi data).
                if r.status_code == 400 and date_field:
                    url_no_order = self._data_url(f"/{entity}?" + "&".join(params))
                    r = self._dyn_get(url_no_order, token, timeout=25)

                if r.status_code != 200:
                    list_errors.append((entity, r.status_code))
                    _dbg(f"list_records: {entity} -> HTTP {r.status_code}: {r.text[:200]}")
                    continue

                records = r.json().get("value", [])
                _dbg(f"list_records: {entity} -> HTTP 200, {len(records)} record")
                if not records:
                    # "nessun record" non è un errore: lo segnaliamo solo se
                    # non troviamo dati da nessuna parte (gestito in fondo).
                    empty_entities.append(entity)
                    continue

                # Descrizione filtri applicati
                desc_bits = []
                if start_iso and end_iso and date_field: desc_bits.append(f"periodo {date_label}")
                if used_requester_field and person_display: desc_bits.append(f"di {person_display}")
                if used_vendor_field and vendor_term: desc_bits.append(f"fornitore '{vendor_term}'")
                header = f"[D365 — LISTA {entity}] {len(records)} record"
                if desc_bits:
                    header += " (" + "; ".join(desc_bits) + ")"
                header += ":"
                parts.append(header + "\n" + self._format_records_list(records))
                html_records.extend(records)
                html_titolo = html_titolo or f"Lista {entity}"
            except Exception as e:
                list_errors.append((entity, str(e)))
                continue

        # Se ci sono dati, dominano; gli errori diventano nota discreta.
        combined = self._combine_results(parts, list_errors)
        if combined:
            # Report HTML cliccabile (per qualsiasi lista, non solo modulo ordini)
            html_path = self._write_generic_html(html_titolo or "Risultati Dynamics", html_records)
            if html_path:
                combined = f"[REPORT_HTML: {html_path}]\n" + combined
            return combined
        # Nessun dato e nessun errore: tutte le entità erano vuote
        if empty_entities:
            return ("Nessun record trovato con i filtri richiesti "
                    f"(entità interrogate: {', '.join(empty_entities)}). "
                    "I criteri potrebbero essere troppo restrittivi, oppure non "
                    "ci sono record corrispondenti.")
        return ""

    def _format_records_list(self, records: list, preferiti: list = None) -> str:
        """Formatta i record per una richiesta di lista: una riga per record,
        solo i campi non-null e non-interni.

        I campi esplicitamente richiesti vengono per primi: prendere i primi N
        nell'ordine del payload significa perdere proprio quelli su cui verte
        la domanda, se sono lontani nello schema dell'entità."""
        pref = [c for c in (preferiti or [])]
        lines = []
        for rec in records:
            fields = {k: v for k, v in rec.items()
                      if not k.startswith("@") and v not in (None, "", [])}
            ordinati = ([(k, fields[k]) for k in pref if k in fields]
                        + [(k, v) for k, v in fields.items() if k not in pref])
            shown = ordinati[:12]
            row = "; ".join(f"{k}={v}" for k, v in shown)
            lines.append(f"  - {row}")
        return "\n".join(lines)

    def _pick_field(self, entity: str, candidates: list, kind: str = "string") -> str:
        """Sceglie il primo campo presente nell'entità tra una lista di
        candidati. kind='string' usa la cache dei campi testuali, 'date' i
        campi data, 'enum' i campi enum."""
        if kind == "date":
            avail = (self.cfg.get("dyn_date_fields", {}) or {}).get(entity)
            if avail is None:
                avail = self.fetch_date_fields([entity]).get(entity, [])
        elif kind == "enum":
            fetched = self.fetch_enum_fields([entity]).get(entity, [])
            avail = [f for f, _t in fetched]
        else:
            avail = (self.cfg.get("dyn_string_fields", {}) or {}).get(entity)
            if avail is None:
                avail = self.fetch_string_fields([entity]).get(entity, [])
        avail = avail or []
        for c in candidates:
            if c in avail:
                return c
        return ""

    # ╔══════════════════════════════════════════════════════════════════════╗
    # ║  MODULO: VERIFICA ORDINI IN RITARDO                      [INIZIO 2/2]  ║
    # ║  Metodi della classe per il calcolo e i join deterministici.          ║
    # ║  Entità usate: CDSSalesOrderLinesV2 (base), PlannedOrders (+nuova      ║
    # ║  data/acquisto reale), ProductionOrderHeaders (+produzione reale).    ║
    # ║  Catena: find_overdue → _planning_map_for_products +                  ║
    # ║          _production_orders_for_sales → _write_overdue_html.          ║
    # ╚══════════════════════════════════════════════════════════════════════╝
    def find_overdue(self, query: str, top: int = 20, current_user_name: str = "",
                     with_planning: bool = False) -> str:
        """Trova ordini/righe IN RITARDO: data di consegna confermata già
        passata E stato ancora 'aperto' (Backorder).

        Per ogni entità configurata che abbia sia un campo data-consegna sia
        un campo stato evasione, costruisce:
            <dataConsegna> lt <oggi> and <stato> eq SalesStatus'Backorder'
        Gli enum F&O nel filtro richiedono il prefisso del tipo; se il primo
        tentativo fallisce (400), riprova col valore nudo.

        Se with_planning=True, dopo aver trovato gli ordini in ritardo aggancia
        per numero articolo gli ordini pianificati (PlannedOrders) per fornire
        la nuova data di consegna pianificata (join deterministico).
        """
        token = self.tm.get_access_token()
        if not token or not self.resource_url:
            return ""
        try:
            top = max(1, min(int(top), 100))
        except (ValueError, TypeError):
            top = 20

        # MODULO DETERMINISTICO: usa l'entità righe-vendita FISSA, non l'elenco
        # caricato. Prende la prima delle entità note effettivamente esistente
        # (verificata sul catalogo se presente, altrimenti si prova in ordine).
        catalog = self.load_catalog()
        if catalog:
            candidates = [e for e in _OVERDUE_SALES_ENTITIES if e in catalog][:1]
        else:
            candidates = _OVERDUE_SALES_ENTITIES[:1]
        if not candidates:
            candidates = [_OVERDUE_SALES_ENTITIES[0]]

        today = datetime.date.today().isoformat()
        person_name, is_self = _detect_person_filter(query, current_user_name)
        vendor_term = _detect_vendor_filter(query)
        cross_company = bool(self.cfg.get("dyn_cross_company", False))

        person_pn = None
        if person_name:
            person_pn, _, _ = self.resolve_person_to_personnel_number(person_name)

        parts = []
        errors = []
        empty = []
        overdue_products = set()
        overdue_rows = []
        _dbg(f"find_overdue: entità candidate = {candidates}, oggi={today}, planning={with_planning}")

        # [v2.0] VERIFICA degli agganci del modulo contro il grafo delle relazioni
        # reali: logga se i join (pianificazione per articolo, produzione per ordine)
        # sono corroborati da una relazione dichiarata in $metadata. Diagnostica
        # non bloccante: aiuta a capire se un aggancio è "per nome di campo" o reale.
        if with_planning:
            try:
                vr = self.verify_overdue_relations()
                for j in vr.get("joins", []):
                    rel = j.get("relazione_reale")
                    _dbg(f"verify modulo1: {j['nome']} -> {'OK' if j['ok'] else 'NO'}; "
                         f"{j['dettaglio']}"
                         + (f" [relazione reale: {rel} chiavi={j.get('chiavi_reali')}]" if rel
                            else " [nessuna relazione reale dichiarata: join per valore]"))
                for n in vr.get("note", []):
                    _dbg(f"verify modulo1 NOTA: {n}")
            except Exception as _e:
                _dbg(f"verify modulo1: errore {_e}")

        for entity in candidates:
            date_field = self._pick_field(entity, _DELIVERY_DATE_FIELDS, "date")
            status_field = self._pick_field(entity, _LINE_STATUS_FIELDS, "enum")
            if not date_field:
                _dbg(f"find_overdue: {entity} saltata (nessun campo data consegna)")
                continue

            base_clauses = [f"{date_field} lt {today}"]
            # filtro stato "aperto" se disponibile
            status_clause_variants = []
            if status_field:
                for val in _OPEN_STATUS_VALUES:
                    status_clause_variants.append(
                        f"{status_field} eq {_SALESSTATUS_ENUM_PREFIX}'{val}'")
            # filtro persona (se risolto e l'entità ha un campo richiedente)
            if person_pn:
                req_field = self._pick_requester_field(entity)
                if req_field:
                    safe_pn = person_pn.replace("'", "''")
                    base_clauses.append(f"{req_field} eq '{safe_pn}'")
            # filtro fornitore
            if vendor_term:
                code_field, name_field = self._pick_vendor_fields(entity)
                safe_v = vendor_term.replace("'", "''")
                vclauses = []
                if code_field: vclauses.append(f"{code_field} eq '{safe_v}'")
                if name_field: vclauses.append(f"contains({name_field},'{safe_v}')")
                if vclauses:
                    base_clauses.append("(" + " or ".join(vclauses) + ")")

            def _run(filter_str):
                params = [f"$filter={quote(filter_str)}", f"$top={top}"]
                if cross_company:
                    params.append("cross-company=true")
                url = self._data_url(f"/{entity}?" + "&".join(params))
                import requests as req
                return self._dyn_get(url, token, timeout=30)

            try:
                # Tentativo 1: con stato Backorder (enum col prefisso)
                if status_clause_variants:
                    filt = " and ".join(base_clauses + ["(" + " or ".join(status_clause_variants) + ")"])
                else:
                    filt = " and ".join(base_clauses)
                r = _run(filt)
                _dbg(f"find_overdue: {entity} filtro1 -> HTTP {r.status_code}")

                # Se 400 e c'era il filtro stato enum, riprova col valore nudo
                if r.status_code == 400 and status_field:
                    nude = [f"{status_field} eq '{v}'" for v in _OPEN_STATUS_VALUES]
                    filt2 = " and ".join(base_clauses + ["(" + " or ".join(nude) + ")"])
                    r = _run(filt2)
                    _dbg(f"find_overdue: {entity} filtro2(nudo) -> HTTP {r.status_code}")

                # Se ancora 400, riprova senza filtro stato (solo data passata)
                if r.status_code == 400:
                    r = _run(" and ".join(base_clauses))
                    _dbg(f"find_overdue: {entity} filtro3(no stato) -> HTTP {r.status_code}")
                    status_note = " (SENZA filtro stato: include anche ordini già evasi con data passata)"
                else:
                    status_note = "" if status_field else " (entità senza campo stato: include anche evasi)"

                if r.status_code != 200:
                    errors.append((entity, r.status_code))
                    continue
                records = r.json().get("value", [])
                if not records:
                    empty.append(entity)
                    continue
                # Raccogli righe complete (ordine, articolo, data orig.) per il
                # join FISICO con le date pianificate.
                prod_field = next((f for f in _PRODUCT_KEY_FIELDS
                                   if records[0].get(f) is not None), None)
                order_field = next((f for f in ["SalesOrderNumber", "PurchaseOrderNumber",
                                    "SalesId", "PurchId"] if records[0].get(f) is not None), None)
                qty_field = next((f for f in _SALES_QTY_FIELDS
                                  if records[0].get(f) is not None), None)
                if prod_field:
                    for rec in records:
                        pn = rec.get(prod_field)
                        if pn:
                            overdue_products.add(str(pn))
                            overdue_rows.append({
                                "ordine": rec.get(order_field, "?") if order_field else "?",
                                "articolo": str(pn),
                                "data_orig": rec.get(date_field, "?"),
                                "qta": rec.get(qty_field, "") if qty_field else "",
                            })
                header = (f"[D365 — ORDINI IN RITARDO {entity}] {len(records)} record "
                          f"con {date_field} < {today}{status_note}:")
                parts.append(header + "\n" + self._format_records_list(records))
            except Exception as e:
                errors.append((entity, str(e)))
                continue

        combined = self._combine_results(parts, errors)
        if not combined:
            if empty:
                return (f"Nessun ordine in ritardo trovato (controllate: {', '.join(empty)}). "
                        "Significa che non ci sono righe con data di consegna passata e "
                        "ancora da evadere — oppure i criteri non corrispondono.")
            return ""

        # ── Arricchimento: join FISICO ordine ↔ nuova data pianificata ──
        if with_planning and overdue_products:
            plan_map = self._planning_map_for_products(overdue_products)
            # Ordini di produzione REALI agganciati per numero ordine di vendita
            overdue_sales = {row["ordine"] for row in overdue_rows if row.get("ordine")}
            prod_map = self._production_orders_for_sales(overdue_sales)
            # [v2.0] La pianificazione (PlannedOrders) si aggancia SOLO per articolo:
            # l'entità non espone alcun legame all'ordine di vendita (verificato sul
            # catalogo). L'aggancio è quindi ambiguo se lo STESSO articolo è in ritardo
            # su PIÙ ordini. Calcolo qui quali articoli ricadono in questo caso, per
            # segnalare quelle righe come "nuova data da verificare" invece di darle
            # per certe. Quando un articolo è in un solo ordine, l'aggancio è univoco.
            art_to_orders = {}
            for _r in overdue_rows:
                art_to_orders.setdefault(_r["articolo"], set()).add(str(_r.get("ordine", "")))
            # Costruisci UNA tabella già accoppiata: il codice fa il join,
            # l'AI deve solo stamparla (niente incrocio a carico del modello).
            jl = ["[D365 — RIEPILOGO GIÀ ACCOPPIATO ordini in ritardo + nuova data] "
                  "Ogni riga unisce l'ordine in ritardo al suo articolo e alla nuova "
                  "data pianificata. USA ESATTAMENTE QUESTE RIGHE, non reincrociare:"]
            struct_rows = []  # per la generazione HTML
            for row in overdue_rows:
                art = row["articolo"]
                qta_cli = row.get("qta", "") or "—"
                info = plan_map.get(art)
                prod_real = prod_map.get(str(row["ordine"]))
                # ambiguità: stesso articolo in più ordini in ritardo
                art_ambiguo = len(art_to_orders.get(art, set())) > 1
                # [v2.0] Data SPECIFICA PER ORDINE dalla produzione: l'ordine di
                # produzione è agganciato a QUESTO ordine di vendita (via
                # DemandSalesOrderNumber). Se porta una data, è preferibile alla
                # data della proposta agganciata per articolo, e NON è ambigua.
                # (Verificato sul grafo delle relazioni: per la produzione esiste un
                # legame a livello ordine; per l'approvvigionamento no.)
                prod_date_specifica = (prod_real or {}).get("data_prod") or ""
                usa_data_produzione = bool(prod_real and prod_real.get("ordine_prod") and prod_date_specifica)
                # Ordine REALE (niente proposte): produzione reale o acquisto reale.
                ordine_reale = "—"
                qta_reale = "—"
                art_reale = "—"
                if prod_real and prod_real.get("ordine_prod"):
                    ordine_reale = f"produzione {prod_real['ordine_prod']}"
                    qta_reale = prod_real.get("quantita", "") or "—"
                    art_reale = prod_real.get("articolo_reale") or "—"
                elif info and (info.get("ordine_acq_reale") or info.get("ordine_trasf_reale")):
                    acq = info.get("ordine_acq_reale") or info.get("ordine_trasf_reale")
                    ordine_reale = f"acquisto {acq}"
                    qta_reale = info.get("quantita", "") or "—"
                    art_reale = info.get("articolo_reale") or "—"

                if usa_data_produzione:
                    # Caso migliore: data presa dall'ordine di produzione di QUESTO ordine.
                    nuova_data = _fmt_date_it(prod_date_specifica)
                    motivo = "attesa produzione interna (data dell'ordine di produzione collegato)"
                    tipo = info["tipo"] if info else "Produzione"
                    fornitore = (info.get("fornitore") if info else "") or "—"
                    stato = "ok"
                elif info:
                    motivo = ("attesa approvvigionamento da fornitore" if "forn" in info["tipo"].lower()
                              else "attesa produzione interna" if "produz" in info["tipo"].lower()
                              else info["tipo"])
                    nuova_data = _fmt_date_it(info["data"])
                    tipo = info["tipo"]
                    fornitore = info["fornitore"] or "—"
                    stato = "ok"
                else:
                    # Senza pianificazione: rilevante solo se esiste un ordine reale di produzione
                    nuova_data = "NON DISPONIBILE"
                    tipo = "—"
                    fornitore = "—"
                    if prod_real and prod_real.get("ordine_prod"):
                        motivo = "in produzione (ordine reale esistente)"
                        stato = "giallo"
                    else:
                        motivo = "nessuna proposta di pianificazione per questo articolo"
                        stato = "rosso"

                # Segnala divergenza: tipo proposta (acquisto/produzione) vs ordine reale collegato
                if info and "forn" in info["tipo"].lower() and ordine_reale.startswith("produzione"):
                    motivo += " — VERIFICARE: proposta di acquisto ma ordine reale di PRODUZIONE collegato"
                    if stato == "ok":
                        stato = "giallo"
                elif info and "produz" in info["tipo"].lower() and ordine_reale.startswith("acquisto"):
                    motivo += " — VERIFICARE: proposta di produzione ma ordine reale di ACQUISTO collegato"
                    if stato == "ok":
                        stato = "giallo"

                # [v2.0] Ambiguità del join per articolo: se la NUOVA DATA proviene
                # dalla PROPOSTA agganciata per articolo (info) e lo stesso articolo è
                # in ritardo su più ordini, potrebbe non essere quella di QUESTO ordine.
                # NON si applica quando la data viene dall'ordine di produzione
                # collegato (usa_data_produzione): in quel caso è specifica per ordine.
                if info and art_ambiguo and not usa_data_produzione:
                    n_ord = len(art_to_orders.get(art, set()))
                    motivo += (f" — N.B. articolo in ritardo su {n_ord} ordini: nuova data "
                               f"agganciata per ARTICOLO (non per ordine), verificare l'attribuzione")
                    if stato == "ok":
                        stato = "giallo"

                jl.append(f"  - Ordine {row['ordine']} | Q.tà ordine {qta_cli} | Articolo {art} | "
                          f"Data orig. {_fmt_date_it(row['data_orig'])} | Nuova data {nuova_data} | "
                          f"{tipo} | Fornitore {fornitore} | "
                          f"Ordine prod/acq reale: {ordine_reale} | Q.tà prod/acq: {qta_reale} | "
                          f"Cod. art. produzione/acquisto: {art_reale} | Motivo: {motivo}")
                struct_rows.append({
                    "ordine": row["ordine"], "qta_cli": qta_cli, "articolo": art,
                    "data_orig": row["data_orig"], "nuova_data": info["data"] if info else "NON DISPONIBILE",
                    "tipo": tipo, "fornitore": fornitore, "ordine_reale": ordine_reale,
                    "qta_reale": qta_reale, "art_reale": art_reale, "motivo": motivo, "stato": stato,
                })
            # Genera il report HTML colorato e salvane il percorso
            try:
                html_path = self._write_overdue_html(struct_rows)
                if html_path:
                    jl.insert(1, f"[REPORT_HTML: {html_path}]")
            except Exception as e:
                _dbg(f"html report errore: {e}")
            n_con = sum(1 for row in overdue_rows if plan_map.get(row["articolo"]))
            jl.append("")
            jl.append(f"[ISTRUZIONE: presenta queste righe come UNA tabella unica con colonne, "
                      f"in questo ordine: Ordine, Q.tà ordine, Articolo, Data orig., Nuova data, "
                      f"Tipo, Fornitore, Ordine prod/acq reale, Q.tà prod/acq, "
                      f"Cod. art. produzione/acquisto, Motivo. "
                      f"Le righe sono GIÀ accoppiate correttamente dal sistema: NON modificare "
                      f"gli abbinamenti. {n_con} ordini su {len(overdue_rows)} "
                      f"hanno una nuova data pianificata. "
                      f"La colonna 'Ordine prod/acq reale' mostra SOLO l'ordine reale (di produzione "
                      f"o di acquisto), non le proposte di pianificazione. La colonna "
                      f"'Cod. art. produzione/acquisto' è il codice articolo come risulta nell'ordine "
                      f"reale (può differire dall'articolo venduto). "
                      f"REGOLA TASSATIVA: riporta i NUMERI ORDINE e i CODICI ARTICOLO "
                      f"ESATTAMENTE come scritti qui sopra, carattere per carattere (es. se "
                      f"qui c'è '10032049' scrivi '10032049', NON inventare codici tipo "
                      f"'TEL_001' o numeri progressivi). NON sostituire, NON abbreviare, "
                      f"NON normalizzare i codici. Copiali letteralmente.]")
            combined += "\n\n" + "\n".join(jl)
        return combined

    def _planning_map_for_products(self, product_numbers: set) -> dict:
        """Ritorna {ProductNumber: {data, ordine, tipo, fornitore}} con la PRIMA
        data utile per ciascun articolo, da PlannedOrders. È la base del join
        fisico ordine↔nuova data."""
        token = self.tm.get_access_token()
        if not token or not product_numbers:
            return {}
        # Entità di pianificazione FISSA (deterministica)
        planned_entity = _OVERDUE_PLANNED_ENTITY
        catalog = self.load_catalog()
        if catalog and planned_entity not in catalog:
            # ripiega su una variante nota presente nel catalogo
            planned_entity = next((e for e in _PLANNED_ORDER_ENTITIES if e in catalog), planned_entity)
        prod_field = self._pick_field(planned_entity, _PRODUCT_KEY_FIELDS, "string")
        date_field = self._pick_field(planned_entity, _PLANNED_DATE_FIELDS, "date")
        if not prod_field:
            return {}
        prods = list(product_numbers)[:25]
        clauses = [f"{prod_field} eq '{str(p).replace(chr(39), chr(39)*2)}'" for p in prods]
        filt = " or ".join(clauses)
        cross_company = bool(self.cfg.get("dyn_cross_company", False))
        params = [f"$filter={quote(filt)}", "$top=200"]
        if date_field:
            params.append(f"$orderby={quote(date_field + ' asc')}")
        if cross_company:
            params.append("cross-company=true")
        try:
            import requests as req
            def _get(ps):
                return self._dyn_get(self._data_url(f"/{planned_entity}?" + "&".join(ps)), token, timeout=30)
            r = _get(params)
            _dbg(f"planning: {planned_entity} per {len(prods)} articoli -> HTTP {r.status_code}")
            if r.status_code == 400 and date_field:
                ps = [f"$filter={quote(filt)}", "$top=200"]
                if cross_company: ps.append("cross-company=true")
                r = _get(ps)
            if r.status_code != 200:
                return {}
            records = r.json().get("value", [])
            # Rileva il campo quantità dai dati (numerico, non in cache string)
            qty_field = next((f for f in _PURCH_QTY_FIELDS
                              if records and records[0].get(f) is not None), None)
            best = {}
            for rec in records:
                prod = rec.get(prod_field)
                if prod is None: continue
                prod = str(prod)
                if prod in best: continue  # i record sono ordinati per data: tieni il primo
                tl = str(rec.get("PlannedOrderType", "") or "").lower()
                if "purch" in tl or "item" in tl: tipo = "Approvvigionamento (fornitore)"
                elif "prod" in tl or "bom" in tl: tipo = "Produzione (BOM)"
                elif "transf" in tl: tipo = "Trasferimento"
                else: tipo = rec.get("PlannedOrderType", "") or "—"
                best[prod] = {
                    "data": rec.get(date_field, "?") if date_field else "?",
                    "ordine": rec.get("PlannedOrderNumber", ""),
                    "tipo": tipo,
                    "fornitore": rec.get("VendorAccountNumber", ""),
                    "ordine_acq_reale": rec.get("AppendingPurchaseOrderNumber", "") or "",
                    "ordine_trasf_reale": rec.get("AppendingTransferOrderNumber", "") or "",
                    "articolo_reale": rec.get("ItemNumber", "") or rec.get("ProductNumber", "") or "",
                    "quantita": rec.get(qty_field, "") if qty_field else "",
                }
            return best
        except Exception as e:
            _dbg(f"planning: errore {e}")
            return {}

    def _production_orders_for_sales(self, sales_order_numbers: set) -> dict:
        """Per un insieme di numeri ordine di vendita, recupera da
        ProductionOrderHeaders gli ordini di produzione REALI collegati,
        agganciando per DemandSalesOrderNumber (legame diretto vendita→produzione).
        Ritorna {sales_order_number: {ordine_prod, stato, data}}.
        """
        token = self.tm.get_access_token()
        if not token or not sales_order_numbers:
            return {}
        # Entità di produzione FISSA (deterministica)
        prod_entity = _OVERDUE_PRODUCTION_ENTITY
        catalog = self.load_catalog()
        if catalog and prod_entity not in catalog:
            prod_entity = next((e for e in ["ProductionOrderHeaders", "ProdTable"]
                                if e in catalog), prod_entity)
        # Campo che lega l'ordine di produzione all'ordine di vendita
        demand_field = self._pick_field(prod_entity,
            ["DemandSalesOrderNumber", "SalesId"], "string")
        prodnum_field = self._pick_field(prod_entity,
            ["ProductionOrderNumber", "ProdId"], "string")
        item_field = self._pick_field(prod_entity,
            ["ItemNumber", "ProductNumber"], "string")
        if not demand_field or not prodnum_field:
            _dbg(f"production: campi mancanti (demand={demand_field}, num={prodnum_field})")
            return {}

        sos = [str(s) for s in sales_order_numbers if s and s != "?"][:25]
        if not sos:
            return {}
        clauses = [f"{demand_field} eq '{s.replace(chr(39), chr(39)*2)}'" for s in sos]
        filt = " or ".join(clauses)
        cross_company = bool(self.cfg.get("dyn_cross_company", False))
        params = [f"$filter={quote(filt)}", "$top=200"]
        if cross_company:
            params.append("cross-company=true")
        try:
            import requests as req
            r = self._dyn_get(self._data_url(f"/{prod_entity}?" + "&".join(params)), token, timeout=30)
            _dbg(f"production: {prod_entity} per {len(sos)} ordini vendita -> HTTP {r.status_code}")
            if r.status_code != 200:
                return {}
            _recs = r.json().get("value", [])
            # Rileva il campo quantità direttamente dai dati (è numerico, non in cache string)
            qty_field = next((f for f in _PROD_QTY_FIELDS
                              if _recs and _recs[0].get(f) is not None), None)
            # Rileva il campo DATA dell'ordine di produzione: prima dal catalogo
            # (campi data noti dell'entità), poi conferma sui dati. Serve per la
            # "nuova data" SPECIFICA PER ORDINE dei casi di produzione.
            prod_date_avail = set(catalog.get(prod_entity, {}).get("date", [])) if catalog else set()
            date_field = next((f for f in _PROD_DATE_FIELDS if f in prod_date_avail), None)
            if not date_field and _recs:
                date_field = next((f for f in _PROD_DATE_FIELDS
                                   if _recs[0].get(f) not in (None, "")), None)
            out = {}
            for rec in _recs:
                so = str(rec.get(demand_field, ""))
                if not so:
                    continue
                # Escludi ordini di produzione chiusi/finiti (Ended, ReportedAsFinished)
                stato = str(rec.get("ProductionOrderStatus", "") or "")
                stato_norm = stato.lower().replace("_", "").replace(" ", "")
                if any(ex.replace(" ", "") in stato_norm for ex in _PROD_STATUS_EXCLUDE):
                    _dbg(f"production: ordine {rec.get(prodnum_field,'?')} escluso (stato {stato})")
                    continue
                if so in out:
                    continue
                qty = rec.get(qty_field, "") if qty_field else ""
                out[so] = {
                    "ordine_prod": rec.get(prodnum_field, ""),
                    "stato": stato,
                    "articolo_reale": rec.get(item_field, "") if item_field else "",
                    "quantita": qty,
                    "data_prod": rec.get(date_field, "") if date_field else "",
                }
            return out
        except Exception as e:
            _dbg(f"production: errore {e}")
            return {}

    def _write_overdue_html(self, rows: list) -> str:
        """Genera un report HTML colorato degli ordini in ritardo e ne ritorna
        il percorso. Verde = nuova data disponibile, Giallo = in produzione
        (ordine reale ma senza data pianificata), Rosso = nessuna proposta."""
        if not rows:
            return ""
        import html as _html, datetime as _dt, tempfile, os
        def esc(v): return _html.escape(str(v if v not in (None, "") else "—"))
        def fmt_data(v):
            """Converte '2025-01-24T12:00:00Z' in '24/01/2025'. Lascia invariati
            i valori non-data (es. 'NON DISPONIBILE', '—')."""
            if not v or v in ("—", "NON DISPONIBILE", "?"):
                return v or "—"
            s = str(v)
            try:
                # prende i primi 10 caratteri (YYYY-MM-DD) se è una data ISO
                if len(s) >= 10 and s[4] == "-" and s[7] == "-":
                    d = _dt.date(int(s[0:4]), int(s[5:7]), int(s[8:10]))
                    return d.strftime("%d/%m/%Y")
            except (ValueError, IndexError):
                pass
            return s
        colori = {"ok": ("#16a34a", "#dcfce7"), "giallo": ("#d97706", "#fef3c7"),
                  "rosso": ("#dc2626", "#fee2e2")}
        oggi = _dt.date.today().strftime("%d/%m/%Y")
        n_ok = sum(1 for r in rows if r["stato"] == "ok")
        n_gi = sum(1 for r in rows if r["stato"] == "giallo")
        n_ro = sum(1 for r in rows if r["stato"] == "rosso")
        righe_html = []
        for r in rows:
            bordo, sfondo = colori.get(r["stato"], ("#64748b", "#f1f5f9"))
            badge = {"ok": "🟢 Nuova data", "giallo": "🟡 In produzione",
                     "rosso": "🔴 Da gestire"}.get(r["stato"], "")
            righe_html.append(f"""
      <tr style="border-left:5px solid {bordo};background:{sfondo}">
        <td class="ord">{esc(r['ordine'])}</td>
        <td class="qta">{esc(r.get('qta_cli'))}</td>
        <td>{esc(r['articolo'])}</td>
        <td>{esc(fmt_data(r['data_orig']))}</td>
        <td class="nd">{esc(fmt_data(r['nuova_data']))}</td>
        <td>{esc(r['tipo'])}</td>
        <td>{esc(r['fornitore'])}</td>
        <td class="rif">{esc(r.get('ordine_reale'))}</td>
        <td class="qta">{esc(r.get('qta_reale'))}</td>
        <td>{esc(r['art_reale'])}</td>
        <td><span class="badge" style="background:{bordo}">{badge}</span><br>{esc(r['motivo'])}</td>
      </tr>""")
        doc = f"""<!DOCTYPE html><html lang="it"><head><meta charset="utf-8">
<title>Ordini in ritardo — {oggi}</title>
<style>
  body{{font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;margin:0;background:#f8fafc;color:#1e293b}}
  .head{{background:#1e293b;color:#fff;padding:24px 32px}}
  .head h1{{margin:0 0 4px;font-size:22px}} .head p{{margin:0;color:#94a3b8;font-size:13px}}
  .kpis{{display:flex;gap:16px;padding:20px 32px;flex-wrap:wrap}}
  .kpi{{flex:1;min-width:140px;background:#fff;border-radius:10px;padding:16px;box-shadow:0 1px 3px rgba(0,0,0,.1)}}
  .kpi .n{{font-size:28px;font-weight:700}} .kpi .l{{font-size:12px;color:#64748b}}
  table{{width:calc(100% - 64px);margin:0 32px 32px;border-collapse:separate;border-spacing:0 6px}}
  th{{text-align:left;padding:10px 12px;font-size:11px;text-transform:uppercase;color:#64748b;letter-spacing:.4px}}
  td{{padding:12px;background:inherit;font-size:13px;vertical-align:top}}
  tr td:first-child{{border-radius:8px 0 0 8px}} tr td:last-child{{border-radius:0 8px 8px 0}}
  .ord{{font-weight:700;font-family:monospace}} .nd{{font-weight:600}}
  .qta{{text-align:right;font-variant-numeric:tabular-nums}}
  .rif{{font-family:monospace;font-size:11px;color:#475569}}
  .badge{{color:#fff;padding:2px 8px;border-radius:10px;font-size:11px;font-weight:600;white-space:nowrap}}
</style></head><body>
<div class="head"><h1>📦 Ordini in ritardo — nuova data e ordine reale</h1>
<p>Generato il {oggi} • Chat Assistant / Dynamics 365</p></div>
<div class="kpis">
  <div class="kpi"><div class="n">{len(rows)}</div><div class="l">Righe in ritardo</div></div>
  <div class="kpi"><div class="n" style="color:#16a34a">{n_ok}</div><div class="l">🟢 Con nuova data</div></div>
  <div class="kpi"><div class="n" style="color:#d97706">{n_gi}</div><div class="l">🟡 In produzione</div></div>
  <div class="kpi"><div class="n" style="color:#dc2626">{n_ro}</div><div class="l">🔴 Da gestire</div></div>
</div>
<table><thead><tr>
  <th>Ordine</th><th>Q.tà</th><th>Articolo</th><th>Data orig.</th><th>Nuova data</th><th>Tipo</th>
  <th>Fornitore</th><th>Ordine prod/acq reale</th><th>Q.tà</th><th>Cod. art. prod/acq</th><th>Stato / Motivo</th>
</tr></thead><tbody>{''.join(righe_html)}</tbody></table>
</body></html>"""
        out_dir = os.path.join(tempfile.gettempdir(), "chat_assistant_reports")
        os.makedirs(out_dir, exist_ok=True)
        fname = f"ordini_ritardo_{_dt.datetime.now().strftime('%Y%m%d_%H%M%S')}.html"
        path = os.path.join(out_dir, fname)
        with open(path, "w", encoding="utf-8") as f:
            f.write(doc)
        _dbg(f"html report scritto: {path}")
        return path
    # ╔══════════════════════════════════════════════════════════════════════╗
    # ║  MODULO: VERIFICA ORDINI IN RITARDO                        [FINE 2/2]  ║
    # ╚══════════════════════════════════════════════════════════════════════╝

    def _write_generic_html(self, titolo: str, records: list, sottotitolo: str = "") -> str:
        """Genera un report HTML generico (tabella pulita) per QUALSIASI risultato
        Dynamics, non solo per il modulo ordini in ritardo. Le colonne sono
        ricavate dall'unione dei campi non interni dei record. Ritorna il percorso
        del file salvato, o '' se non ci sono record."""
        if not records:
            return ""
        import html as _html, datetime as _dt, tempfile, os
        def esc(v): return _html.escape(str(v if v not in (None, "") else "—"))
        def fmt(v):
            """Formatta le date ISO in gg/mm/aaaa, lascia il resto invariato."""
            s = str(v)
            try:
                if len(s) >= 10 and s[4] == "-" and s[7] == "-":
                    d = _dt.date(int(s[0:4]), int(s[5:7]), int(s[8:10]))
                    return d.strftime("%d/%m/%Y")
            except (ValueError, IndexError):
                pass
            return s
        # Colonne: unione dei campi non interni, preservando l'ordine di comparsa
        colonne = []
        for rec in records:
            for k in rec.keys():
                if not k.startswith("@") and k not in colonne:
                    colonne.append(k)
        # Limita a un numero ragionevole di colonne per leggibilità
        MAX_COL = 12
        colonne = colonne[:MAX_COL]
        oggi = _dt.datetime.now().strftime("%d/%m/%Y %H:%M")
        thead = "".join(f"<th>{esc(c)}</th>" for c in colonne)
        righe = []
        for rec in records:
            tds = "".join(
                f'<td>{esc(fmt(rec.get(c)))}</td>' for c in colonne
            )
            righe.append(f"<tr>{tds}</tr>")
        sub = esc(sottotitolo) if sottotitolo else f"{len(records)} record"
        doc = f"""<!DOCTYPE html><html lang="it"><head><meta charset="utf-8">
<title>{esc(titolo)}</title>
<style>
  body{{font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;margin:0;background:#f8fafc;color:#1e293b}}
  .head{{background:#1e293b;color:#fff;padding:24px 32px}}
  .head h1{{margin:0 0 4px;font-size:22px}} .head p{{margin:0;color:#94a3b8;font-size:13px}}
  .wrap{{padding:20px 32px 32px;overflow-x:auto}}
  table{{width:100%;border-collapse:separate;border-spacing:0 6px}}
  th{{text-align:left;padding:10px 12px;font-size:11px;text-transform:uppercase;color:#64748b;letter-spacing:.4px;white-space:nowrap}}
  td{{padding:10px 12px;background:#fff;font-size:13px;vertical-align:top}}
  tr td:first-child{{border-radius:8px 0 0 8px;font-weight:600}} tr td:last-child{{border-radius:0 8px 8px 0}}
  tr:hover td{{background:#eff6ff}}
</style></head><body>
<div class="head"><h1>{esc(titolo)}</h1><p>{sub} · generato il {oggi}</p></div>
<div class="wrap"><table><thead><tr>{thead}</tr></thead><tbody>{''.join(righe)}</tbody></table></div>
</body></html>"""
        out_dir = os.path.join(tempfile.gettempdir(), "chat_assistant_reports")
        os.makedirs(out_dir, exist_ok=True)
        fname = f"dynamics_{_dt.datetime.now().strftime('%Y%m%d_%H%M%S')}.html"
        path = os.path.join(out_dir, fname)
        with open(path, "w", encoding="utf-8") as f:
            f.write(doc)
        _dbg(f"html report generico scritto: {path}")
        return path

    # NOTA: _fetch_planning_for_products è una versione PRECEDENTE (deprecata)
    # del recupero pianificazione, sostituita da _planning_map_for_products.
    # Non è più richiamata: conservata solo per riferimento, rimovibile.
    def _fetch_planning_for_products(self, product_numbers: set) -> str:
        """Per un insieme di ProductNumber, recupera da PlannedOrders gli ordini
        pianificati corrispondenti e la loro nuova data (DeliveryDate/RequirementDate).
        È il JOIN deterministico ordine-in-ritardo <-> nuova data pianificata,
        agganciato sul numero articolo.
        """
        token = self.tm.get_access_token()
        if not token or not product_numbers:
            return ""
        entities_cfg = self.cfg.get("dyn_entities", []) or []
        if isinstance(entities_cfg, str):
            entities_cfg = [e.strip() for e in entities_cfg.splitlines() if e.strip()]
        entities = [e.split(":", 1)[0].strip() if ":" in e else e.strip()
                    for e in entities_cfg if e.strip()]
        planned_entity = next((e for e in entities if e in _PLANNED_ORDER_ENTITIES), None)
        if not planned_entity:
            _dbg("planning: nessuna entità PlannedOrders caricata")
            return ""

        prod_field = self._pick_field(planned_entity, _PRODUCT_KEY_FIELDS, "string")
        date_field = self._pick_field(planned_entity, _PLANNED_DATE_FIELDS, "date")
        if not prod_field:
            return ""

        # Limita a un numero ragionevole di articoli per non generare URL enormi
        prods = list(product_numbers)[:15]
        # Costruisci filtro OR: ProductNumber eq 'A' or ProductNumber eq 'B' ...
        clauses = [f"{prod_field} eq '{str(p).replace(chr(39), chr(39)*2)}'" for p in prods]
        filt = " or ".join(clauses)
        cross_company = bool(self.cfg.get("dyn_cross_company", False))
        params = [f"$filter={quote(filt)}", "$top=100"]
        if date_field:
            params.append(f"$orderby={quote(date_field + ' asc')}")
        if cross_company:
            params.append("cross-company=true")
        url = self._data_url(f"/{planned_entity}?" + "&".join(params))

        try:
            import requests as req
            r = self._dyn_get(url, token, timeout=30)
            _dbg(f"planning: {planned_entity} per {len(prods)} articoli -> HTTP {r.status_code}")
            # Se l'orderby non è supportato, riprova senza
            if r.status_code == 400 and date_field:
                params = [f"$filter={quote(filt)}", "$top=100"]
                if cross_company:
                    params.append("cross-company=true")
                r = self._dyn_get(self._data_url(f"/{planned_entity}?" + "&".join(params)), token, timeout=30)
            if r.status_code != 200:
                return ""
            records = r.json().get("value", [])
            if not records:
                return ""
            # Deduplica per articolo: tieni solo la PRIMA data utile per ciascuno
            # (i record sono già ordinati per data crescente). Riduce 60 righe a
            # una per articolo: molto più leggibile.
            best = {}
            for rec in records:
                prod = rec.get(prod_field)
                if prod is None:
                    continue
                prod = str(prod)
                if prod in best:
                    continue  # già preso il primo (data più vicina)
                tipo_raw = str(rec.get("PlannedOrderType", "") or "")
                # Traduci i tipi più comuni in italiano leggibile
                tl = tipo_raw.lower()
                if "purch" in tl: tipo = "Approvvigionamento (fornitore)"
                elif "prod" in tl: tipo = "Produzione (BOM)"
                elif "transf" in tl: tipo = "Trasferimento"
                else: tipo = tipo_raw or "—"
                best[prod] = {
                    "data": rec.get(date_field, "?") if date_field else "?",
                    "ordine": rec.get("PlannedOrderNumber", ""),
                    "tipo": tipo,
                    "fornitore": rec.get("VendorAccountNumber", ""),
                }

            n_art = len(best)
            lines = [f"[D365 — NUOVE DATE PIANIFICATE] Per gli articoli degli ordini in "
                     f"ritardo è stata trovata la prima data di riapprovvigionamento/"
                     f"produzione pianificata (fonte: {planned_entity}, aggancio per "
                     f"{prod_field}). {n_art} articoli con proposta di pianificazione:"]
            for prod, info in best.items():
                bits = [f"articolo {prod}", f"nuova data prevista: {info['data']}"]
                if info["tipo"] and info["tipo"] != "—": bits.append(info["tipo"])
                if info["fornitore"]: bits.append(f"fornitore {info['fornitore']}")
                if info["ordine"]: bits.append(f"rif. {info['ordine']}")
                lines.append("  - " + "; ".join(bits))
            lines.append("")
            lines.append("[ISTRUZIONE PRESENTAZIONE: presenta UNA sola tabella che unisce "
                         "ogni ordine di vendita in ritardo con la nuova data prevista del "
                         "suo articolo (colonne: Ordine, Articolo, Data confermata originale, "
                         "Nuova data prevista, Tipo, Fornitore). Per il motivo, deduci dalla "
                         "colonna Tipo: 'in attesa di approvvigionamento dal fornitore' o "
                         "'in attesa di produzione interna'. Mostra una sola riga per ordine.]")
            return "\n".join(lines)
        except Exception as e:
            _dbg(f"planning: errore {e}")
            return ""

    def _translate_to_italian(self, text: str) -> str:
        """Traduce una breve domanda in ITALIANO tramite il motore AI, per
        permettere ai rilevatori d'intento (italiani) dei moduli deterministici
        di riconoscere richieste poste in altre lingue. L'AI fa SOLO la
        traduzione: l'esecuzione del modulo resta deterministica.

        Mantiene invariati nomi propri, codici, numeri e date. Ritorna la
        traduzione, oppure "" in caso di errore: in tal caso la domanda prosegue
        verso la modalità dinamica, senza alcuna regressione."""
        text = (text or "").strip()
        if not text:
            return ""
        sys_p = ("Sei un traduttore. Traduci in ITALIANO il testo dell'utente, "
                 "mantenendo invariati nomi propri, codici, numeri e date. "
                 "Rispondi SOLO con la traduzione, senza virgolette né altro.")
        try:
            out = self._ask_ai(sys_p, text, max_tokens=200)
            return (out or "").strip()
        except Exception:
            return ""

    def _ask_ai(self, system_prompt: str, user_prompt: str, max_tokens: int = 800) -> str:
        """Chiama il motore AI configurato (Claude/OpenAI/LM Studio) per un
        singolo turno. Usato dalla modalità dinamica per costruire il piano di
        query. Ritorna il testo della risposta, o stringa vuota in caso di errore.

        RETRY AUTOMATICO sui codici transitori 529 (server sovraccarico) e 429
        (troppe richieste) per i motori cloud: aspetta e ritenta fino a 3 volte,
        così i sovraccarichi temporanei di Anthropic/OpenAI non fanno fallire
        l'intera operazione dinamica.
        """
        import requests as req, time as _time
        engine = self.cfg.get("ai_engine", "lmstudio")
        RETRY_STATUS = {429, 529}
        MAX_RETRY = 3
        try:
            if engine == "claude":
                key = self.cfg.get("claude_api_key", "")
                model = self.cfg.get("claude_model", "claude-sonnet-4-6")
                # Fallback: se il modello dell'area fallisce (es. non abilitato
                # per la chiave), si ritenta col modello base configurato.
                fallback = (self.cfg.get("claude_model_fallback") or "").strip()
                if not key:
                    return ""

                def _claude_text(data: dict) -> str:
                    """Estrae il testo dai blocchi 'text'. I modelli più recenti
                    (es. Fable 5) possono anteporre blocchi di ragionamento:
                    prendere content[0]['text'] alla cieca falliva con KeyError."""
                    return "".join(b.get("text", "") for b in (data.get("content") or [])
                                   if isinstance(b, dict) and b.get("type") == "text").strip()

                def _call(model_name: str) -> str | None:
                    for tentativo in range(MAX_RETRY):
                        r = req.post("https://api.anthropic.com/v1/messages",
                            headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                                     "Content-Type": "application/json"},
                            json={"model": model_name, "max_tokens": max_tokens,
                                  "system": system_prompt,
                                  "messages": [{"role": "user", "content": user_prompt}]},
                            timeout=45)
                        if r.status_code in RETRY_STATUS and tentativo < MAX_RETRY - 1:
                            attesa = 3 * (tentativo + 1)  # 3s, poi 6s
                            _dbg(f"dynamic: AI claude HTTP {r.status_code} (sovraccarico), "
                                 f"ritento tra {attesa}s ({tentativo+1}/{MAX_RETRY-1})")
                            _time.sleep(attesa)
                            continue
                        if r.status_code != 200:
                            _dbg(f"dynamic: AI claude HTTP {r.status_code} "
                                 f"[{model_name}]: {r.text[:200]}")
                            if r.status_code in RETRY_STATUS:
                                self._ai_overloaded = True
                            return None
                        return _claude_text(r.json())
                    return None

                out = _call(model)
                if not out and fallback and fallback != model:
                    _dbg(f"dynamic: modello '{model}' senza risposta, "
                         f"ripiego sul modello base '{fallback}'")
                    out = _call(fallback)
                return out or ""
                return ""
            elif engine == "openai":
                key = self.cfg.get("openai_api_key", "")
                model = self.cfg.get("openai_model", "gpt-4o-mini")
                if not key:
                    return ""
                for tentativo in range(MAX_RETRY):
                    r = req.post("https://api.openai.com/v1/chat/completions",
                        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                        json={"model": model, "max_tokens": max_tokens,
                              "messages": [{"role": "system", "content": system_prompt},
                                           {"role": "user", "content": user_prompt}]},
                        timeout=45)
                    if r.status_code in RETRY_STATUS and tentativo < MAX_RETRY - 1:
                        attesa = 3 * (tentativo + 1)
                        _dbg(f"dynamic: AI openai HTTP {r.status_code} (sovraccarico), "
                             f"ritento tra {attesa}s ({tentativo+1}/{MAX_RETRY-1})")
                        _time.sleep(attesa)
                        continue
                    if r.status_code != 200:
                        _dbg(f"dynamic: AI openai HTTP {r.status_code}")
                        if r.status_code in RETRY_STATUS:
                            self._ai_overloaded = True
                        return ""
                    return r.json()["choices"][0]["message"]["content"].strip()
                return ""
            else:  # lmstudio (locale: nessun retry, non si sovraccarica come il cloud)
                url = self.cfg.get("lm_url", "http://localhost:1234/v1/chat/completions")
                model = self.cfg.get("lm_model", "local-model")
                r = req.post(url,
                    headers={"Content-Type": "application/json"},
                    json={"model": model, "max_tokens": max_tokens, "temperature": 0,
                          "messages": [{"role": "system", "content": system_prompt},
                                       {"role": "user", "content": user_prompt}]},
                    timeout=60)
                if r.status_code != 200:
                    _dbg(f"dynamic: AI lmstudio HTTP {r.status_code}")
                    return ""
                return r.json()["choices"][0]["message"]["content"].strip()
        except Exception as e:
            _dbg(f"dynamic: AI errore {e}")
            return ""

    def _dynamic_join(self, j: dict, full_catalog: dict, string_fields: dict,
                      date_fields: dict, token: str) -> str:
        """JOIN DINAMICO ASSISTITO tra due entità. L'AI propone le due entità e
        i campi di aggancio; QUI il codice valida che i campi esistano davvero in
        entrambe ed esegue l'unione con MATCH ESATTO sui valori chiave (l'unione
        la fa il codice, non l'AI). Limiti e avviso di verifica imposti."""
        import json
        ent_a = (j.get("entita_a") or "").strip()
        ent_b = (j.get("entita_b") or "").strip()
        campo_a = (j.get("campo_a") or "").strip()
        campo_b = (j.get("campo_b") or "").strip()
        filtro_a = (j.get("filtro_a") or "").strip()
        spieg = j.get("spiegazione", "")
        _dbg(f"dynamic JOIN: {ent_a}.{campo_a} <-> {ent_b}.{campo_b}")

        # SALVAGUARDIA: entrambe le entità devono esistere nel catalogo
        if not (full_catalog and ent_a in full_catalog and ent_b in full_catalog):
            _dbg("dynamic JOIN: entità non valide nel catalogo")
            return ""
        # Carica i campi reali di entrambe dal catalogo
        for e in (ent_a, ent_b):
            string_fields[e] = full_catalog[e]["string"]
            date_fields[e] = full_catalog[e]["date"]
        campi_a_validi = set(string_fields[ent_a] + date_fields[ent_a])
        campi_b_validi = set(string_fields[ent_b] + date_fields[ent_b])
        # SALVAGUARDIA: i campi di aggancio devono esistere nelle rispettive entità
        if campo_a not in campi_a_validi or campo_b not in campi_b_validi:
            _dbg(f"dynamic JOIN: campi di aggancio inesistenti ({campo_a}/{campo_b})")
            return ""
        # [v2.0] Verifica se esiste una RELAZIONE REALE tra le due entità ($metadata):
        # se sì, l'aggancio proposto è corroborato dalla struttura dichiarata da
        # Dynamics; se no, resta un join "per valore" da verificare con più cautela.
        rel = self._find_relation(ent_a, ent_b, full_catalog)
        rel_nota = ""
        if rel:
            coppie = rel.get("pairs") or []
            if coppie:
                rel_nota = (" Aggancio confermato da una relazione reale dichiarata in Dynamics "
                            f"(via {rel['nav']}).")
            else:
                rel_nota = f" Esiste una relazione dichiarata (via {rel['nav']}) ma senza chiave esplicita."
            _dbg(f"dynamic JOIN: relazione reale trovata {ent_a}<->{ent_b} via {rel['nav']}")
        else:
            _dbg(f"dynamic JOIN: nessuna relazione reale {ent_a}<->{ent_b}, join per valore")
        # Filtro read-only e validazione campi del filtro su A
        if filtro_a and not self._is_readonly_filter(filtro_a):
            _dbg("dynamic JOIN: filtro_a scartato (non read-only)")
            filtro_a = ""

        HARD_LIMIT = 50
        cross_company = bool(self.cfg.get("dyn_cross_company", False))
        # 1. Interroga A (con eventuale filtro), limitato
        params_a = [f"$top={HARD_LIMIT}"]
        if filtro_a:
            params_a.insert(0, f"$filter={quote(filtro_a)}")
        if cross_company:
            params_a.append("cross-company=true")
        try:
            ra = self._dyn_get(self._data_url(f"/{ent_a}?" + "&".join(params_a)), token, timeout=30)
            if ra.status_code == 400 and filtro_a:
                ra = self._dyn_get(self._data_url(f"/{ent_a}?$top={HARD_LIMIT}"), token, timeout=30)
            if ra.status_code != 200:
                _dbg(f"dynamic JOIN: A HTTP {ra.status_code}")
                return ""
            rows_a = ra.json().get("value", [])
            if not rows_a:
                return f"[D365 — JOIN DINAMICO] Nessun record in {ent_a} per questa richiesta."
            # 2. Raccogli i valori chiave da A e interroga B filtrando su quei valori
            chiavi = list({r.get(campo_a) for r in rows_a if r.get(campo_a) not in (None, "")})[:HARD_LIMIT]
            rows_b = []
            if chiavi:
                # Costruisci un filtro OR sui valori chiave (a blocchi per non sforare l'URL)
                blocco = chiavi[:25]
                ors = " or ".join(f"{campo_b} eq '{str(v).replace(chr(39), chr(39)*2)}'" for v in blocco)
                params_b = [f"$filter={quote(ors)}", f"$top={HARD_LIMIT}"]
                if cross_company:
                    params_b.append("cross-company=true")
                rb = self._dyn_get(self._data_url(f"/{ent_b}?" + "&".join(params_b)), token, timeout=30)
                if rb.status_code == 200:
                    rows_b = rb.json().get("value", [])
                else:
                    _dbg(f"dynamic JOIN: B HTTP {rb.status_code}")
            # 3. UNIONE NEL CODICE: match esatto sui valori chiave
            idx_b = {}
            for rb_row in rows_b:
                idx_b.setdefault(rb_row.get(campo_b), []).append(rb_row)
            uniti = []
            for ra_row in rows_a:
                k = ra_row.get(campo_a)
                matches = idx_b.get(k, [])
                if matches:
                    for mb in matches:
                        riga = dict(ra_row)
                        # prefissa i campi di B per evitare collisioni di nome
                        for kk, vv in mb.items():
                            if not kk.startswith("@"):
                                riga[f"{ent_b}.{kk}"] = vv
                        uniti.append(riga)
                else:
                    uniti.append(ra_row)  # riga di A senza corrispondenza in B
            if not uniti:
                return f"[D365 — JOIN DINAMICO] Nessuna corrispondenza tra {ent_a} e {ent_b}."
            header = (f"[D365 — JOIN DINAMICO ({ent_a} ⋈ {ent_b} su {campo_a}={campo_b}, costruito dall'AI)] "
                      f"{len(uniti)} righe. {spieg}{rel_nota}\n"
                      f"⚠️ ATTENZIONE: questi dati sono stati INCROCIATI da una query dinamica "
                      f"costruita dall'AI, non da un modulo collaudato. L'aggancio tra le due "
                      f"entità potrebbe non essere quello corretto: VERIFICA OBBLIGATORIA prima "
                      f"di qualsiasi uso operativo.")
            html_path = self._write_generic_html(f"Join {ent_a} ⋈ {ent_b}", uniti, spieg)
            body = header + "\n" + self._format_records_list(uniti)
            if html_path:
                body = f"[REPORT_HTML: {html_path}]\n" + body
            return body
        except Exception as e:
            _dbg(f"dynamic JOIN: errore {e}")
            return ""

    def _dynamic_resolve(self, r: dict, full_catalog: dict, token: str) -> str:
        """CASO D — RISOLVI-POI-FILTRA (join a due salti). Esempio: 'RDA di Marco
        Bonometti' -> cerca 'Marco Bonometti' in Workers per ottenere la matricola,
        poi filtra PurchaseRequisitionHeaders su quella matricola. Tutto validato e
        eseguito dal codice; l'AI indica solo entità e campi (dal catalogo)."""
        ent_lookup = (r.get("entita_lookup") or "").strip()
        campo_cerca = (r.get("campo_cerca") or "").strip()
        valore_cerca = (r.get("valore_cerca") or "").strip()
        campo_codice = (r.get("campo_codice") or "").strip()
        ent_target = (r.get("entita_target") or "").strip()
        campo_target = (r.get("campo_target") or "").strip()
        filtro_extra = (r.get("filtro_extra") or "").strip()
        spieg = r.get("spiegazione", "")
        _dbg(f"dynamic RISOLVI: {valore_cerca!r} in {ent_lookup}.{campo_cerca} -> "
             f"{campo_codice}, filtro {ent_target}.{campo_target}")

        # SALVAGUARDIA: entità e campi devono esistere nel catalogo
        if not (full_catalog and ent_lookup in full_catalog and ent_target in full_catalog):
            _dbg("dynamic RISOLVI: entità non valide")
            return ""
        campi_lookup = set(full_catalog[ent_lookup]["string"] + full_catalog[ent_lookup]["date"])
        campi_target = set(full_catalog[ent_target]["string"] + full_catalog[ent_target]["date"])
        if campo_cerca not in campi_lookup or campo_codice not in campi_lookup:
            _dbg(f"dynamic RISOLVI: campi lookup inesistenti ({campo_cerca}/{campo_codice})")
            return ""
        if campo_target not in campi_target:
            _dbg(f"dynamic RISOLVI: campo target inesistente ({campo_target})")
            return ""
        if filtro_extra and not self._is_readonly_filter(filtro_extra):
            filtro_extra = ""

        HARD_LIMIT = 50
        cross_company = bool(self.cfg.get("dyn_cross_company", False))
        try:
            # PASSO 1: risolvi il valore leggibile nel/i codice/i (cerca per parti del nome)
            safe = valore_cerca.replace("'", "''")
            # usa contains per tollerare ordine nome/cognome
            parti = [p for p in safe.split() if len(p) >= 2][:3]
            if parti:
                cond = " and ".join(f"contains({campo_cerca},'{p}')" for p in parti)
            else:
                cond = f"contains({campo_cerca},'{safe}')"
            url1 = self._data_url(f"/{ent_lookup}?$filter={quote(cond)}&$top=10")
            if cross_company:
                url1 += "&cross-company=true"
            r1 = self._dyn_get(url1, token, timeout=30)
            look = []
            if r1.status_code == 200:
                look = r1.json().get("value", [])
            else:
                # FALLBACK: il filtro contains su questo campo non è supportato (HTTP 400),
                # oppure il campo nome è diverso. Scarico un blocco di record dall'anagrafica
                # e faccio il match del nome NEL CODICE, su TUTTI i campi stringa (così
                # troviamo il nome anche se non è esattamente nel campo che l'AI ha indicato).
                _dbg(f"dynamic RISOLVI: lookup filtro HTTP {r1.status_code}, fallback a match nel codice")
                url1b = self._data_url(f"/{ent_lookup}?$top=1000")
                if cross_company:
                    url1b += "&cross-company=true"
                r1b = self._dyn_get(url1b, token, timeout=30)
                if r1b.status_code != 200:
                    _dbg(f"dynamic RISOLVI: lookup fallback HTTP {r1b.status_code}")
                    return ""
                tutti = r1b.json().get("value", [])
                campi_str = full_catalog[ent_lookup]["string"]
                parti_low = [p.lower() for p in (parti or [safe])]
                for rec in tutti:
                    # concatena tutti i campi stringa del record e cerca tutte le parti del nome
                    blob = " ".join(str(rec.get(c, "")) for c in campi_str).lower()
                    if all(p in blob for p in parti_low):
                        look.append(rec)
                    if len(look) >= 10:
                        break
            codici = list({rec.get(campo_codice) for rec in look if rec.get(campo_codice) not in (None, "")})
            if not codici:
                return (f"[D365 — RICERCA PER PERSONA] Non ho trovato nessuna persona corrispondente "
                        f"a '{valore_cerca}' nell'anagrafica {ent_lookup}. Verifica il nome, oppure "
                        f"indica direttamente il codice dipendente.")
            nomi_trovati = [f"{rec.get(campo_cerca,'?')} ({rec.get(campo_codice,'?')})" for rec in look][:5]
            _dbg(f"dynamic RISOLVI: codici trovati = {codici[:5]}")

            # PASSO 2: filtra l'entità-bersaglio sui codici trovati
            blocco = codici[:25]
            ors = " or ".join(f"{campo_target} eq '{str(c).replace(chr(39), chr(39)*2)}'" for c in blocco)
            filtro_finale = f"({ors})"
            if filtro_extra:
                filtro_finale += f" and ({filtro_extra})"
            url2 = self._data_url(f"/{ent_target}?$filter={quote(filtro_finale)}&$top={HARD_LIMIT}")
            if cross_company:
                url2 += "&cross-company=true"
            r2 = self._dyn_get(url2, token, timeout=30)
            if r2.status_code == 400 and filtro_extra:
                # ritenta senza il filtro extra (es. data non filtrabile)
                url2b = self._data_url(f"/{ent_target}?$filter={quote('(' + ors + ')')}&$top={HARD_LIMIT}")
                r2 = self._dyn_get(url2b, token, timeout=30)
            if r2.status_code != 200:
                _dbg(f"dynamic RISOLVI: target HTTP {r2.status_code}")
                return ""
            records = r2.json().get("value", [])
            if not records:
                return (f"[D365 — RICERCA PER PERSONA] Persona '{valore_cerca}' trovata "
                        f"(codice {', '.join(str(c) for c in codici[:3])}), ma nessun record in "
                        f"{ent_target} corrispondente per i criteri richiesti.")
            header = (f"[D365 — RICERCA PER PERSONA (risolto dall'AI: {valore_cerca} → "
                      f"{', '.join(str(c) for c in codici[:3])}; entità {ent_target})] "
                      f"{len(records)} record. Persone trovate in anagrafica: {', '.join(nomi_trovati)}. {spieg}\n"
                      f"⚠️ ATTENZIONE: il collegamento nome→codice→documenti è stato costruito da una "
                      f"query dinamica. Verifica che la persona e i documenti siano quelli attesi "
                      f"prima di usare i dati per decisioni operative.")
            html_path = self._write_generic_html(f"{ent_target} per {valore_cerca}", records, spieg)
            body = header + "\n" + self._format_records_list(records)
            if html_path:
                body = f"[REPORT_HTML: {html_path}]\n" + body
            return body
        except Exception as e:
            _dbg(f"dynamic RISOLVI: errore {e}")
            return ""

    # ════════════════════════════════════════════════════════════════════════
    #  v2.0 — MOTORE DINAMICO AGENTICO (multi-step, orientato dal codice)
    #  L'AI agisce a passi: a ogni passo propone UNA azione (interroga, espandi
    #  una relazione reale, risolvi un nome, unisci, oppure concludi); il codice
    #  ESEGUE l'azione, restituisce un'osservazione compatta (conteggio + campione)
    #  e ripete. Il codice resta l'arbitro: valida entità/campi contro il catalogo,
    #  consente i join SOLO lungo relazioni reali ($metadata) o con verifica del
    #  match dei valori, applica read-only e limiti. Loop limitato (MAX_STEP).
    # ════════════════════════════════════════════════════════════════════════
    HARD_LIMIT = 50          # tetto risultati per query (non negoziabile dall'AI)
    MAX_STEP = 8             # passi massimi del loop agentico (8: l'azione 'schema' consuma passi)
    SAMPLE_ROWS = 3          # righe di campione restituite all'AI per osservazione

    def _exec_query(self, entity: str, filtro: str, expand: str, token: str,
                    catalog: dict, top: int = None, campi: list = None) -> dict:
        """Esegue UNA lettura su una entità, con eventuale $filter e $expand
        (server-side join lungo una navigation property REALE). Valida i campi e
        applica read-only. Ritorna {ok, status, rows, count, url}.
        Tutte le salvaguardie sono qui: è l'unico punto che costruisce la query."""
        top = top or self.HARD_LIMIT
        if entity not in catalog:
            return {"ok": False, "status": 0, "rows": [], "count": 0, "errore": f"entità {entity} inesistente"}
        cross_company = bool(self.cfg.get("dyn_cross_company", False))
        # read-only sul filtro
        if filtro and not self._is_readonly_filter(filtro):
            _dbg(f"[READ-ONLY] _exec_query filtro scartato: {filtro[:80]}")
            filtro = ""
        params = [f"$top={top}"]
        # $select: i campi chiesti dall'AI vanno onorati. Senza, la risposta
        # arriva con tutti i campi e il rendering ne mostra solo i primi: un
        # campo richiesto ma lontano nell'ordine sparisce, e chi legge conclude
        # che il dato non esista.
        sel = self._valida_campi(entity, campi, catalog)
        if sel:
            params.append("$select=" + quote(",".join(sel), safe=","))
        if filtro:
            params.insert(0, f"$filter={quote(filtro)}")
        if expand:
            # valida che l'expand sia una navigation property reale dell'entità
            navs = {r["nav"] for r in self._relations_of(entity, catalog)}
            nav_name = expand.split("(", 1)[0].strip()
            if nav_name in navs:
                params.append(f"$expand={expand}")
            else:
                _dbg(f"_exec_query: $expand '{expand}' non è una relazione reale di {entity}, ignorato")
        if cross_company:
            params.append("cross-company=true")
        url = self._data_url(f"/{entity}?" + "&".join(params))
        try:
            _dbg(f"_exec_query: {url[:400]}")
            r = self._dyn_get(url, token, timeout=30)
            if r.status_code == 400 and (filtro or expand or sel):
                # ritenta senza filtro/expand/select per degradare invece di fallire
                r = self._dyn_get(self._data_url(f"/{entity}?$top={top}"
                                                 + ("&cross-company=true" if cross_company else "")),
                                  token, timeout=30)
            if r.status_code != 200:
                return {"ok": False, "status": r.status_code, "rows": [], "count": 0, "url": url}
            rows = r.json().get("value", [])
            return {"ok": True, "status": 200, "rows": rows, "count": len(rows), "url": url}
        except Exception as e:
            return {"ok": False, "status": 0, "rows": [], "count": 0, "errore": str(e)}

    def _conteggio_reale(self, entity: str, filtro: str, token: str) -> int:
        """Righe totali dell'entità (con l'eventuale filtro). None se il
        servizio non risponde in fretta: su tabelle enormi $count e' lento e
        non vale la pena farci aspettare l'utente."""
        try:
            url = self._data_url(f"/{entity}/$count"
                                 + (f"?$filter={quote(filtro)}" if filtro else ""))
            r = self._dyn_get(url, token, timeout=12)
            if r.status_code == 200:
                return int(r.text.strip())
        except Exception:
            pass
        return None

    @staticmethod
    def _valida_campi(entity: str, campi, catalog: dict) -> list:
        """Tiene solo i campi che esistono davvero sull'entità: un nome
        inventato in $select fa fallire la richiesta con HTTP 400."""
        if not campi:
            return []
        ent = catalog.get(entity) or {}
        noti = set(ent.get("string") or []) | set(ent.get("date") or []) \
            | set(ent.get("num") or []) | set((ent.get("enum") or {}).keys())
        if not noti:
            return []
        fuori = [c for c in campi if c not in noti]
        if fuori:
            _dbg(f"_valida_campi: scartati (inesistenti su {entity}): {fuori[:6]}")
        return [c for c in campi if c in noti][:40]

    AGG_MAX_RIGHE = 25000    # tetto di righe scandite per una aggregazione
    AGG_MAX_SEC = 45         # e tetto di tempo: meglio parziale dichiarato che attesa

    def _aggrega(self, entity: str, campo: str, filtro: str, token: str,
                 catalog: dict) -> dict:
        """Conta le occorrenze di un campo su TUTTE le righe, non su un campione.

        D365 F&O non implementa $apply: passandolo, il servizio non da' errore,
        lo ignora e restituisce le righe grezze. Costruirci sopra significherebbe
        scambiare righe non aggregate per aggregati. L'unica via corretta e'
        leggere la sola colonna che serve e contare qui.

        Ritorna {ok, gruppi, distinti, righe, completo}. 'completo' e' falso se
        si e' toccato un tetto: in quel caso il risultato e' un parziale e va
        dichiarato tale, mai presentato come totale.
        """
        import time as _t
        if entity not in catalog:
            return {"ok": False, "errore": f"entità {entity} inesistente"}
        if not self._valida_campi(entity, [campo], catalog):
            return {"ok": False, "errore": f"campo {campo} inesistente su {entity}"}
        if filtro and not self._is_readonly_filter(filtro):
            _dbg(f"[READ-ONLY] _aggrega filtro scartato: {filtro[:80]}")
            filtro = ""

        params = [f"$select={campo}"]
        if filtro:
            params.insert(0, f"$filter={quote(filtro)}")
        if bool(self.cfg.get("dyn_cross_company", False)):
            params.append("cross-company=true")
        url = self._data_url(f"/{entity}?" + "&".join(params))

        conta, righe, completo, t0 = {}, 0, True, _t.time()
        while url:
            if righe >= self.AGG_MAX_RIGHE or (_t.time() - t0) > self.AGG_MAX_SEC:
                completo = False
                break
            try:
                r = self._dyn_get(url, token, timeout=30)
            except Exception as e:
                return {"ok": False, "errore": str(e)}
            if r.status_code != 200:
                return {"ok": False, "errore": f"HTTP {r.status_code}"}
            body = r.json()
            for x in body.get("value", []):
                righe += 1
                v = x.get(campo)
                if v not in (None, ""):
                    conta[v] = conta.get(v, 0) + 1
            url = body.get("@odata.nextLink")
        gruppi = sorted(conta.items(), key=lambda kv: -kv[1])
        _dbg(f"_aggrega {entity}.{campo}: {righe} righe, {len(gruppi)} distinti, "
             f"completo={completo}, {_t.time()-t0:.1f}s")
        return {"ok": True, "gruppi": gruppi, "distinti": len(gruppi),
                "righe": righe, "completo": completo}

    def _avviso_troncamento(self, entity: str, filtro: str, records: list,
                            token: str) -> str:
        """Se la lettura ha toccato il tetto, dirlo senza ambiguita'.

        Con la sola frase "N record" un campione da 50 righe viene letto come
        l'insieme completo: si finisce per dichiarare "in tutto 23 fornitori"
        su un'entita' che ne ha centinaia. Il tetto non e' negoziabile, ma va
        almeno dichiarato."""
        if len(records) < self.HARD_LIMIT:
            return ""
        tot = self._conteggio_reale(entity, filtro, token)
        quanti = (f" L'interrogazione completa ne conta {tot}."
                  if tot is not None else "")
        return ("\n\n⚠️ RISULTATO TRONCATO: sono le prime "
                f"{len(records)} righe, NON l'elenco completo.{quanti} "
                "Non ricavare da questo campione totali, conteggi di valori "
                "distinti, intervalli di date o espressioni come 'in tutto': "
                "presentalo esplicitamente come un campione parziale.")

    def _compact_obs(self, rows: list, max_rows: int = None) -> list:
        """Riduce le righe a un campione compatto (poche righe, solo campi non-@)
        da restituire all'AI come osservazione, senza saturare il contesto."""
        max_rows = max_rows or self.SAMPLE_ROWS
        out = []
        for rec in rows[:max_rows]:
            out.append({k: v for k, v in rec.items()
                        if not k.startswith("@") and not isinstance(v, (dict, list))})
        return out

    def _dynamic_agentic(self, query: str, full_catalog: dict, entities: list,
                         string_fields: dict, date_fields: dict, token: str) -> str:
        """Loop agentico a passi. L'AI propone un'azione per volta; il codice la
        esegue e restituisce l'osservazione. Massimo MAX_STEP passi. Ritorna il
        testo della risposta finale, oppure "" se non conclude."""
        import json, re as _re

        # contesto entità (campi reali) + relazioni reali tra le candidate
        def _desc(ents):
            blocchi = []
            schema_budget = 3500   # tetto ai caratteri di schema .md (anti-bloat)
            schema_used = 0
            for e in ents:
                sf = (string_fields.get(e) or full_catalog.get(e, {}).get("string") or [])[:50]
                df = (date_fields.get(e) or full_catalog.get(e, {}).get("date") or [])[:12]
                base = (f"Entità: {e}\n  Campi testo: {', '.join(sf) or '(nessuno)'}"
                        f"\n  Campi data: {', '.join(df) or '(nessuno)'}")
                # Arricchimento da schema .md (chiavi + enum), finché c'è budget
                extra = ""
                if schema_used < schema_budget:
                    extra = self._entity_schema_extra(e)
                    schema_used += len(extra)
                blocchi.append(base + extra)
            return "\n".join(blocchi)
        rel_txt = self._relations_summary(entities, full_catalog) or "(nessuna relazione nota tra le candidate)"

        system = (
            "Sei un motore di interrogazione per Dynamics 365 F&O che lavora A PASSI. "
            "A ogni passo proponi UNA sola azione in JSON; riceverai un'osservazione "
            "(conteggio righe + campione) e potrai proporre il passo successivo, fino a "
            "concludere. USA ESCLUSIVAMENTE le entità, i campi e le relazioni forniti: non "
            "inventare nomi. Le azioni possibili sono:\n"
            '1) {"azione":"query","entita":"E","filtro":"<OData $filter o vuoto>","campi":[...],"motivo":"..."}\n'
            '   Interroga una entità. Per collegare dati lungo una relazione reale aggiungi "expand":"NavProp($select=campo1,campo2)".\n'
            '2) {"azione":"risolvi","entita_lookup":"E","campo_cerca":"C","valore_cerca":"V","campo_codice":"K","motivo":"..."}\n'
            '   Cerca un valore leggibile (es. nome persona) in una anagrafica e restituisce i codici trovati.\n'
            '3) {"azione":"join","entita_a":"A","campo_a":"Ka","entita_b":"B","campo_b":"Kb","filtro_a":"<o vuoto>","motivo":"..."}\n'
            '   Unisce due entità su una chiave comune (preferisci "expand" nella query se esiste una relazione reale).\n'
            '4) {"azione":"cerca_entita","parola":"<parole inglesi del nome entità>","motivo":"..."}\n'
            '   Se ti serve un\'entità non presente tra le candidate.\n'
            '5) {"azione":"concludi","entita_finale":"E","filtro":"<o vuoto>","expand":"<o vuoto>","campi":[...],"spiegazione":"<sintesi>"}\n'
            "   Quando hai capito quale interrogazione risponde: il codice la esegue e mostra i dati all'utente.\n"
            '6) {"azione":"schema","entita":["E1","E2"],"motivo":"..."}\n'
            "   Chiede la SCHEDA TECNICA di una o più entità (chiavi, valori enum, relazioni): usala quando ti "
            "servono i letterali enum esatti per un $filter o le chiavi/relazioni per un expand/join.\n"
            '7) {"azione":"aggrega","entita":"E","campo":"C","filtro":"<o vuoto>","motivo":"..."}\n'
            "   Conta le occorrenze di un campo su TUTTE le righe, non su un campione. "
            "Usala SEMPRE per domande su quanti, quali valori distinti, classifiche o "
            "totali: una query normale legge solo le prime righe e da' un numero sbagliato.\n"
            "Regole: preferisci 'expand' lungo le relazioni reali elencate; usa 'join' solo se non c'è "
            "relazione reale. Se per un'entità sono indicate 'Chiavi:' o 'Enum:', usa quei campi chiave "
            "per i lookup e quei valori enum come letterali nei $filter. Concludi appena possibile. Sii essenziale.\n"
            "IMPORTANTE su 'campi': determina il $select, cioè COSA VIENE LETTO dal gestionale. "
            "Un campo che non elenchi non arriva nella risposta. La sua assenza NON significa che sia "
            "vuoto nel gestionale: significa solo che non l'hai chiesto. Non dedurre mai che un dato "
            "manchi da un campo che non hai selezionato; se serve, rifai la query includendolo. "
            "Includi sempre i campi su cui verte la domanda dell'utente e quelli usati per gli agganci."
        )

        history = [f"DOMANDA UTENTE:\n{query}",
                   f"\nENTITÀ CANDIDATE E CAMPI REALI:\n{_desc(entities)}",
                   f"\nRELAZIONI REALI TRA LE CANDIDATE (usa queste per i join/expand):\n{rel_txt}"]
        hints = self._domain_hints()
        if hints:
            history.append(f"\nNOTE DI DOMINIO (regole aziendali, valide in qualsiasi lingua della domanda):\n{hints}")
            _dbg(f"schema .md: note di dominio _hints.md iniettate ({len(hints)} char)")
        resolved_codes = {}  # eventuali codici risolti da 'risolvi', riusabili nei filtri
        _dbg("schema .md: chiavi/enum iniettati nel planner"
             if self._schema_dir else "schema .md: non attivo (planner come v2.0)")

        for step in range(self.MAX_STEP):
            user = "\n".join(history) + (
                f"\n\nPasso {step+1}/{self.MAX_STEP}. Proponi la prossima azione in JSON."
            )
            raw = self._ask_ai(system, user, max_tokens=600)
            if not raw:
                if getattr(self, "_ai_overloaded", False):
                    return self._msg_overload()
                _dbg(f"agentic: nessuna risposta AI al passo {step+1}")
                return ""
            m = _re.search(r"\{.*\}", raw, _re.S)
            if not m:
                _dbg(f"agentic: nessun JSON al passo {step+1}: {raw[:100]}")
                return ""
            try:
                act = json.loads(m.group(0))
            except Exception as e:
                _dbg(f"agentic: JSON non valido al passo {step+1}: {e}")
                return ""
            azione = (act.get("azione") or "").strip()
            _dbg(f"agentic passo {step+1}: azione={azione} motivo={str(act.get('motivo',''))[:80]}")

            if azione == "concludi":
                ent = (act.get("entita_finale") or "").strip()
                if ent not in full_catalog:
                    history.append(f"\n[OSSERVAZIONE] Entità '{ent}' inesistente. Scegline una tra le candidate.")
                    continue
                filtro = (act.get("filtro") or "").strip()
                expand = (act.get("expand") or "").strip()
                campi_richiesti = act.get("campi") or []
                res = self._exec_query(ent, filtro, expand, token, full_catalog,
                                       campi=campi_richiesti)
                if not res["ok"]:
                    history.append(f"\n[OSSERVAZIONE] Query finale fallita (HTTP {res['status']}). Riprova diversamente.")
                    continue
                records = res["rows"]
                if not records:
                    return (f"[D365 — RICERCA DINAMICA v2.0] L'interrogazione su {ent} non ha restituito "
                            f"record per questa domanda. {act.get('spiegazione','')}")
                spieg = act.get("spiegazione", "")
                via = f" con espansione {expand}" if expand else ""
                header = (f"[D365 — RICERCA DINAMICA v2.0 (piano costruito dall'AI a passi su {ent}{via})] "
                          f"{len(records)} record. {spieg}\n"
                          f"⚠️ Dati prodotti da una query dinamica multi-step; verifica prima di usarli "
                          f"per decisioni operative.")
                header += self._avviso_troncamento(ent, filtro, records, token)
                html_path = self._write_generic_html(f"Ricerca dinamica v2.0 — {ent}", records, spieg)
                body = header + "\n" + self._format_records_list(records, campi_richiesti)
                if html_path:
                    body = f"[REPORT_HTML: {html_path}]\n" + body
                return body

            elif azione == "query":
                ent = (act.get("entita") or "").strip()
                filtro = (act.get("filtro") or "").strip()
                expand = (act.get("expand") or "").strip()
                # sostituisci eventuali codici risolti referenziati come {codici}
                if "{codici}" in filtro and resolved_codes:
                    vals = list(resolved_codes.values())[0]
                    ors = " or ".join(f"{c}" for c in vals) if vals else ""
                res = self._exec_query(ent, filtro, expand, token, full_catalog,
                                       campi=act.get("campi") or [])
                obs = (f"\n[OSSERVAZIONE passo {step+1}] query {ent}: "
                       f"{'OK' if res['ok'] else 'FALLITA HTTP '+str(res['status'])}, "
                       f"{res['count']} righe. Campione: {json.dumps(self._compact_obs(res['rows']), ensure_ascii=False)[:600]}")
                history.append(obs)

            elif azione == "risolvi":
                el = (act.get("entita_lookup") or "").strip()
                cc = (act.get("campo_cerca") or "").strip()
                vc = (act.get("valore_cerca") or "").strip()
                ck = (act.get("campo_codice") or "").strip()
                codici, nomi = self._resolve_codes(el, cc, vc, ck, token, full_catalog)
                if codici:
                    resolved_codes[ck] = [f"{ck} eq '{str(c).replace(chr(39), chr(39)*2)}'" for c in codici]
                obs = (f"\n[OSSERVAZIONE passo {step+1}] risolvi '{vc}' in {el}.{cc}: "
                       f"trovati {len(codici)} codici {ck} = {codici[:5]}. "
                       f"Per filtrare un'altra entità su questi codici usa un filtro OR su {ck}.")
                history.append(obs)

            elif azione == "join":
                # delega al join deterministico esistente (match esatto nel codice)
                j = {"entita_a": act.get("entita_a",""), "campo_a": act.get("campo_a",""),
                     "entita_b": act.get("entita_b",""), "campo_b": act.get("campo_b",""),
                     "filtro_a": act.get("filtro_a",""), "campi_a": act.get("campi_a",[]),
                     "campi_b": act.get("campi_b",[]), "spiegazione": act.get("motivo","")}
                out = self._dynamic_join(j, full_catalog, string_fields, date_fields, token)
                if out:
                    return out
                history.append(f"\n[OSSERVAZIONE passo {step+1}] join non riuscito; prova un'altra strada.")

            elif azione == "schema":
                ents_req = act.get("entita") or []
                if isinstance(ents_req, str):
                    ents_req = [ents_req]
                ents_req = [str(e).strip() for e in ents_req if str(e).strip()][:4]
                schede = []
                for e in ents_req:
                    if e not in full_catalog:
                        schede.append(f"SCHEDA {e}: entità inesistente nel catalogo.")
                        continue
                    s = self._entity_schema_full(e)
                    schede.append(s if s else f"SCHEDA {e}: non disponibile (schema .md assente).")
                history.append(f"\n[OSSERVAZIONE passo {step+1}]\n" + "\n".join(schede))

            elif azione == "aggrega":
                ent = (act.get("entita") or "").strip()
                campo = (act.get("campo") or "").strip()
                agg = self._aggrega(ent, campo, (act.get("filtro") or "").strip(),
                                    token, full_catalog)
                if not agg.get("ok"):
                    history.append(f"\n[OSSERVAZIONE passo {step+1}] Aggregazione non "
                                   f"riuscita: {agg.get('errore')}. Riprova diversamente.")
                    continue
                testa = agg["gruppi"][:40]
                righe_txt = "; ".join(f"{v}={n}" for v, n in testa)
                stato = ("su TUTTE le righe dell'interrogazione"
                         if agg["completo"] else
                         f"PARZIALE: scansione interrotta al tetto dopo {agg['righe']} "
                         f"righe, quindi i conteggi sono un minimo e i valori distinti "
                         f"possono essere di piu'")
                history.append(
                    f"\n[OSSERVAZIONE passo {step+1}] Aggregazione {ent}.{campo} "
                    f"({stato}): {agg['righe']} righe lette, {agg['distinti']} valori "
                    f"distinti.\nPrimi {len(testa)} per frequenza: {righe_txt}\n"
                    f"Usa questi numeri per la risposta: sono contati, non stimati.")

            elif azione == "cerca_entita":
                parola = (act.get("parola") or "").strip()
                nuove = self._candidates_by_keyword(parola, full_catalog, top=12)
                for e in nuove:
                    string_fields.setdefault(e, full_catalog[e].get("string", []))
                    date_fields.setdefault(e, full_catalog[e].get("date", []))
                entities = list(dict.fromkeys(entities + nuove))[:18]
                rel_txt = self._relations_summary(entities, full_catalog) or "(nessuna relazione nota)"
                history.append(f"\n[OSSERVAZIONE passo {step+1}] nuove entità candidate: {nuove}\n"
                               f"Campi:\n{_desc(nuove)}\nRelazioni aggiornate:\n{rel_txt}")
            else:
                history.append(f"\n[OSSERVAZIONE] Azione '{azione}' non riconosciuta. "
                               f"Usa query/risolvi/join/cerca_entita/schema/concludi.")

        _dbg("agentic: esauriti i passi senza conclusione")
        return ""

    def _resolve_codes(self, ent_lookup, campo_cerca, valore_cerca, campo_codice,
                       token, catalog):
        """Risolve un valore leggibile (es. nome) in uno o più codici dentro
        un'anagrafica. Riusa la logica robusta del caso D: prova contains, poi
        fallback a match nel codice su tutti i campi stringa. Ritorna (codici, nomi)."""
        if ent_lookup not in catalog:
            return [], []
        campi = set(catalog[ent_lookup].get("string", []) + catalog[ent_lookup].get("date", []))
        if campo_cerca not in campi or campo_codice not in campi:
            return [], []
        cross_company = bool(self.cfg.get("dyn_cross_company", False))
        safe = (valore_cerca or "").replace("'", "''")
        parti = [p for p in safe.split() if len(p) >= 2][:3]
        cond = " and ".join(f"contains({campo_cerca},'{p}')" for p in parti) if parti \
               else f"contains({campo_cerca},'{safe}')"
        look = []
        try:
            url = self._data_url(f"/{ent_lookup}?$filter={quote(cond)}&$top=10"
                                 + ("&cross-company=true" if cross_company else ""))
            r = self._dyn_get(url, token, timeout=30)
            if r.status_code == 200:
                look = r.json().get("value", [])
            else:
                url2 = self._data_url(f"/{ent_lookup}?$top=1000"
                                      + ("&cross-company=true" if cross_company else ""))
                r2 = self._dyn_get(url2, token, timeout=30)
                if r2.status_code == 200:
                    tutti = r2.json().get("value", [])
                    campi_str = catalog[ent_lookup].get("string", [])
                    parti_low = [p.lower() for p in (parti or [safe])]
                    for rec in tutti:
                        blob = " ".join(str(rec.get(c, "")) for c in campi_str).lower()
                        if all(p in blob for p in parti_low):
                            look.append(rec)
                        if len(look) >= 10:
                            break
        except Exception as e:
            _dbg(f"_resolve_codes: errore {e}")
        codici = list({rec.get(campo_codice) for rec in look if rec.get(campo_codice) not in (None, "")})
        nomi = [f"{rec.get(campo_cerca,'?')} ({rec.get(campo_codice,'?')})" for rec in look][:5]
        return codici, nomi

    def _candidates_by_keyword(self, parola: str, full_catalog: dict, top: int = 12) -> list:
        """Screma il catalogo per parole chiave sui nomi entità. Usato dal caso B
        e dall'azione 'cerca_entita' del loop agentico."""
        import re as _re
        pk = [p.strip().lower() for p in _re.split(r"[\s,;/]+", parola or "") if len(p.strip()) >= 3]
        if not pk:
            return []
        scored = []
        for name in full_catalog:
            low = name.lower()
            sc = sum(1 for p in pk if p in low)
            if sc:
                scored.append((sc, name))
        scored.sort(key=lambda x: (-x[0], len(x[1])))
        return [n for _, n in scored[:top]]

    def _msg_overload(self) -> str:
        """Messaggio onesto unificato quando il motore AI è sovraccarico (529)."""
        return ("[D365 — SERVIZIO AI TEMPORANEAMENTE NON DISPONIBILE]\n"
                "Non ho potuto completare la richiesta perché i server del motore AI sono "
                "temporaneamente sovraccarichi (errore 529), anche dopo alcuni tentativi "
                "automatici. NON è un problema dei dati né della domanda.\nISTRUZIONE PER "
                "L'ASSISTENTE: invita l'utente a riprovare tra qualche minuto, senza elencare "
                "dati né trarre conclusioni.")

    def dynamic_query(self, query: str, current_user_name: str = "") -> str:
        """MODALITÀ DINAMICA (AI-driven con salvaguardia sui campi reali).

        Usata come fallback quando nessun modulo deterministico copre la domanda.
        Flusso:
          1. Il CODICE raccoglie le entità caricate e i loro campi REALI.
          2. L'AI propone un piano di query (entità, filtro, campi) scegliendo
             SOLO tra entità e campi reali forniti.
          3. Il CODICE valida che entità e campi esistano davvero (salvaguardia 1).
          4. Il CODICE esegue con LIMITE e TIMEOUT imposti (non negoziabili).
          5. Ritorna i risultati grezzi per la presentazione AI.
        """
        token = self.tm.get_access_token()
        if not token or not self.resource_url:
            return ""
        self._ai_overloaded = False  # resettato a ogni nuova richiesta dinamica

        # CATALOGO COMPLETO: se presente, l'AI può scegliere tra TUTTE le entità
        # (non solo quelle eventualmente caricate). Il catalogo è troppo grande
        # per il prompt, quindi prima si restringe per parola chiave sui nomi.
        full_catalog = self.load_catalog()
        string_fields = self.cfg.get("dyn_string_fields", {}) or {}
        date_fields = self.cfg.get("dyn_date_fields", {}) or {}

        if full_catalog:
            # PASSO 1: Claude traduce la domanda italiana in parole-chiave inglesi
            # dei NOMI ENTITÀ Dynamics, per scremare le 4704 entità. Niente mappa
            # statica: Claude conosce la terminologia gestionale IT/EN.
            kw_system = (
                "Dato un testo in italiano su dati gestionali (Dynamics 365 F&O), elenca le "
                "parole chiave INGLESI che probabilmente compaiono nei NOMI delle entità/tabelle "
                "Dynamics pertinenti. Esempi: 'condizioni di pagamento' -> payment terms; "
                "'RDA / richiesta d'acquisto' -> purchase requisition; 'fornitore' -> vendor; "
                "'cliente' -> customer; 'bolla di consegna' -> delivery note packing slip; "
                "'cespiti' -> fixed asset. IMPORTANTE: se il testo cita il NOME di una persona "
                "(es. 'di Marco Bonometti', 'emesse da Mario Rossi'), aggiungi SEMPRE anche "
                "'worker employee' perché servirà l'anagrafica dipendenti per risolvere il nome. "
                "Rispondi SOLO con le parole inglesi separate da spazio, minuscole, senza "
                "punteggiatura né spiegazioni (max 8 parole)."
            )
            kw_raw = self._ask_ai(kw_system, f"Testo: {query}", max_tokens=50)
            import re as _rk
            kw = [w.lower() for w in _rk.findall(r"[a-zA-Z]{3,}", kw_raw or "")][:6]
            # Aggiungi anche le parole della domanda già in inglese (se l'utente le usa)
            stop = {"the","and","for","with","all","что","trovami","tutti","gli","del","della",
                    "dei","delle","mostrami","elencami","dammi","quali","quanti","quante"}
            parole_q = [w.lower() for w in _rk.findall(r"[A-Za-z]{4,}", query) if w.lower() not in stop]
            termini = list(dict.fromkeys(kw + parole_q))  # dedup, ordine preservato
            _dbg(f"dynamic: parole-chiave EN da Claude: {kw} (+ query: {parole_q})")
            if not termini:
                _dbg("dynamic: nessuna parola-chiave per scremare il catalogo")
                # se Claude non ha tradotto (es. 529), segnala se sovraccarico
                if getattr(self, "_ai_overloaded", False):
                    return ("[D365 — SERVIZIO AI TEMPORANEAMENTE NON DISPONIBILE]\n"
                            "Non ho potuto elaborare la richiesta: i server del motore AI sono "
                            "temporaneamente sovraccarichi (errore 529).\nISTRUZIONE PER "
                            "L'ASSISTENTE: invita l'utente a riprovare tra qualche minuto, senza "
                            "elencare dati né trarre conclusioni.")
                return ""
            # Scrematura del catalogo per le parole-chiave (sui nomi entità)
            scored = []
            for name in full_catalog:
                low = name.lower()
                sc = sum(1 for t in termini if t in low)
                if sc:
                    scored.append((sc, name))
            scored.sort(key=lambda x: (-x[0], len(x[1])))
            lexical = [n for _, n in scored[:12]]
            # [v2.0] SCOPERTA SEMANTICA: aggiunge entità vicine per significato anche
            # se il nome non contiene la parola chiave (usa embeddings se disponibili).
            semantic = self._semantic_candidates(query, full_catalog, top_k=12)
            if semantic:
                _dbg(f"dynamic v2.0: candidate semantiche: {semantic[:8]}")
            # unione: prima le lessicali (match esatto di nome), poi le semantiche
            entities = list(dict.fromkeys(lexical + semantic))[:16] or list(full_catalog.keys())[:12]
            string_fields = {**string_fields,
                             **{e: full_catalog[e]["string"] for e in entities if e in full_catalog}}
            date_fields = {**date_fields,
                           **{e: full_catalog[e]["date"] for e in entities if e in full_catalog}}
            _dbg(f"dynamic: catalogo completo ({len(full_catalog)} entità), candidate: {entities}")

            # [v2.0] MOTORE AGENTICO: l'AI lavora a passi (interroga, espande
            # relazioni reali, risolve nomi, unisce) con il codice come arbitro.
            # È il percorso primario della 2.0. Se non conclude, si degrada al
            # piano a colpo singolo (casi A/B/C/D) più sotto.
            if self.cfg.get("dyn_agentic", True):
                ag = self._dynamic_agentic(query, full_catalog, list(entities),
                                           string_fields, date_fields, token)
                if ag:
                    return ag
                if getattr(self, "_ai_overloaded", False):
                    return self._msg_overload()
                _dbg("dynamic: il loop agentico non ha concluso, ripiego sul piano a colpo singolo")
        else:
            # Nessun catalogo: ripiega sulle entità caricate (comportamento precedente)
            entities_cfg = self.cfg.get("dyn_entities", []) or []
            if isinstance(entities_cfg, str):
                entities_cfg = [e.strip() for e in entities_cfg.splitlines() if e.strip()]
            entities = [e.split(":", 1)[0].strip() if ":" in e else e.strip()
                        for e in entities_cfg if e.strip()]
            if not entities:
                return ""
            miss_s = [e for e in entities if e not in string_fields]
            if miss_s:
                string_fields = {**string_fields, **self.fetch_string_fields(miss_s)}
            miss_d = [e for e in entities if e not in date_fields]
            if miss_d:
                date_fields = {**date_fields, **self.fetch_date_fields(miss_d)}

        # Descrizione delle entità reali per l'AI. Includo più campi e do
        # priorità a quelli "anagrafici" (cliente/fornitore/conto) così Claude
        # li vede sempre, anche se in lista compaiono oltre la soglia.
        def _prioritize(fields):
            chiavi = ("custom", "vendor", "account", "cliente", "fornitor",
                      "invoice", "sold", "deliver", "party", "name", "number", "id")
            pri = [f for f in fields if any(k in f.lower() for k in chiavi)]
            resto = [f for f in fields if f not in pri]
            return pri + resto
        catalogo = []
        for e in entities:
            sf = _prioritize(string_fields.get(e) or [])[:60]
            df = (date_fields.get(e) or [])[:15]
            catalogo.append(f"Entità: {e}\n  Campi testo: {', '.join(sf) or '(nessuno)'}"
                            f"\n  Campi data: {', '.join(df) or '(nessuno)'}")
        catalogo_txt = "\n".join(catalogo)

        # 2. Chiedi all'AI un piano di query in JSON
        system = (
            "Sei un assistente che traduce una domanda in una query OData per Dynamics 365 F&O. "
            "DEVI usare ESCLUSIVAMENTE le entità e i campi forniti nel catalogo: non inventare nomi. "
            "Rispondi SOLO con un oggetto JSON valido, senza testo attorno.\n"
            "CASO A — una delle entità elencate può rispondere:\n"
            '{"entita": "NomeEntita", "filtro": "<espressione OData $filter o vuoto>", '
            '"campi": ["campo1","campo2"], "spiegazione": "<breve>"}\n'
            "CASO B — nessuna delle entità elencate è adatta, ma sai quale tipo di entità "
            "servirebbe (la cercherò io nel catalogo completo):\n"
            '{"entita": "", "parola_ricerca": "<1-3 parole inglesi del nome entità da cercare, '
            'es. payment terms, customer, delivery>"}\n'
            "CASO C — la domanda richiede di INCROCIARE due entità (es. ordini + anagrafica "
            "cliente). Indica le due entità e il campo di aggancio in CIASCUNA (devono "
            "contenere lo STESSO valore per collegare le righe):\n"
            '{"join": {"entita_a": "NomeA", "campo_a": "CampoChiaveA", '
            '"entita_b": "NomeB", "campo_b": "CampoChiaveB", '
            '"filtro_a": "<filtro OData su A o vuoto>", '
            '"campi_a": ["..."], "campi_b": ["..."], "spiegazione": "<breve>"}}\n'
            "CASO D — la domanda filtra per un valore LEGGIBILE (es. NOME di una persona) che "
            "nell'entità-bersaglio è memorizzato come CODICE (es. matricola). Prima risolvi il "
            "valore leggibile in codice nell'entità anagrafica, poi filtra l'entità-bersaglio:\n"
            '{"risolvi": {"entita_lookup": "Entità anagrafica (es. Workers/Employees)", '
            '"campo_cerca": "campo su cui cercare il valore leggibile (es. Name)", '
            '"valore_cerca": "il valore leggibile dalla domanda (es. Marco Bonometti)", '
            '"campo_codice": "campo che contiene il codice da estrarre (es. PersonnelNumber)", '
            '"entita_target": "Entità-bersaglio (es. PurchaseRequisitionHeaders)", '
            '"campo_target": "campo dell\'entità-bersaglio da filtrare col codice (es. PreparerPersonnelNumber)", '
            '"filtro_extra": "<altro filtro OData sull\'entità-bersaglio o vuoto, es. data>", '
            '"campi_target": ["..."], "spiegazione": "<breve>"}}\n'
            "Regole: usa il CASO C per incroci a chiave comune; usa il CASO D quando devi prima "
            "tradurre un nome/etichetta in un codice. Ogni entità e campo deve esistere nel catalogo."
        )
        user = f"CATALOGO ENTITÀ E CAMPI REALI:\n{catalogo_txt}\n\nDOMANDA:\n{query}"
        _dbg(f"dynamic: invio piano AI (motore={self.cfg.get('ai_engine','?')}) per domanda: {query[:80]}")
        _dbg(f"dynamic: catalogo {len(entities)} entità, {sum(len(string_fields.get(e) or []) for e in entities)} campi totali")
        raw = self._ask_ai(system, user, max_tokens=500)
        if not raw:
            if getattr(self, "_ai_overloaded", False):
                _dbg("dynamic: motore AI sovraccarico (529) dopo i retry")
                return ("[D365 — SERVIZIO AI TEMPORANEAMENTE NON DISPONIBILE]\n"
                        "Non ho potuto elaborare la richiesta perché i server del motore AI "
                        "sono temporaneamente sovraccarichi (errore 529), anche dopo alcuni "
                        "tentativi automatici. NON è un problema dei dati né della domanda.\n"
                        "ISTRUZIONE PER L'ASSISTENTE: comunica all'utente che il servizio AI è "
                        "temporaneamente sovraccarico e di riprovare tra qualche minuto. NON "
                        "elencare dati né trarre conclusioni: non hai potuto interrogare l'ERP.")
            _dbg("dynamic: nessuna risposta dal motore AI (motore non raggiungibile o chiave assente?)")
            return ""
        # Estrai il JSON (l'AI a volte lo avvolge in ```json)
        import json, re as _re
        m = _re.search(r"\{.*\}", raw, _re.S)
        if not m:
            _dbg(f"dynamic: nessun JSON nel piano AI: {raw[:120]}")
            return ""
        try:
            plan = json.loads(m.group(0))
        except Exception as e:
            _dbg(f"dynamic: JSON non valido: {e}")
            return ""

        # CASO C: JOIN tra due entità — gestito da un metodo dedicato che fa
        # l'unione NEL CODICE (match esatto sui valori chiave), non nell'AI.
        if isinstance(plan.get("join"), dict):
            return self._dynamic_join(plan["join"], full_catalog, string_fields,
                                      date_fields, token)

        # CASO D: RISOLVI-POI-FILTRA (join a due salti, es. nome persona -> matricola
        # -> documenti). Il codice risolve il nome in codice e filtra il bersaglio.
        if isinstance(plan.get("risolvi"), dict):
            return self._dynamic_resolve(plan["risolvi"], full_catalog, token)

        ent = (plan.get("entita") or "").strip()
        # CASO B: l'AI non ha trovato un'entità adatta tra le candidate iniziali.
        # Con il catalogo completo NON chiediamo all'utente di "caricare" nulla
        # (le ha già tutte): cerchiamo noi nel catalogo con la parola suggerita
        # dall'AI e ri-interroghiamo l'AI con le nuove candidate (un solo retry).
        if not ent:
            parola = (plan.get("parola_ricerca") or "").strip()
            if parola and full_catalog:
                _dbg(f"dynamic: caso B, ri-cerco nel catalogo con: {parola!r}")
                pk = [p.strip().lower() for p in _re.split(r"[\s,;/]+", parola) if len(p.strip()) >= 3]
                rescored = []
                for name in full_catalog:
                    low = name.lower()
                    sc = sum(1 for p in pk if p in low)
                    if sc:
                        rescored.append((sc, name))
                rescored.sort(key=lambda x: (-x[0], len(x[1])))
                nuove = [n for _, n in rescored[:12]]
                if nuove:
                    for e in nuove:
                        string_fields[e] = full_catalog[e]["string"]
                        date_fields[e] = full_catalog[e]["date"]
                    cat2 = []
                    for e in nuove:
                        sf = _prioritize(string_fields.get(e) or [])[:60]
                        df = (date_fields.get(e) or [])[:15]
                        cat2.append(f"Entità: {e}\n  Campi testo: {', '.join(sf) or '(nessuno)'}"
                                    f"\n  Campi data: {', '.join(df) or '(nessuno)'}")
                    user2 = f"CATALOGO ENTITÀ E CAMPI REALI:\n{chr(10).join(cat2)}\n\nDOMANDA:\n{query}"
                    _dbg(f"dynamic: retry con candidate raffinate: {nuove}")
                    raw2 = self._ask_ai(system, user2, max_tokens=500)
                    m2 = _re.search(r"\{.*\}", raw2 or "", _re.S)
                    if m2:
                        try:
                            plan = json.loads(m2.group(0))
                            ent = (plan.get("entita") or "").strip()
                            entities = nuove
                        except Exception:
                            pass
            if not ent:
                if getattr(self, "_ai_overloaded", False):
                    _dbg("dynamic: motore AI sovraccarico (529) durante il retry caso B")
                    return ("[D365 — SERVIZIO AI TEMPORANEAMENTE NON DISPONIBILE]\n"
                            "Avevo individuato le entità giuste, ma non ho potuto completare "
                            "perché i server del motore AI sono temporaneamente sovraccarichi "
                            "(errore 529), anche dopo alcuni tentativi automatici.\n"
                            "ISTRUZIONE PER L'ASSISTENTE: comunica all'utente che il servizio AI "
                            "è temporaneamente sovraccarico e di riprovare tra qualche minuto. NON "
                            "elencare dati né trarre conclusioni.")
                _dbg("dynamic: nessuna entità adatta trovata nemmeno dopo il retry")
                return ""
        # Se l'entità scelta non è tra le candidate ma è nel catalogo completo,
        # accettala e caricane i campi dal catalogo.
        if ent not in entities:
            if full_catalog and ent in full_catalog:
                string_fields[ent] = full_catalog[ent]["string"]
                date_fields[ent] = full_catalog[ent]["date"]
                _dbg(f"dynamic: entità {ent} dal catalogo completo (non tra le candidate)")
            else:
                _dbg(f"dynamic: entità proposta non valida: {ent!r}")
                return ""

        # 3. SALVAGUARDIA 1: valida campi contro la lista reale
        valid_fields = set((string_fields.get(ent) or []) + (date_fields.get(ent) or []))
        filtro = (plan.get("filtro") or "").strip()
        campi = [c for c in (plan.get("campi") or []) if c in valid_fields]
        # SALVAGUARDIA READ-ONLY: rifiuta filtri con sintassi non di sola lettura
        if filtro and not self._is_readonly_filter(filtro):
            _dbg(f"[READ-ONLY] filtro AI scartato (sintassi sospetta): {filtro[:80]}")
            filtro = ""
        # Verifica che i campi citati nel filtro esistano davvero
        if filtro:
            cited = set(_re.findall(r"[A-Za-z_][A-Za-z0-9_]*", filtro))
            odata_kw = {"eq","ne","gt","ge","lt","le","and","or","not","contains",
                        "startswith","endswith","true","false","null","Microsoft",
                        "Dynamics","DataEntities"}
            unknown = [c for c in cited if c[0].isalpha() and c not in valid_fields
                       and c not in odata_kw and not c.isupper() and len(c) > 2]
            # Se il filtro cita campi inesistenti, scartalo (esegui senza filtro)
            if unknown:
                _dbg(f"dynamic: filtro scartato, campi non reali: {unknown}")
                filtro = ""

        # 4. Esegui con LIMITE e TIMEOUT imposti dal codice (non dall'AI)
        HARD_LIMIT = 50
        cross_company = bool(self.cfg.get("dyn_cross_company", False))
        params = [f"$top={HARD_LIMIT}"]
        if filtro:
            params.insert(0, f"$filter={quote(filtro)}")
        if cross_company:
            params.append("cross-company=true")
        url = self._data_url(f"/{ent}?" + "&".join(params))
        try:
            r = self._dyn_get(url, token, timeout=30)
            _dbg(f"dynamic: query AI su {ent} (filtro={'sì' if filtro else 'no'}) -> HTTP {r.status_code}")
            # Se il filtro AI causa 400, riprova senza filtro
            if r.status_code == 400 and filtro:
                r = self._dyn_get(self._data_url(f"/{ent}?$top={HARD_LIMIT}"), token, timeout=30)
                _dbg(f"dynamic: retry senza filtro -> HTTP {r.status_code}")
            if r.status_code != 200:
                return ""
            records = r.json().get("value", [])
            if not records:
                return (f"[D365 — RICERCA DINAMICA] L'entità {ent} è stata interrogata ma non "
                        f"ha restituito record per questa domanda.")
            spiegazione = plan.get("spiegazione", "")
            header = (f"[D365 — RICERCA DINAMICA (query costruita dall'AI sui campi reali di {ent})] "
                      f"{len(records)} record. {spiegazione}\n"
                      f"NOTA: questa risposta NON proviene da un modulo dedicato ma da una query "
                      f"dinamica; verifica i dati prima di usarli per decisioni operative.")
            header += self._avviso_troncamento(ent, filtro, records, token)
            body = header + "\n" + self._format_records_list(records, campi)
            html_path = self._write_generic_html(f"Ricerca dinamica — {ent}", records, spiegazione)
            if html_path:
                body = f"[REPORT_HTML: {html_path}]\n" + body
            return body
        except Exception as e:
            _dbg(f"dynamic: errore esecuzione {e}")
            return ""

    def search(self, query: str, max_results: int = 5, current_user_name: str = "") -> str:
        """Cerca nei Data Entity configurati e ritorna testo formattato.

        Dispatcher per tipo di intento:
        - quantitativo (quanti/quante/numero/totale)  → count_records()
        - lista (mostrami/elencami/ultimi N/recenti)  → list_records()
        - altrimenti                                   → ricerca testuale contains
        """
        # Normalizza max_results (config potenzialmente corrotta)
        try:
            max_results = max(1, min(int(max_results), 100))
        except (ValueError, TypeError):
            max_results = 5

        # PREFISSO /dyn (dyn_force_dynamic): per questo messaggio salta i moduli
        # deterministici (ordini in ritardo, conteggio) e va DIRETTO alla modalità
        # dinamica AI-driven. Serve a confrontare A/B il modulo dedicato con la
        # dinamica sulla stessa domanda, senza disattivare nulla in modo permanente.
        if self.cfg.get("dyn_force_dynamic", False):
            _dbg("search: /dyn attivo — bypass moduli deterministici, vado alla modalità dinamica")
            token = self.tm.get_access_token()
            if not token or not self.resource_url:
                return ""
            dyn = self.dynamic_query(query, current_user_name=current_user_name)
            if dyn:
                return dyn
            _dbg("search: /dyn — la dinamica non ha prodotto risultati")
            return ("[D365 — /dyn] La modalità dinamica non ha prodotto un risultato per questa "
                    "domanda. Nota: con /dyn i moduli dedicati (es. ordini in ritardo) sono "
                    "disattivati; togli /dyn per usarli.")

        # 0. Ordini in ritardo / scaduti (intento più specifico, va prima)
        if _detect_overdue_intent(query):
            want_planning = _detect_planning_enrichment(query)
            ovd = self.find_overdue(query, top=20, current_user_name=current_user_name,
                                    with_planning=want_planning)
            if ovd:
                return ovd

        # 1. Conteggio
        if _detect_quantitative_intent(query):
            cnt = self.count_records(query, current_user_name=current_user_name)
            if cnt:
                return cnt

        # 1b. Fallback multilingua per i moduli deterministici.
        #     Se l'utente lavora in una lingua diversa dall'italiano e i
        #     rilevatori (italiani) NON hanno riconosciuto l'intento sulla
        #     domanda originale, l'AI traduce la domanda in italiano e si
        #     ri-eseguono i rilevatori sulla traduzione. Se ora combaciano, si
        #     usa il modulo deterministico (che esegue IDENTICO a sempre): l'AI
        #     fa SOLO la traduzione, l'esecuzione resta deterministica. Le lingue
        #     non riconosciute ricadono comunque nella modalità dinamica.
        #     Guard: solo per utenti con lingua esplicitamente non italiana
        #     (Italiano/Auto non pagano alcuna chiamata aggiuntiva).
        reply_lang = self.cfg.get("reply_lang", "Auto")
        if reply_lang not in ("", "Italiano", "Auto"):
            it_query = self._translate_to_italian(query)
            if it_query and it_query.strip().lower() != query.strip().lower():
                _dbg(f"search: fallback multilingua — '{query}' -> IT '{it_query}'")
                if _detect_overdue_intent(it_query):
                    want_planning = _detect_planning_enrichment(it_query)
                    ovd = self.find_overdue(it_query, top=20,
                                            current_user_name=current_user_name,
                                            with_planning=want_planning)
                    if ovd:
                        return ovd
                if _detect_quantitative_intent(it_query):
                    cnt = self.count_records(it_query, current_user_name=current_user_name)
                    if cnt:
                        return cnt

        token = self.tm.get_access_token()
        if not token or not self.resource_url:
            return ""

        is_list, n = _detect_list_intent(query)

        # 2. MODALITÀ DINAMICA (AI-driven) — percorso di DEFAULT per tutto ciò
        #    che i moduli specifici (ordini in ritardo, conteggio) non coprono.
        #    Con il catalogo completo, l'AI individua l'entità giusta tra tutte le
        #    4704 (anche non caricate), costruisce la query e, se serve, incrocia
        #    due entità (join assistito). Viene PRIMA della lista/ricerca testuale
        #    generica. Se non produce nulla, si degrada ai percorsi sotto.
        if self.cfg.get("dyn_dynamic_mode", True):
            dyn = self.dynamic_query(query, current_user_name=current_user_name)
            if dyn:
                return dyn

        # 3. Lista / Mostra esplicita (fallback se la dinamica non ha risposto)
        if is_list:
            lst = self.list_records(query, top=n, current_user_name=current_user_name)
            if lst:
                return lst

        # 4. Frase naturale → list_records senza filtri di lista espliciti
        is_natural_sentence = len(query.split()) >= 4 or any(
            w in query.lower() for w in [" le ", " la ", " gli ", " i ", " del ", " di ", "?"]
        )
        if is_natural_sentence:
            lst = self.list_records(query, top=max_results, current_user_name=current_user_name)
            if lst:
                return lst

        entities_cfg = self.cfg.get("dyn_entities", []) or []
        if isinstance(entities_cfg, str):
            entities_cfg = [e.strip() for e in entities_cfg.splitlines() if e.strip()]
        # Compatibilita': se una voce ha il vecchio formato "Entita:campi",
        # tieni solo il nome entita' (i campi ora sono automatici).
        entities = []
        for raw in entities_cfg:
            name = raw.split(":", 1)[0].strip() if ":" in raw else raw.strip()
            if name:
                entities.append(name)
        if not entities:
            return ""

        # Cache campi testuali (entity -> [campi]). Se manca, prova a popolarla.
        string_fields = self.cfg.get("dyn_string_fields", {}) or {}
        missing = [e for e in entities if e not in string_fields]
        if missing:
            fetched = self.fetch_string_fields(missing)
            string_fields = {**string_fields, **fetched}

        cross_company = bool(self.cfg.get("dyn_cross_company", False))
        safe_q = query.replace("'", "''")

        # Limita il numero di campi nel filtro per non generare URL enormi
        try:
            MAX_FIELDS = int(self.cfg.get("dyn_max_filter_fields", 8))
        except (ValueError, TypeError):
            MAX_FIELDS = 8

        parts = []
        errors = []
        for entity in entities:
            try:
                fields = (string_fields.get(entity) or [])[:MAX_FIELDS]
                if not fields:
                    # Nessun campo testuale noto -> salta la ricerca testuale
                    continue

                clauses = [f"contains({f},'{safe_q}')" for f in fields]
                filt = " or ".join(clauses)

                params = [f"$filter={quote(filt)}", f"$top={max_results}"]
                if cross_company:
                    params.append("cross-company=true")
                url = self._data_url(f"/{entity}?" + "&".join(params))

                import requests as req
                r = self._dyn_get(url, token, timeout=25)

                if r.status_code == 401:
                    new_token = self.tm._refresh(
                        self.tm._token_data.get("refresh_token", ""))
                    if new_token:
                        token = new_token
                        r = self._dyn_get(url, new_token, timeout=25)

                if r.status_code != 200:
                    errors.append((entity, r.status_code))
                    continue

                records = r.json().get("value", [])
                if not records:
                    continue

                parts.append(self._format_records(entity, records))
            except Exception:
                continue

        combined = self._combine_results(parts, errors)
        # FALLBACK DINAMICO: se nessun modulo deterministico e nessuna ricerca
        # testuale ha prodotto dati, prova la modalità dinamica AI-driven.
        if not combined or "nessun dato" in combined.lower():
            dyn = self.dynamic_query(query, current_user_name=current_user_name)
            if dyn:
                return dyn
        return combined

    def _combine_results(self, parts: list, errors: list) -> str:
        """Unisce risultati ed errori in modo che i DATI dominino sugli errori.

        Se c'è almeno un risultato, gli errori su altre entità diventano una
        nota a piè di pagina discreta — così l'AI usa i dati invece di
        concludere 'non ho accesso'. Se NON c'è nessun dato, restituisce un
        messaggio neutro che spiega la situazione senza allarmismo.
        """
        data = [p for p in parts if p]
        if data:
            out = "\n\n".join(data)
            if errors:
                # Nota discreta, non allarmante: alcune entità non accessibili
                skipped = ", ".join(f"{e}" for e, _ in errors)
                out += (f"\n\n(Nota tecnica: alcune entità non interrogabili "
                        f"con i permessi attuali: {skipped}. I dati sopra sono "
                        f"comunque validi e completi per le entità accessibili.)")
            return out
        # Nessun dato
        if errors:
            return ("Nessun dato accessibile per questa richiesta con i permessi "
                    "attuali dell'utente. Questo NON è un errore dell'applicazione: "
                    "l'utente F&O potrebbe non avere i diritti di lettura su queste "
                    "entità. Suggerire di verificare i ruoli di sicurezza in Dynamics.")
        return ""

    def _format_records(self, entity: str, records: list) -> str:
        """Formatta i record OData in testo leggibile per il contesto AI."""
        lines = [f"[D365 — {entity}] {len(records)} risultato/i:"]
        for rec in records:
            # Mostra solo i campi non nulli e non interni (@odata.*)
            fields = {k: v for k, v in rec.items()
                      if not k.startswith("@") and v not in (None, "", [])}
            # Limita per leggibilita'
            shown = list(fields.items())[:12]
            row = "; ".join(f"{k}={v}" for k, v in shown)
            lines.append(f"  - {row}")
        return "\n".join(lines)


# ── Istanza globale ──────────────────────────────────────────
_dyn_instance = None

def get_dyn(cfg: dict) -> DynamicsSearch:
    global _dyn_instance
    key_changed = (
        _dyn_instance is None
        or _dyn_instance.cfg.get("dyn_client_id") != cfg.get("dyn_client_id")
        or _dyn_instance.cfg.get("dyn_resource_url") != cfg.get("dyn_resource_url")
    )
    if key_changed:
        _dyn_instance = DynamicsSearch(cfg)
    else:
        _dyn_instance.cfg = cfg
    return _dyn_instance


def warm_semantic_index(cfg: dict | None = None) -> bool:
    """Precostruisce l'indice semantico all'avvio del processo, se il catalogo
    è già su disco: il PRIMO utente non paga i ~2 minuti di costruzione.
    Non richiede credenziali (legge solo il catalogo locale)."""
    try:
        ds = DynamicsSearch(dict(cfg or {}))
        cat = ds.load_catalog()
        if not cat:
            _dbg("semantic warm-up: catalogo assente, salto")
            return False
        return _semantic_ensure(cat, cfg or {}) is not None
    except Exception as e:
        _dbg(f"semantic warm-up: non riuscito ({e})")
        return False

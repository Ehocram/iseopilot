"""Configurazione dello snapshot D365 F&O."""
from __future__ import annotations

import os
from pathlib import Path

# Ambiente ISEO (sovrascrivibile da env o da --resource)
RESOURCE = os.environ.get(
    "D365_RESOURCE", "https://isd365-prod.operations.eu.dynamics.com"
).rstrip("/")

# App registration gia' usata da IseoPilot per il connettore Dynamics.
# Client pubblico (device code): il client_id NON e' un segreto.
CLIENT_ID = os.environ.get("D365_CLIENT_ID", "c5a90f54-d599-4f71-a98f-0fa0781145c1")
TENANT_ID = os.environ.get("D365_TENANT_ID", "a97887fe-14ea-46bc-afa8-f7b85f2164ff")

AUTHORITY = f"https://login.microsoftonline.com/{TENANT_ID}"

# Token dedicato allo snapshot: NON tocchiamo quello di IseoPilot, cosi' un
# rinnovo qui non invalida il refresh token del connettore in produzione.
TOKEN_FILE = Path(
    os.environ.get("D365_TOKEN_FILE", str(Path.home() / ".d365_snapshot_token.json"))
)

# Token di IseoPilot, usabile solo in sola lettura con --borrow-token.
ISEOPILOT_TOKEN = Path.home() / ".chat_assistant_dyn_token.json"

# Societa' su cui misurare i dati. Vuoto = tutte (cross-company).
COMPANY = os.environ.get("D365_COMPANY", "")

OUT_DIR = Path(
    os.environ.get("D365_SNAPSHOT_OUT", str(Path.home() / "IseoPilot" / "data" / "d365_snapshot"))
)

# Prudenza verso la PRODUZIONE: poche richieste in volo, backoff generoso.
HARVEST_CONCURRENCY = int(os.environ.get("D365_HARVEST_CONCURRENCY", "6"))
PROFILE_CONCURRENCY = int(os.environ.get("D365_PROFILE_CONCURRENCY", "3"))
# Le etichette sono GET minuscole sul servizio metadati (nessun accesso ai dati
# applicativi): si puo' spingere di piu' senza pesare sul gestionale.
LABEL_CONCURRENCY = int(os.environ.get("D365_LABEL_CONCURRENCY", "12"))
HTTP_TIMEOUT = int(os.environ.get("D365_HTTP_TIMEOUT", "90"))
MAX_RETRIES = 4

# Campi che non vanno mai considerati chiave esterna nell'inferenza: troppo
# generici, genererebbero migliaia di relazioni fantasma.
NOISE_FIELDS = {
    "dataareaid", "name", "description", "lineNumber".lower(), "linenum",
    "id", "code", "type", "status", "value", "amount", "quantity", "text",
    "createddatetime", "modifieddatetime", "createdby", "modifiedby",
    "recid", "partition", "sequencenumber", "number", "note", "comment",
    "startdate", "enddate", "fromdate", "todate", "language", "currency",
}

# Prefissi AOT -> dominio funzionale, per raggruppare le entita' nella
# fotografia e partizionare gli ERD.
DOMAINS = [
    ("Purch", "Acquisti"), ("Vend", "Fornitori"), ("Procurement", "Acquisti"),
    ("PurchaseRequisition", "Acquisti"), ("PurchaseOrder", "Acquisti"),
    ("Sales", "Vendite"), ("Cust", "Clienti"), ("Customer", "Clienti"),
    ("Invent", "Magazzino"), ("Inventory", "Magazzino"), ("Warehouse", "Magazzino"),
    ("WHS", "Magazzino"), ("Released", "Prodotti"), ("Product", "Prodotti"),
    ("Eco", "Prodotti"), ("Engineering", "Prodotti"),
    ("Prod", "Produzione"), ("Production", "Produzione"), ("Bom", "Produzione"),
    ("Route", "Produzione"), ("Planned", "Pianificazione"), ("ReqPlan", "Pianificazione"),
    ("MasterPlan", "Pianificazione"), ("Forecast", "Pianificazione"),
    ("Ledger", "Contabilita"), ("General", "Contabilita"), ("Fiscal", "Contabilita"),
    ("Tax", "Fiscale"), ("Vat", "Fiscale"), ("Bank", "Tesoreria"),
    ("Asset", "Cespiti"), ("Budget", "Budget"), ("Project", "Progetti"), ("Proj", "Progetti"),
    ("Hcm", "Risorse umane"), ("Hrm", "Risorse umane"), ("Worker", "Risorse umane"),
    ("Payroll", "Risorse umane"), ("Compensation", "Risorse umane"),
    ("Sys", "Sistema"), ("Security", "Sistema"), ("Batch", "Sistema"),
    ("Data", "Sistema"), ("Workflow", "Sistema"), ("Document", "Sistema"),
    ("Quality", "Qualita"), ("Service", "Assistenza"), ("Retail", "Retail"),
    ("Transportation", "Trasporti"), ("Shipping", "Trasporti"),
]


def domain_of(name: str, label: str = "") -> str:
    for prefix, dom in DOMAINS:
        if name.startswith(prefix):
            return dom
    return "Altro"

"""
powerbi_search.py — Connettore Power BI (REST API, identità DELEGATA per-utente).

Principi (allineati al resto di ISEOPilot):
  * OGNI query gira col token OAuth dell'UTENTE (device code, come OneDrive e
    Dynamics): workspace visibili, permessi sui dataset e Row-Level Security
    sono quelli della sua utenza. Nessun account di servizio, nessun bypass.
  * Canale primario: endpoint REST `executeQueries` (JSON) — funziona anche su
    capacità condivisa Pro. Vincoli documentati Microsoft: una query per
    chiamata, una tabella per query, servono permessi Lettura + BUILD sul
    dataset e il tenant setting "Dataset Execute Queries REST API" abilitato.
  * Arricchimento opzionale: endpoint `executeDaxQueries` (risposta Apache
    Arrow) per leggere l'elenco MISURE via funzioni INFO — disponibile SOLO
    per dataset su capacità dedicata e solo se `pyarrow` è installato. Se non
    disponibile, il catalogo lo dice chiaramente: niente degradi silenziosi.
  * Catalogo PER-UTENTE: i workspace visibili dipendono dai privilegi, quindi
    ogni utente genera e usa il proprio catalogo (file accanto al suo token).
  * Sola lettura per costruzione: EVALUATE non può modificare dati.
  * Fail loud: catalogo assente, token scaduto, permesso Build mancante o
    tenant setting spento producono messaggi espliciti, mai stringhe vuote
    mascherate da "nessun risultato".

A differenza di onedrive_search/dynamics_search, questo modulo NON usa una
costante di modulo TOKEN_FILE da reindirizzare sotto lock: percorso token e
percorso catalogo arrivano nel cfg dell'istanza. Le ricerche Power BI di
utenti diversi possono quindi correre in parallelo senza serializzazione.
"""
from __future__ import annotations

import datetime
import json
import re
import time
import unicodedata
from pathlib import Path

import requests

PBI_API = "https://api.powerbi.com/v1.0/myorg"
PBI_RESOURCE = "https://analysis.windows.net/powerbi/api"
# .default = permessi delegati configurati sull'app registration
# (Dataset.Read.All, Workspace.Read.All, Report.Read.All) + refresh token.
PBI_SCOPE = PBI_RESOURCE + "/.default offline_access"

CATALOG_VERSION = "1.0"

# Tabelle-data automatiche di Power BI Desktop: rumore di modello, non dati
# di business. Filtrarle è deterministico e dichiarato (nota nel catalogo).
_AUTO_DATE_RE = re.compile(r"^(DateTableTemplate_|LocalDateTable_)")

# Hook di log: connectors.py lo instrada nel log unico dei connettori
# (leggibile dall'admin). Default: stderr, mai silenzio.
_dbg_hook = None


def _dbg(msg: str) -> None:
    try:
        if _dbg_hook:
            _dbg_hook(str(msg))
        else:
            import sys
            print(f"[powerbi] {msg}", file=sys.stderr)
    except Exception:
        pass


def _now_iso() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ════════════════════════════════════════════════════════════════════════
#  Token OAuth delegato (device code avviato da connectors.py; qui refresh)
# ════════════════════════════════════════════════════════════════════════
class PowerBITokenManager:
    """Access/refresh token Microsoft per la risorsa Power BI, su file
    PER-UTENTE (percorso passato dal chiamante, nessuna costante globale)."""

    def __init__(self, client_id: str, tenant_id: str, token_file: Path):
        self.client_id = client_id
        self.tenant_id = tenant_id or "common"
        self.token_file = Path(token_file)
        self._token_data = None
        self._load()

    def _load(self):
        try:
            if self.token_file.exists():
                self._token_data = json.loads(self.token_file.read_text(encoding="utf-8"))
        except Exception as e:
            _dbg(f"token: file illeggibile ({e})")
            self._token_data = None

    def _save(self, data: dict):
        try:
            self._token_data = data
            self.token_file.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except Exception as e:
            _dbg(f"token: salvataggio fallito ({e})")

    def is_authenticated(self) -> bool:
        return bool(self._token_data and self._token_data.get("access_token"))

    def get_access_token(self) -> str:
        if not self._token_data:
            return ""
        expires_at = self._token_data.get("expires_at", 0)
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
            url = f"https://login.microsoftonline.com/{self.tenant_id}/oauth2/v2.0/token"
            r = requests.post(url, data={
                "client_id":     self.client_id,
                "grant_type":    "refresh_token",
                "refresh_token": refresh_token,
                "scope":         PBI_SCOPE,
            }, timeout=15)
            resp = r.json()
            if "access_token" in resp:
                resp["expires_at"] = time.time() + resp.get("expires_in", 3600)
                self._save(resp)
                return resp["access_token"]
            _dbg(f"token: refresh negato ({resp.get('error','?')}: "
                 f"{str(resp.get('error_description',''))[:120]})")
        except Exception as e:
            _dbg(f"token: refresh fallito ({e})")
        return ""


# ════════════════════════════════════════════════════════════════════════
#  Utilità di parsing
# ════════════════════════════════════════════════════════════════════════
def _clean_key(k: str) -> str:
    """'Sales[Amount]' -> 'Amount' · '[Totale]' -> 'Totale' (per la resa
    all'utente; verso l'AI le chiavi restano qualificate, non ambigue)."""
    k = str(k)
    m = re.match(r"^(?:[^\[\]]*)\[(.+)\]$", k)
    return m.group(1) if m else k


def _pick(row: dict, needle: str) -> str:
    """Estrae dal record il valore la cui CHIAVE contiene `needle`
    (case-insensitive, ignorando parentesi quadre): le colonne di
    COLUMNSTATISTICS/INFO arrivano come '[Table Name]', '[Column Name]'…"""
    needle = needle.lower().replace(" ", "")
    for k, v in row.items():
        kk = _clean_key(k).lower().replace(" ", "").replace("_", "")
        if needle in kk:
            return "" if v is None else str(v)
    return ""


def _norm_terms(text: str) -> list[str]:
    """Termini di ricerca normalizzati (minuscole, senza accenti, len>2)."""
    t = unicodedata.normalize("NFKD", (text or "").lower())
    t = "".join(c for c in t if not unicodedata.combining(c))
    return [w for w in re.findall(r"[a-z0-9]+", t) if len(w) > 2]


# ════════════════════════════════════════════════════════════════════════
#  Ricerca Power BI
# ════════════════════════════════════════════════════════════════════════
class PowerBISearch:
    HARD_LIMIT = 50      # tetto righe mostrate (non negoziabile dall'AI)
    MAX_STEP = 6         # passi massimi del loop agentico
    SAMPLE_ROWS = 5      # righe di campione restituite all'AI per osservazione
    SCHEMA_BUDGET = 4000  # caratteri di schema iniettati nel planner (anti-bloat)
    CANDIDATES = 4       # dataset candidati proposti al planner

    def __init__(self, cfg: dict):
        self.cfg = cfg or {}
        self.token_file = Path(self.cfg.get("pbi_token_file", ""))
        self.catalog_file = Path(self.cfg.get("pbi_catalog_file", ""))
        self.tm = PowerBITokenManager(
            self.cfg.get("pbi_client_id", ""),
            self.cfg.get("pbi_tenant_id", "common"),
            self.token_file,
        )
        self.last_links: list[tuple[str, str]] = []
        self._ai_overloaded = False

    # ── HTTP con gestione 429 (rate limit: 120 richieste/min/utente) ────
    def _req(self, method: str, url: str, token: str, payload: dict | None = None,
             timeout: int = 60):
        headers = {"Authorization": "Bearer " + token,
                   "Content-Type": "application/json"}
        for attempt in (0, 1):
            if method == "GET":
                r = requests.get(url, headers=headers, timeout=timeout)
            else:
                r = requests.post(url, headers=headers, json=payload, timeout=timeout)
            if r.status_code == 429 and attempt == 0:
                wait = min(int(r.headers.get("Retry-After", "10") or "10"), 30)
                _dbg(f"HTTP 429 (rate limit Power BI), attendo {wait}s e ritento")
                time.sleep(wait)
                continue
            return r
        return r

    @staticmethod
    def _err_body(r) -> str:
        try:
            j = r.json()
            e = j.get("error", {})
            code = e.get("code", "")
            msg = e.get("message", "") or json.dumps(e)[:200]
            return f"{code}: {msg}"[:300]
        except Exception:
            return (r.text or "")[:300]

    def _friendly_http_error(self, status: int, body: str) -> str:
        """Traduce gli errori HTTP di executeQueries in indicazioni operative
        esplicite (mai 'nessun risultato' quando il problema è di accesso)."""
        b = (body or "").lower()
        if status == 401:
            return ("accesso negato (HTTP 401): token scaduto o permessi "
                    "insufficienti — per interrogare un dataset servono i permessi "
                    "di Lettura e BUILD sulla tua utenza. Riconnetti Power BI dalla "
                    "pagina Connessioni; se persiste, chiedi il permesso Build sul dataset.")
        if status == 403:
            if "executequeries" in b or "feature" in b or "disabled" in b:
                return ("funzione disabilitata (HTTP 403): il tenant setting "
                        "'Dataset Execute Queries REST API' (Integration settings) "
                        "non è abilitato nel portale amministrazione Power BI. "
                        "Segnalalo all'amministratore Power BI.")
            return f"accesso vietato (HTTP 403): {body}"
        if status == 404:
            return ("dataset non trovato (HTTP 404): rimosso, spostato o non più "
                    "visibile alla tua utenza. Rigenera il catalogo Power BI dalla "
                    "pagina Connessioni.")
        if status == 429:
            return "limite di richieste Power BI raggiunto (HTTP 429): riprova tra un minuto."
        return f"HTTP {status}: {body}"

    # ── executeQueries (canale primario, JSON) ──────────────────────────
    def _dataset_url(self, group_id: str, dataset_id: str, action: str) -> str:
        if group_id:
            return f"{PBI_API}/groups/{group_id}/datasets/{dataset_id}/{action}"
        return f"{PBI_API}/datasets/{dataset_id}/{action}"

    def _execute_dax(self, group_id: str, dataset_id: str, dax: str,
                     token: str, timeout: int = 60) -> dict:
        """Esegue UNA query DAX via executeQueries. Ritorna
        {ok, rows, status, errore}. Unico punto che parla con l'endpoint dati:
        qui vivono tutte le salvaguardie e la mappatura degli errori."""
        dax = (dax or "").strip()
        if not dax:
            return {"ok": False, "rows": [], "status": 0, "errore": "query DAX vuota"}
        if not re.match(r"(?is)^\s*(DEFINE\b|EVALUATE\b)", dax):
            # EVALUATE è per costruzione sola lettura; tutto il resto non passa.
            return {"ok": False, "rows": [], "status": 0,
                    "errore": "query rifiutata: sono ammesse solo query DAX EVALUATE (sola lettura)"}
        url = self._dataset_url(group_id, dataset_id, "executeQueries")
        payload = {"queries": [{"query": dax}],
                   "serializerSettings": {"includeNulls": True}}
        try:
            r = self._req("POST", url, token, payload, timeout=timeout)
        except Exception as e:
            return {"ok": False, "rows": [], "status": 0, "errore": f"rete non raggiungibile: {e}"}
        if r.status_code != 200:
            body = self._err_body(r)
            _dbg(f"executeQueries HTTP {r.status_code} su {dataset_id}: {body}")
            return {"ok": False, "rows": [], "status": r.status_code,
                    "errore": self._friendly_http_error(r.status_code, body)}
        try:
            results = r.json().get("results", [])
            tables = (results[0] or {}).get("tables", []) if results else []
            rows = (tables[0] or {}).get("rows", []) if tables else []
        except Exception as e:
            return {"ok": False, "rows": [], "status": 200,
                    "errore": f"risposta non interpretabile: {e}"}
        return {"ok": True, "rows": rows, "status": 200, "errore": ""}

    # ── Catalogo per-utente ──────────────────────────────────────────────
    def load_catalog(self) -> dict:
        try:
            if self.catalog_file.exists():
                return json.loads(self.catalog_file.read_text(encoding="utf-8"))
        except Exception as e:
            _dbg(f"catalogo: file illeggibile ({e})")
        return {}

    def catalog_status(self) -> dict:
        cat = self.load_catalog()
        if not cat:
            return {"present": False}
        items = cat.get("items", [])
        return {
            "present": True,
            "versione": cat.get("versione", ""),
            "generato": cat.get("generato", ""),
            "workspaces": cat.get("workspaces", 0),
            "datasets": len(items),
            "interrogabili": sum(1 for i in items if i.get("schema_ok")),
            "misure_rilevate": sum(1 for i in items if i.get("misure")),
        }

    def _harvest_schema(self, group_id: str, dataset_id: str, token: str) -> dict:
        """Tabelle+colonne del modello via EVALUATE COLUMNSTATISTICS():
        pura DAX, funziona sull'endpoint JSON anche su capacità condivisa.
        Le tabelle-data automatiche vengono filtrate (rumore, non dati)."""
        res = self._execute_dax(group_id, dataset_id, "EVALUATE COLUMNSTATISTICS()",
                                token, timeout=90)
        if not res["ok"]:
            return {"ok": False, "note": res["errore"], "tabelle": {}}
        tabelle: dict[str, dict] = {}
        for row in res["rows"]:
            tn = _pick(row, "tablename")
            cn = _pick(row, "columnname")
            if not tn or not cn or _AUTO_DATE_RE.match(tn):
                continue
            card = _pick(row, "cardinality")
            ent = tabelle.setdefault(tn, {"colonne": [], "cardinalita": {}})
            if cn not in ent["colonne"]:
                ent["colonne"].append(cn)
                if card:
                    ent["cardinalita"][cn] = card
        if not tabelle:
            return {"ok": False, "note": "COLUMNSTATISTICS non ha restituito colonne",
                    "tabelle": {}}
        return {"ok": True, "note": "", "tabelle": tabelle}

    def _harvest_measures(self, group_id: str, dataset_id: str, token: str) -> tuple[list, str]:
        """MISURE via executeDaxQueries + INFO.VIEW.MEASURES() (risposta Apache
        Arrow). Arricchimento OPZIONALE: richiede pyarrow e dataset su capacità
        dedicata. In caso contrario si ferma con motivo esplicito nel catalogo."""
        try:
            import io
            import pyarrow as pa  # noqa: F401
        except Exception:
            return [], "pyarrow non installato: elenco misure non rilevato"
        url = self._dataset_url(group_id, dataset_id, "executeDaxQueries")
        payload = {"query": "EVALUATE INFO.VIEW.MEASURES()", "queryTimeout": 60}
        try:
            r = self._req("POST", url, token, payload, timeout=75)
        except Exception as e:
            return [], f"endpoint DAX/Arrow non raggiungibile: {e}"
        if r.status_code != 200:
            return [], ("endpoint DAX/Arrow non disponibile "
                        f"(HTTP {r.status_code}: {self._err_body(r)[:120]}) — "
                        "richiede dataset su capacità dedicata")
        try:
            import pyarrow as pa
            stream = io.BytesIO(r.content)
            misure: list[str] = []
            while stream.tell() < len(r.content):
                try:
                    reader = pa.ipc.open_stream(stream)
                    table = reader.read_all()
                except pa.ArrowInvalid:
                    break
                meta = {k.decode(): v.decode()
                        for k, v in (reader.schema.metadata or {}).items()}
                if meta.get("IsError") == "true":
                    return [], f"errore modello: {meta.get('FaultString','')[:150]}"
                cols = {c.lower(): c for c in table.column_names}
                name_col = next((cols[c] for c in cols
                                 if "name" in c and "table" not in c), None)
                if name_col:
                    for v in table.column(name_col).to_pylist():
                        if v and str(v) not in misure:
                            misure.append(str(v))
            return misure, ""
        except Exception as e:
            return [], f"parsing Arrow fallito: {e}"

    def build_catalog(self, progress_cb=None) -> dict:
        """Genera (o rigenera) il catalogo PER-UTENTE: workspace visibili,
        dataset, tabelle/colonne e — dove possibile — misure. Ogni dataset
        senza schema resta elencato con il MOTIVO: niente omissioni mute."""
        def _prog(msg: str):
            _dbg("catalogo: " + msg)
            if progress_cb:
                try:
                    progress_cb(msg)
                except Exception:
                    pass

        token = self.tm.get_access_token()
        if not token:
            return {"errore": "Account Power BI non connesso o token scaduto: "
                              "collega il tuo account dalla pagina Connessioni."}

        # 1) workspace visibili all'utente + area personale
        _prog("elenco workspace…")
        r = self._req("GET", f"{PBI_API}/groups?$top=5000", token, timeout=30)
        if r.status_code != 200:
            return {"errore": "Elenco workspace non riuscito: "
                              + self._friendly_http_error(r.status_code, self._err_body(r))}
        groups = r.json().get("value", [])
        scopes = [{"gid": "", "nome": "Area personale"}] + [
            {"gid": g.get("id", ""), "nome": g.get("name", "(senza nome)")}
            for g in groups if g.get("id")
        ]

        items: list[dict] = []
        for sc in scopes:
            gid, wname = sc["gid"], sc["nome"]
            url = (f"{PBI_API}/groups/{gid}/datasets" if gid else f"{PBI_API}/datasets")
            r = self._req("GET", url, token, timeout=30)
            if r.status_code != 200:
                _prog(f"workspace '{wname}': elenco dataset fallito "
                      f"(HTTP {r.status_code}) — saltato")
                continue
            for ds in r.json().get("value", []):
                did, dname = ds.get("id", ""), ds.get("name", "(senza nome)")
                if not did:
                    continue
                _prog(f"schema di '{wname} / {dname}'…")
                schema = self._harvest_schema(gid, did, token)
                misure, mis_note = ([], "")
                if schema["ok"]:
                    misure, mis_note = self._harvest_measures(gid, did, token)
                web_url = ds.get("webUrl") or (
                    f"https://app.powerbi.com/groups/{gid or 'me'}/datasets/{did}/details")
                items.append({
                    "workspace": wname, "group_id": gid,
                    "dataset": dname, "dataset_id": did,
                    "web_url": web_url,
                    "schema_ok": schema["ok"],
                    "schema_note": schema["note"],
                    "tabelle": schema["tabelle"],
                    "misure": misure,
                    "misure_note": mis_note,
                })

        catalog = {
            "versione": CATALOG_VERSION,
            "generato": _now_iso(),
            "workspaces": len(scopes),
            "items": items,
            "nota": ("Catalogo per-utente: riflette i permessi Power BI della tua utenza. "
                     "Tabelle-data automatiche (DateTableTemplate/LocalDateTable) escluse."),
        }
        try:
            self.catalog_file.parent.mkdir(parents=True, exist_ok=True)
            self.catalog_file.write_text(json.dumps(catalog, ensure_ascii=False, indent=1),
                                         encoding="utf-8")
        except Exception as e:
            return {"errore": f"Catalogo non salvabile su disco: {e}"}
        ok_n = sum(1 for i in items if i["schema_ok"])
        _prog(f"completato: {len(items)} dataset, {ok_n} interrogabili")
        if not items:
            return {"ok": True, "workspaces": len(scopes), "datasets": 0, "interrogabili": 0,
                    "avviso": "La tua utenza non vede alcun dataset Power BI."}
        return {"ok": True, "workspaces": len(scopes), "datasets": len(items),
                "interrogabili": ok_n, "generato": catalog["generato"]}

    # ── Selezione candidati (prefiltro deterministico) ──────────────────
    def rank_datasets(self, catalog: dict, query: str) -> list[dict]:
        terms = _norm_terms(query)
        scored = []
        for it in catalog.get("items", []):
            hay_ds = _norm_terms(it.get("dataset", ""))
            hay_ws = _norm_terms(it.get("workspace", ""))
            hay_tab = _norm_terms(" ".join(it.get("tabelle", {}).keys()))
            hay_col = _norm_terms(" ".join(
                c for t in it.get("tabelle", {}).values() for c in t.get("colonne", [])))
            hay_mis = _norm_terms(" ".join(it.get("misure", [])))
            score = 0
            for w in terms:
                if w in hay_ds:
                    score += 3
                if w in hay_ws:
                    score += 2
                if w in hay_tab:
                    score += 2
                if w in hay_col or w in hay_mis:
                    score += 1
            scored.append((score, it))
        scored.sort(key=lambda x: (-x[0], x[1].get("dataset", "")))
        top = [it for s, it in scored if s > 0][: self.CANDIDATES]
        if not top:  # nessun match lessicale: proponi comunque i primi
            top = [it for _s, it in scored][: self.CANDIDATES]
        return top

    def _find_item(self, catalog: dict, name: str) -> dict | None:
        name = (name or "").strip().lower()
        for it in catalog.get("items", []):
            if it.get("dataset", "").strip().lower() == name:
                return it
        # tollera "workspace / dataset"
        for it in catalog.get("items", []):
            full = f"{it.get('workspace','')} / {it.get('dataset','')}".strip().lower()
            if full == name:
                return it
        return None

    def _schema_text(self, item: dict, budget: int) -> str:
        lines = [f"Dataset: {item.get('dataset')}  (workspace: {item.get('workspace')})"]
        if not item.get("schema_ok"):
            lines.append(f"  ⚠️ schema non rilevato: {item.get('schema_note','')}")
            return "\n".join(lines)
        used = 0
        for tn, t in item.get("tabelle", {}).items():
            cols = ", ".join(t.get("colonne", [])[:40])
            row = f"  Tabella '{tn}': {cols}"
            if used + len(row) > budget:
                lines.append("  … (schema troncato per budget)")
                break
            lines.append(row)
            used += len(row)
        mis = item.get("misure") or []
        if mis:
            lines.append("  Misure: " + ", ".join(f"[{m}]" for m in mis[:40]))
        elif item.get("misure_note"):
            lines.append("  Misure: non rilevate (" + item["misure_note"][:120] + ")")
        return "\n".join(lines)

    # ── Motore AI del planner (stessa disciplina di dynamics_search) ────
    def _ask_ai(self, system_prompt: str, user_prompt: str, max_tokens: int = 700) -> str:
        engine = self.cfg.get("ai_engine", "claude")
        RETRY_STATUS = {429, 529}
        MAX_RETRY = 3
        try:
            if engine == "claude":
                key = self.cfg.get("claude_api_key", "")
                model = self.cfg.get("claude_model", "claude-sonnet-4-6")
                fallback = (self.cfg.get("claude_model_fallback") or "").strip()
                if not key:
                    _dbg("planner: chiave Claude ASSENTE nel cfg")
                    return ""

                def _txt(data: dict) -> str:
                    return "".join(b.get("text", "") for b in (data.get("content") or [])
                                   if isinstance(b, dict) and b.get("type") == "text").strip()

                def _call(model_name: str) -> str | None:
                    for tentativo in range(MAX_RETRY):
                        r = requests.post("https://api.anthropic.com/v1/messages",
                            headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                                     "Content-Type": "application/json"},
                            json={"model": model_name, "max_tokens": max_tokens,
                                  "system": system_prompt,
                                  "messages": [{"role": "user", "content": user_prompt}]},
                            timeout=45)
                        if r.status_code in RETRY_STATUS and tentativo < MAX_RETRY - 1:
                            attesa = 3 * (tentativo + 1)
                            _dbg(f"planner: Claude HTTP {r.status_code}, ritento tra {attesa}s")
                            time.sleep(attesa)
                            continue
                        if r.status_code != 200:
                            _dbg(f"planner: Claude HTTP {r.status_code} [{model_name}]: "
                                 f"{r.text[:200]}")
                            if r.status_code in RETRY_STATUS:
                                self._ai_overloaded = True
                            return None
                        return _txt(r.json())
                    return None

                out = _call(model)
                if not out and fallback and fallback != model:
                    _dbg(f"planner: modello '{model}' senza risposta, ripiego su '{fallback}'")
                    out = _call(fallback)
                return out or ""
            elif engine == "openai":
                key = self.cfg.get("openai_api_key", "")
                model = self.cfg.get("openai_model", "gpt-4o-mini")
                if not key:
                    return ""
                for tentativo in range(MAX_RETRY):
                    r = requests.post("https://api.openai.com/v1/chat/completions",
                        headers={"Authorization": f"Bearer {key}",
                                 "Content-Type": "application/json"},
                        json={"model": model, "max_tokens": max_tokens,
                              "messages": [{"role": "system", "content": system_prompt},
                                           {"role": "user", "content": user_prompt}]},
                        timeout=45)
                    if r.status_code in RETRY_STATUS and tentativo < MAX_RETRY - 1:
                        time.sleep(3 * (tentativo + 1))
                        continue
                    if r.status_code != 200:
                        _dbg(f"planner: OpenAI HTTP {r.status_code}")
                        if r.status_code in RETRY_STATUS:
                            self._ai_overloaded = True
                        return ""
                    return (r.json().get("choices", [{}])[0]
                            .get("message", {}).get("content", "") or "").strip()
                return ""
            else:  # lmstudio
                url = self.cfg.get("lm_url", "")
                if not url:
                    return ""
                r = requests.post(url, headers={"Content-Type": "application/json"},
                    json={"model": self.cfg.get("lm_model", "local-model"),
                          "max_tokens": max_tokens,
                          "messages": [{"role": "system", "content": system_prompt},
                                       {"role": "user", "content": user_prompt}]},
                    timeout=120)
                if r.status_code != 200:
                    _dbg(f"planner: LM Studio HTTP {r.status_code}")
                    return ""
                return (r.json().get("choices", [{}])[0]
                        .get("message", {}).get("content", "") or "").strip()
        except Exception as e:
            _dbg(f"planner: eccezione motore AI: {e}")
            return ""

    def _msg_overload(self) -> str:
        return ("[Power BI] Il motore AI del planner è momentaneamente sovraccarico "
                "(HTTP 429/529): riprova tra qualche istante.")

    # ── Formattazione risultati ──────────────────────────────────────────
    def _compact_obs(self, rows: list, max_rows: int | None = None) -> list:
        max_rows = max_rows or self.SAMPLE_ROWS
        out = []
        for rec in rows[:max_rows]:
            out.append({k: (str(v)[:120] if v is not None else None)
                        for k, v in rec.items()})
        return out

    def _format_rows(self, rows: list) -> str:
        shown = rows[: self.HARD_LIMIT]
        lines = []
        for rec in shown:
            parts = []
            for k, v in rec.items():
                if v is None:
                    continue
                parts.append(f"{_clean_key(k)}: {str(v)[:200]}")
            lines.append("• " + " · ".join(parts))
        if len(rows) > self.HARD_LIMIT:
            lines.append(f"… ({len(rows) - self.HARD_LIMIT} righe ulteriori non mostrate: "
                         f"tetto {self.HARD_LIMIT} righe)")
        return "\n".join(lines)

    # ── Loop agentico bounded ────────────────────────────────────────────
    def _agentic(self, query: str, catalog: dict, candidates: list[dict],
                 token: str) -> str:
        cand_by_name = {c["dataset"].strip().lower(): c for c in candidates}

        def _desc(items: list[dict]) -> str:
            out, used = [], 0
            per = max(800, self.SCHEMA_BUDGET // max(1, len(items)))
            for it in items:
                t = self._schema_text(it, per)
                out.append(t)
                used += len(t)
                if used > self.SCHEMA_BUDGET:
                    break
            return "\n".join(out)

        system = (
            "Sei un motore di interrogazione per modelli semantici Power BI che lavora A PASSI. "
            "A ogni passo proponi UNA sola azione in JSON; riceverai un'osservazione "
            "(conteggio righe + campione) e proporrai il passo successivo, fino a concludere. "
            "USA ESCLUSIVAMENTE i dataset, le tabelle, le colonne e le misure elencati: non "
            "inventare nomi. Regole DAX: solo query di lettura che iniziano con EVALUATE "
            "(eventualmente precedute da DEFINE MEASURE); tabelle tra apici singoli "
            "('Tabella'), colonne come 'Tabella'[Colonna], misure come [Misura]; UNA query "
            "per passo; AGGREGA sempre (SUMMARIZECOLUMNS, TOPN, ROW, CALCULATE): mai scaricare "
            "tabelle intere. Se l'elenco misure non è disponibile, aggrega le colonne "
            "numeriche con SUM/COUNT/AVERAGE. Le azioni possibili sono:\n"
            '1) {"azione":"dax","dataset":"<nome dataset>","dax":"EVALUATE ...","motivo":"..."}\n'
            "   Esegue una query esplorativa e restituisce conteggio + campione.\n"
            '2) {"azione":"schema","dataset":["D1","D2"],"motivo":"..."}\n'
            "   Chiede lo schema completo (tabelle, colonne, misure) di uno o più dataset.\n"
            '3) {"azione":"cerca_dataset","parola":"<termine>","motivo":"..."}\n'
            "   Cerca altri dataset nel catalogo se i candidati non bastano.\n"
            '4) {"azione":"concludi","dataset":"<nome>","dax":"EVALUATE ...","spiegazione":"<sintesi per l\'utente>"}\n'
            "   Quando hai la query che risponde: il codice la esegue e mostra i dati.\n"
            "Concludi appena possibile. Sii essenziale."
        )

        history = [f"DOMANDA UTENTE:\n{query}",
                   f"\nDATASET CANDIDATI (permessi della tua utenza):\n{_desc(candidates)}"]

        for step in range(self.MAX_STEP):
            user = "\n".join(history) + (
                f"\n\nPasso {step+1}/{self.MAX_STEP}. Proponi la prossima azione in JSON.")
            raw = self._ask_ai(system, user, max_tokens=700)
            if not raw:
                if self._ai_overloaded:
                    return self._msg_overload()
                _dbg(f"agentic: nessuna risposta AI al passo {step+1}")
                return ""
            m = re.search(r"\{.*\}", raw, re.S)
            if not m:
                _dbg(f"agentic: nessun JSON al passo {step+1}: {raw[:100]}")
                return ""
            try:
                act = json.loads(m.group(0))
            except Exception as e:
                _dbg(f"agentic: JSON non valido al passo {step+1}: {e}")
                history.append("\n[OSSERVAZIONE] Il JSON proposto non era valido: riformula.")
                continue
            azione = (act.get("azione") or "").strip()
            _dbg(f"agentic passo {step+1}: azione={azione} "
                 f"motivo={str(act.get('motivo',''))[:80]}")

            if azione in ("dax", "concludi"):
                nome = (act.get("dataset") or "").strip()
                it = cand_by_name.get(nome.lower()) or self._find_item(catalog, nome)
                if not it:
                    history.append(f"\n[OSSERVAZIONE] Dataset '{nome}' non nel catalogo: "
                                   "scegline uno tra quelli elencati.")
                    continue
                res = self._execute_dax(it["group_id"], it["dataset_id"],
                                        act.get("dax", ""), token)
                if not res["ok"]:
                    # Errore di ACCESSO/CONFIGURAZIONE: inutile insistere, l'utente
                    # deve saperlo subito. Errore di QUERY: il planner può correggersi.
                    if res["status"] in (401, 403, 404, 429):
                        return f"[Power BI — {it['workspace']} / {it['dataset']}] {res['errore']}"
                    history.append(f"\n[OSSERVAZIONE] Query fallita: {res['errore'][:300]}. "
                                   "Correggi la DAX (nomi esatti, apici, EVALUATE).")
                    continue
                if azione == "concludi":
                    self.last_links = [(f"{it['dataset']} · Power BI", it.get("web_url", ""))]
                    spieg = act.get("spiegazione", "")
                    if not res["rows"]:
                        return (f"[Power BI — {it['workspace']} / {it['dataset']}] "
                                f"La query non ha restituito righe. {spieg}")
                    header = (f"[Power BI — {it['workspace']} / {it['dataset']}] "
                              f"{len(res['rows'])} righe. {spieg}\n"
                              f"⚠️ Dati prodotti da una query DAX dinamica con i permessi "
                              f"della tua utenza; verifica prima di usarli per decisioni operative.")
                    return header + "\n" + self._format_rows(res["rows"])
                obs = self._compact_obs(res["rows"])
                history.append(f"\n[OSSERVAZIONE] {len(res['rows'])} righe. Campione: "
                               + json.dumps(obs, ensure_ascii=False)[:1500])
                continue

            if azione == "schema":
                nomi = act.get("dataset") or []
                if isinstance(nomi, str):
                    nomi = [nomi]
                blocchi = []
                for n in nomi[:3]:
                    it = self._find_item(catalog, n)
                    blocchi.append(self._schema_text(it, 2500) if it
                                   else f"Dataset '{n}' non nel catalogo.")
                history.append("\n[SCHEMA]\n" + "\n".join(blocchi))
                continue

            if azione == "cerca_dataset":
                parola = (act.get("parola") or "").strip()
                found = self.rank_datasets(catalog, parola)
                for f in found:
                    cand_by_name.setdefault(f["dataset"].strip().lower(), f)
                nomi = ", ".join(f"{f['workspace']} / {f['dataset']}" for f in found) or "(nessuno)"
                history.append(f"\n[OSSERVAZIONE] Dataset trovati per '{parola}': {nomi}")
                continue

            history.append(f"\n[OSSERVAZIONE] Azione '{azione}' sconosciuta: usa una delle 4 previste.")
        _dbg("agentic: passi esauriti senza conclusione")
        return ""

    # ── Ingresso principale ──────────────────────────────────────────────
    def search(self, query: str, max_results: int = 5,
               current_user_name: str = "") -> str:
        self.last_links = []
        self._ai_overloaded = False
        if not self.tm.is_authenticated():
            return ("[Power BI] Account non connesso: collega il tuo account "
                    "dalla pagina Connessioni.")
        catalog = self.load_catalog()
        if not catalog or not catalog.get("items"):
            return ("[Power BI] Catalogo non ancora generato per la tua utenza: "
                    "vai in Connessioni → Power BI → «Genera catalogo», poi riprova. "
                    "Il catalogo elenca i workspace e i dataset che la TUA utenza può interrogare.")
        token = self.tm.get_access_token()
        if not token:
            return ("[Power BI] Token scaduto e refresh non riuscito: riconnetti "
                    "il tuo account dalla pagina Connessioni.")
        candidates = self.rank_datasets(catalog, query)
        if not candidates:
            return ("[Power BI] La tua utenza non vede alcun dataset Power BI "
                    "(catalogo vuoto).")
        _dbg(f"search: query={query[:80]!r} | candidati="
             + ", ".join(c["dataset"] for c in candidates))
        out = self._agentic(query, catalog, candidates, token)
        if out:
            return out
        if self._ai_overloaded:
            return self._msg_overload()
        return ("[Power BI] Il planner non è riuscito a costruire una query DAX "
                "valida per questa domanda entro i passi previsti. Riformula la "
                "domanda citando il dataset o le colonne (vedi il catalogo in Connessioni).")

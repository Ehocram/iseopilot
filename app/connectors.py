"""
connectors.py — Sign-in per-utente OneDrive / Dynamics 365 (Device Code Flow).

Differenze rispetto al desktop:
  * Il desktop ha UN token globale (TOKEN_FILE nel modulo) e fa polling
    BLOCCANTE. Sul web il token deve essere ISOLATO PER UTENTE e il polling
    NON può bloccare una richiesta HTTP per minuti.
  * Qui il device flow è in due endpoint:
      - start()      -> una POST /devicecode, ritorna user_code + URL;
      - poll_once()  -> UNA POST /token (nessun loop). Il browser ripete via JS.
  * Il token finisce in store.user_token_path(user, connettore): un file per
    identità sotto il volume dati. Nessuna condivisione di token fra utenti.

La RICERCA riusa i moduli desktop (OneDriveSearch / DynamicsSearch), che però
leggono il token dalla costante di modulo TOKEN_FILE: la si reindirizza al file
dell'utente sotto un lock, senza modificare i moduli.

Identificatori app/tenant: NON sono segreti (device code, nessun client secret).
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import requests

from . import store
from .engines import onedrive_search, dynamics_search

# Catalogo entità e schema .md Dynamics: reindirizzati sotto il volume dati
# (di default il modulo li metterebbe nella home dell'utente). Il catalogo è
# CONDIVISO fra utenti (è lo schema dell'istanza F&O, uguale per tutti).
_DATA_DIR = Path(os.environ.get("APP_DATA_DIR", "/data"))
DYN_DIR = _DATA_DIR / "dynamics"
DYN_DIR.mkdir(parents=True, exist_ok=True)
dynamics_search.CATALOG_FILE = DYN_DIR / "catalog.json"
dynamics_search.SCHEMA_DIR = DYN_DIR / "schema"
# Log diagnostico Dynamics in un percorso noto (nel volume dati), così è
# leggibile dall'admin senza entrare nel container.
DYN_LOG = DYN_DIR / "dyn_debug.log"
try:
    dynamics_search._DBG_LOG = DYN_LOG
except Exception:
    pass


def _dyn_log(msg: str):
    try:
        import datetime
        with open(DYN_LOG, "a", encoding="utf-8") as f:
            f.write(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [connectors] {msg}\n")
    except Exception:
        pass


def _od_log_bridge(msg: str) -> None:
    """Instrada il log diagnostico del modulo OneDrive (query Graph, esiti,
    eccezioni) nel log unico dei connettori, leggibile da Admin. Sostituisce il
    file separato del desktop, che era spento di default."""
    _dyn_log("[onedrive] " + str(msg))


onedrive_search._odlog = _od_log_bridge


def dyn_log_tail(n: int = 80) -> str:
    try:
        if not DYN_LOG.is_file():
            return "(nessun log Dynamics ancora generato)"
        lines = DYN_LOG.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-n:])
    except Exception as e:
        return f"(impossibile leggere il log: {e})"

# Registro dei report HTML generati da Dynamics: il modulo li scrive su disco
# e restituisce [REPORT_HTML: path]; qui li mappiamo a un token non indovinabile
# legato all'utente, così possiamo servirli in sicurezza (no path traversal,
# solo il proprietario). Effimeri: vivono finché il processo è attivo.
import re as _re
import uuid as _uuid
_REPORTS: dict[str, dict] = {}
_REPORTS_LOCK = threading.Lock()
_REPORT_RE = _re.compile(r"\[REPORT_HTML: (.+?)\]")


def register_report(user: str, path: str) -> str:
    token = _uuid.uuid4().hex
    with _REPORTS_LOCK:
        _REPORTS[token] = {"user": user, "path": path}
    return token


def report_path(user: str, token: str) -> str | None:
    with _REPORTS_LOCK:
        rec = _REPORTS.get(token)
    if not rec or rec["user"] != user:
        return None
    return rec["path"]


# Registro dei file generati (Word/Excel/PPT/PDF) per il download sicuro.
_DOWNLOADS: dict[str, dict] = {}


def register_download(user: str, path: str, filename: str) -> str:
    token = _uuid.uuid4().hex
    with _REPORTS_LOCK:
        _DOWNLOADS[token] = {"user": user, "path": path, "filename": filename}
    return token


def download_info(user: str, token: str) -> dict | None:
    with _REPORTS_LOCK:
        rec = _DOWNLOADS.get(token)
    if not rec or rec["user"] != user:
        return None
    return rec

# Valori predefiniti dei connettori Microsoft (app desktop ISEO).
DEF_OD_CLIENT_ID     = "c5a90f54-d599-4f71-a98f-0fa0781145c1"
DEF_OD_TENANT_ID     = "a97887fe-14ea-46bc-afa8-f7b85f2164ff"
DEF_DYN_CLIENT_ID    = "c5a90f54-d599-4f71-a98f-0fa0781145c1"
DEF_DYN_TENANT_ID    = "a97887fe-14ea-46bc-afa8-f7b85f2164ff"
DEF_DYN_RESOURCE_URL = "https://isd365-prod.operations.eu.dynamics.com"

CONNECTORS = ("onedrive", "dynamics")
_LOCK = threading.Lock()  # serializza l'uso della ricerca (TOKEN_FILE condiviso)


# ── Configurazione (da admin settings, con default ISEO) ────
def ms_cfg(conn: str) -> dict:
    if conn == "onedrive":
        return {
            "client_id": store.get_setting("od_client_id", DEF_OD_CLIENT_ID),
            "tenant_id": store.get_setting("od_tenant_id", DEF_OD_TENANT_ID),
            "resource_url": "",
            "scope": "Files.Read.All offline_access",
        }
    res = store.get_setting("dyn_resource_url", DEF_DYN_RESOURCE_URL).rstrip("/")
    return {
        "client_id": store.get_setting("dyn_client_id", DEF_DYN_CLIENT_ID),
        "tenant_id": store.get_setting("dyn_tenant_id", DEF_DYN_TENANT_ID),
        "resource_url": res,
        "scope": (f"{res}/.default offline_access" if res else "offline_access"),
    }


def is_configured(conn: str) -> bool:
    if conn not in CONNECTORS:
        return False
    c = ms_cfg(conn)
    if conn == "dynamics":
        return bool(c["client_id"] and c["resource_url"])
    return bool(c["client_id"])


# ── Token per-utente ────────────────────────────────────────
def _token_path(user: str, conn: str) -> Path:
    return store.user_token_path(user, conn)


def _load_token(user: str, conn: str) -> dict:
    p = _token_path(user, conn)
    try:
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _save_token(user: str, conn: str, data: dict) -> None:
    try:
        _token_path(user, conn).write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception:
        pass


def is_connected(user: str, conn: str) -> bool:
    return bool(_load_token(user, conn).get("access_token"))


def disconnect(user: str, conn: str) -> None:
    try:
        p = _token_path(user, conn)
        if p.exists():
            p.unlink()
    except Exception:
        pass
    store.set_user_setting(user, f"{conn}_devicecode", "")


# ── Device Code Flow (non bloccante) ────────────────────────
def _login_url(tenant: str, ep: str) -> str:
    return f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/{ep}"


def start(user: str, conn: str) -> dict:
    """Avvia il device flow: una POST /devicecode. Salva device_code per il poll."""
    if conn not in CONNECTORS:
        return {"ok": False, "error": "Connettore non valido."}
    if not is_configured(conn):
        return {"ok": False, "error": "Connettore non configurato dall'amministratore."}
    cfg = ms_cfg(conn)
    try:
        r = requests.post(_login_url(cfg["tenant_id"], "devicecode"),
                          data={"client_id": cfg["client_id"], "scope": cfg["scope"]},
                          timeout=15)
        resp = r.json()
    except Exception as e:
        return {"ok": False, "error": f"Rete non raggiungibile: {e}"}
    if "device_code" not in resp:
        return {"ok": False, "error": resp.get("error_description", "Avvio non riuscito.")}
    store.set_user_setting(user, f"{conn}_devicecode", resp["device_code"], secret=True)
    return {
        "ok": True,
        "user_code": resp.get("user_code", ""),
        "verification_uri": resp.get("verification_uri", "https://microsoft.com/devicelogin"),
        "message": resp.get("message", ""),
        "interval": resp.get("interval", 5),
    }


def poll_once(user: str, conn: str) -> dict:
    """UN solo tentativo di scambio token. Il browser ripete finché non risolve."""
    if conn not in CONNECTORS:
        return {"status": "error", "message": "Connettore non valido."}
    device_code = store.get_user_setting(user, f"{conn}_devicecode", "")
    if not device_code:
        return {"status": "error", "message": "Avvia prima la connessione."}
    cfg = ms_cfg(conn)
    try:
        r = requests.post(_login_url(cfg["tenant_id"], "token"), data={
            "client_id": cfg["client_id"],
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "device_code": device_code,
        }, timeout=12)
        resp = r.json()
    except Exception as e:
        return {"status": "error", "message": f"Rete non raggiungibile: {e}"}
    if "access_token" in resp:
        resp["expires_at"] = time.time() + resp.get("expires_in", 3600)
        _save_token(user, conn, resp)
        store.set_user_setting(user, f"{conn}_devicecode", "")
        return {"status": "connected"}
    err = resp.get("error", "")
    if err == "authorization_pending":
        return {"status": "pending"}
    if err == "slow_down":
        return {"status": "pending", "slow_down": True}
    if err in ("expired_token", "authorization_declined", "access_denied"):
        store.set_user_setting(user, f"{conn}_devicecode", "")
        return {"status": "error", "message": resp.get("error_description", err)}
    return {"status": "error", "message": resp.get("error_description", err or "Errore.")}


# ── Ricerca (riusa i moduli desktop col token per-utente) ───
def search(user: str, conn: str, query: str, max_results: int = 3,
           current_user_name: str = "", ai_settings: dict | None = None) -> str:
    """Cerca con l'identità dell'utente. Stringa vuota se non connesso o errore.
    Reindirizza il TOKEN_FILE del modulo al file dell'utente, sotto lock."""
    if conn not in CONNECTORS or not is_connected(user, conn):
        return ""
    cfg = ms_cfg(conn)
    user_token = _token_path(user, conn)
    with _LOCK:
        try:
            if conn == "onedrive":
                prev = onedrive_search.TOKEN_FILE
                onedrive_search.TOKEN_FILE = user_token
                try:
                    od = onedrive_search.OneDriveSearch({
                        "od_client_id": cfg["client_id"],
                        "od_tenant_id": cfg["tenant_id"],
                    })
                    return od.search(query, max_results=max_results) or ""
                finally:
                    onedrive_search.TOKEN_FILE = prev
            else:
                prev = dynamics_search.TOKEN_FILE
                dynamics_search.TOKEN_FILE = user_token
                try:
                    dyn = dynamics_search.DynamicsSearch(_dyn_full_cfg(cfg, ai_settings))
                    return dyn.search(query, max_results=max_results,
                                      current_user_name=current_user_name) or ""
                finally:
                    dynamics_search.TOKEN_FILE = prev
        except Exception:
            return ""


def _dyn_full_cfg(cfg: dict, ai_settings: dict | None) -> dict:
    """Costruisce il cfg COMPLETO per DynamicsSearch, come fa l'app desktop.
    Il modulo non usa solo i parametri di connessione: per il planner agentico
    e per tradurre la domanda in parole chiave chiama il motore AI, quindi gli
    servono ai_engine + chiave/modello. Senza questi, il planner fallisce in
    silenzio e la ricerca torna vuota."""
    ai = ai_settings or {}
    return {
        # connessione
        "dyn_client_id": cfg["client_id"],
        "dyn_tenant_id": cfg["tenant_id"],
        "dyn_resource_url": cfg["resource_url"],
        "dyn_schema_dir": str(dynamics_search.SCHEMA_DIR),
        # motore AI per il planner (replica il cfg desktop)
        "ai_engine": ai.get("ai_engine", "claude"),
        "claude_api_key": ai.get("claude_api_key", ""),
        "claude_model": (ai.get("claude_model_dynamics") or "").strip() or ai.get("claude_model", "claude-opus-4-8"),
        "openai_api_key": ai.get("openai_api_key", ""),
        "openai_model": ai.get("openai_model", ""),
        "lm_url": ai.get("lm_url", ""),
        "lm_model": ai.get("lm_model", ""),
        "reply_lang": ai.get("reply_lang", "Italiano"),
        # comportamento ricerca
        "dyn_agentic": ai.get("dyn_agentic", True),
    }


def search_with_links(user: str, conn: str, query: str, max_results: int = 3,
                      current_user_name: str = "",
                      ai_settings: dict | None = None) -> tuple[str, list[dict]]:
    """Come search(), ma ritorna anche i link strutturati delle fonti
    (nome, url) — replica i last_links del desktop. Per Dynamics i link
    non sono applicabili e la lista è vuota."""
    if conn not in CONNECTORS or not is_connected(user, conn):
        return "", []
    cfg = ms_cfg(conn)
    user_token = _token_path(user, conn)
    with _LOCK:
        try:
            if conn == "onedrive":
                prev = onedrive_search.TOKEN_FILE
                onedrive_search.TOKEN_FILE = user_token
                _dyn_log(f"[onedrive] search avviata | query={query[:100]!r} | "
                         f"token_file_esiste={user_token.is_file()}")
                try:
                    od = onedrive_search.OneDriveSearch({
                        "od_client_id": cfg["client_id"],
                        "od_tenant_id": cfg["tenant_id"],
                    })
                    text = od.search(query, max_results=max_results) or ""
                    links = [{"name": n, "url": u}
                             for (n, u) in getattr(od, "last_links", []) if u]
                    _dyn_log(f"[onedrive] search conclusa | caratteri={len(text)} | link={len(links)}")
                    return text, links
                finally:
                    onedrive_search.TOKEN_FILE = prev
            else:
                prev = dynamics_search.TOKEN_FILE
                dynamics_search.TOKEN_FILE = user_token
                _dyn_log(f"search avviata | query={query[:80]!r} | "
                         f"resource_url={cfg.get('resource_url','')!r} | "
                         f"token_file_esiste={user_token.is_file()}")
                _ai = ai_settings or {}
                _dyn_log(f"planner AI | motore={_ai.get('ai_engine','(assente)')} | "
                         f"chiave_claude={'presente' if _ai.get('claude_api_key') else 'ASSENTE'} | "
                         f"modello={_ai.get('claude_model','(default)')}")
                if not cfg.get("resource_url"):
                    _dyn_log("ERRORE: resource_url VUOTO — il token non può avere lo "
                             "scope Dynamics. Configura l'URL istanza in Admin e RICONNETTI.")
                try:
                    dyn = dynamics_search.DynamicsSearch(_dyn_full_cfg(cfg, ai_settings))
                    text = dyn.search(query, max_results=max_results,
                                      current_user_name=current_user_name) or ""
                    _dyn_log(f"search conclusa | caratteri_risultato={len(text)}")
                    # Estrai eventuale report HTML generato dal modulo: lo
                    # registriamo per token e lo togliamo dal testo (così non
                    # finisce grezzo nel contesto inviato a Claude).
                    links = []
                    m = _REPORT_RE.search(text)
                    if m:
                        token = register_report(user, m.group(1).strip())
                        text = text.replace(m.group(0), "").lstrip()
                        links.append({"name": "Report Dynamics (HTML)",
                                      "url": "/dyn-report/" + token, "kind": "report"})
                    return text, links
                finally:
                    dynamics_search.TOKEN_FILE = prev
        except Exception as e:
            import traceback
            _dyn_log("ECCEZIONE durante la ricerca " + conn + ":\n" + traceback.format_exc())
            return "", []


# ── Catalogo entità Dynamics (mappa relazioni + schema .md) ─
def _jwt_audience(token: str) -> str:
    """Estrae il claim 'aud' (audience) da un JWT senza verificarne la firma:
    serve solo a capire PER QUALE risorsa è stato emesso il token."""
    try:
        import base64
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload).decode("utf-8", "replace"))
        return str(data.get("aud", ""))
    except Exception:
        return ""


def dyn_diagnose(user: str) -> str:
    """Diagnostica end-to-end della connessione Dynamics dell'utente: resource
    URL, presenza token, ottenimento access token, audience del token e una
    chiamata di prova. Pensata per l'admin: dice dove si rompe la catena."""
    out = []
    if not is_connected(user, "dynamics"):
        return ("Dynamics non risulta connesso per questo utente.\n"
                "Vai in Connessioni e completa l'accesso device-code.")
    cfg = ms_cfg("dynamics")
    res = cfg.get("resource_url", "")
    out.append("1) URL istanza (resource_url): " + (res if res else "*** VUOTO ***"))
    if not res:
        out.append("   ⛔ Senza URL istanza il token non può avere lo scope Dynamics.")
        out.append("   → Imposta l'URL in Admin, poi DISCONNETTI e RICONNETTI Dynamics.")
        return "\n".join(out)
    user_token = _token_path(user, "dynamics")
    out.append("2) File token utente: " + ("presente" if user_token.is_file() else "ASSENTE"))
    with _LOCK:
        prev = dynamics_search.TOKEN_FILE
        dynamics_search.TOKEN_FILE = user_token
        try:
            dyn = dynamics_search.DynamicsSearch({
                "dyn_client_id": cfg["client_id"],
                "dyn_tenant_id": cfg["tenant_id"],
                "dyn_resource_url": res,
                "dyn_schema_dir": str(dynamics_search.SCHEMA_DIR),
            })
            tok = ""
            try:
                tok = dyn.tm.get_access_token() or ""
                out.append("3) Access token: " + ("ottenuto" if tok else "NON ottenuto"))
            except Exception as e:
                out.append("3) Access token: ERRORE " + str(e)[:160])
            if tok:
                aud = _jwt_audience(tok)
                out.append("4) Audience del token (aud): " + (aud or "(non leggibile)"))
                res_host = res.replace("https://", "").rstrip("/")
                if aud and (res in aud or res_host in aud):
                    out.append("   ✅ Il token punta all'istanza Dynamics giusta.")
                else:
                    out.append("   ⛔ Il token NON è per questa istanza Dynamics "
                               "(scope sbagliato al momento della connessione).")
                    out.append("   → DISCONNETTI e RICONNETTI Dynamics ora che l'URL è impostato.")
                # 5) chiamata di prova
                try:
                    import requests
                    r = requests.get(res + "/data/$metadata",
                                     headers={"Authorization": "Bearer " + tok},
                                     timeout=30)
                    out.append("5) GET /data/$metadata → HTTP " + str(r.status_code))
                    if r.status_code == 401:
                        out.append("   ⛔ 401 Unauthorized: token senza accesso. Riconnetti.")
                    elif r.status_code == 200:
                        out.append("   ✅ Dynamics risponde correttamente.")
                except Exception as e:
                    out.append("5) Chiamata di prova: ERRORE " + str(e)[:160])
        finally:
            dynamics_search.TOKEN_FILE = prev
    return "\n".join(out)


def dyn_catalog_status() -> dict:
    """Stato del catalogo Dynamics: presente, n. entità, relazioni, schema .md."""
    cf = Path(dynamics_search.CATALOG_FILE)
    if not cf.exists():
        return {"present": False}
    try:
        cat = json.loads(cf.read_text(encoding="utf-8"))
        sd = Path(dynamics_search.SCHEMA_DIR)
        n_md = len(list(sd.glob("*.md"))) if sd.is_dir() else 0
        return {
            "present": True,
            "count": cat.get("count", 0),
            "relazioni": cat.get("relazioni", 0),
            "generato": cat.get("generato", ""),
            "istanza": cat.get("istanza", ""),
            "versione": cat.get("versione", ""),
            "schema_md": n_md,
        }
    except Exception:
        return {"present": False}


def dyn_build_catalog(user: str) -> dict:
    """Genera (o rigenera) il catalogo entità + schema .md interrogando il
    $metadata dell'istanza, con il token Dynamics dell'utente (admin). Lungo:
    scarica i metadati completi (~migliaia di entità). Ritorna il risultato."""
    if not is_connected(user, "dynamics"):
        return {"errore": "Account Dynamics non connesso: collega prima il tuo account in Connessioni."}
    cfg = ms_cfg("dynamics")
    user_token = _token_path(user, "dynamics")
    with _LOCK:
        prev = dynamics_search.TOKEN_FILE
        dynamics_search.TOKEN_FILE = user_token
        try:
            dyn = dynamics_search.DynamicsSearch({
                "dyn_client_id": cfg["client_id"],
                "dyn_tenant_id": cfg["tenant_id"],
                "dyn_resource_url": cfg["resource_url"],
                "dyn_schema_dir": str(dynamics_search.SCHEMA_DIR),
            })
            return dyn.build_full_catalog()
        except Exception as e:
            return {"errore": str(e)}
        finally:
            dynamics_search.TOKEN_FILE = prev

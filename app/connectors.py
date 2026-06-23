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
           current_user_name: str = "") -> str:
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
                    dyn = dynamics_search.DynamicsSearch({
                        "dyn_client_id": cfg["client_id"],
                        "dyn_tenant_id": cfg["tenant_id"],
                        "dyn_resource_url": cfg["resource_url"],
                        "dyn_schema_dir": str(dynamics_search.SCHEMA_DIR),
                    })
                    return dyn.search(query, max_results=max_results,
                                      current_user_name=current_user_name) or ""
                finally:
                    dynamics_search.TOKEN_FILE = prev
        except Exception:
            return ""


# ── Catalogo entità Dynamics (mappa relazioni + schema .md) ─
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

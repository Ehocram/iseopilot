"""
Ricerca unificata Microsoft 365 (Incremento 11) — SharePoint, Posta, Teams.

Usa l'API di ricerca unificata di Graph (POST /search/query) con l'identità
DELEGATA dell'utente: ciascuno vede esclusivamente ciò a cui ha già accesso in
Microsoft 365. Nessuna elevazione di privilegio, nessun account di servizio.

Principio di minimizzazione, esplicito nel disegno:
  • al MODELLO va solo lo SNIPPET restituito da Graph più i metadati
    (mittente, data, oggetto) — mai il corpo integrale del messaggio;
  • il CORPO INTEGRALE è scaricabile dall'utente su richiesta, recuperato da
    Graph al momento del download e servito come file: non transita
    dall'API del modello.

Governance: kill-switch globale, interruttori per singola fonte e concessione
per-utente vivono in connectors.py/main.py — qui c'è solo il motore.
"""
import datetime
import html
import json
import re
import time
from pathlib import Path

GRAPH = "https://graph.microsoft.com/v1.0"
# Scope delegati: SharePoint (Sites.Read.All + Files.Read.All), Posta
# (Mail.Read), Teams (Chat.Read). offline_access per il refresh.
M365_SCOPE = ("Files.Read.All Sites.Read.All Mail.Read Chat.Read "
              "User.Read offline_access")

FONTI = ("sharepoint", "mail", "teams")
_ENTITY = {
    "sharepoint": ["driveItem", "listItem"],
    "mail": ["message"],
    "teams": ["chatMessage"],
}
SNIPPET_MAX = 1200          # per singolo risultato, verso il modello
TESTO_MAX = 14000           # totale del blocco di contesto


def _log(msg: str) -> None:
    import sys
    print(f"[m365] {msg}", file=sys.stderr)


class TokenM365:
    """Token per-utente su file (percorso nel cfg: nessun globale condiviso,
    stessa scelta fatta per Power BI)."""

    def __init__(self, cfg: dict):
        self.client_id = cfg.get("client_id", "")
        self.tenant_id = cfg.get("tenant_id", "")
        self.path = Path(cfg.get("token_path", ""))
        self._data = {}
        try:
            if self.path.exists():
                self._data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            self._data = {}

    def _save(self, d: dict) -> None:
        try:
            self._data = d
            self.path.write_text(json.dumps(d, indent=2), encoding="utf-8")
        except Exception:
            pass

    def access_token(self) -> str:
        if not self._data:
            return ""
        tok = self._data.get("access_token", "")
        if tok and time.time() < self._data.get("expires_at", 0) - 300:
            return tok
        rt = self._data.get("refresh_token", "")
        if not rt:
            return tok
        try:
            import requests
            r = requests.post(
                f"https://login.microsoftonline.com/{self.tenant_id}/oauth2/v2.0/token",
                data={"client_id": self.client_id, "grant_type": "refresh_token",
                      "refresh_token": rt, "scope": M365_SCOPE}, timeout=20)
            resp = r.json()
            if "access_token" in resp:
                resp["expires_at"] = time.time() + resp.get("expires_in", 3600)
                self._save(resp)
                return resp["access_token"]
            _log(f"refresh FALLITO: {str(resp.get('error_description', resp))[:180]}")
        except Exception as e:
            _log(f"refresh ECCEZIONE: {e}")
        return tok


def _strip_html(s: str) -> str:
    s = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", s or "", flags=re.S | re.I)
    s = re.sub(r"<br\s*/?>|</p>|</div>|</tr>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)
    s = re.sub(r"[ \t]+", " ", s)
    # i tag rimossi lasciano spazi prima della punteggiatura ("riavviata .")
    s = re.sub(r"\s+([,.;:!?%»)\]])", r"\1", s)
    s = re.sub(r"([«(\[])\s+", r"\1", s)
    return re.sub(r"\n{3,}", "\n\n", s).strip()


def _data_breve(iso: str) -> str:
    if not iso:
        return ""
    try:
        return datetime.datetime.fromisoformat(
            iso.replace("Z", "+00:00")).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(iso)[:16]


class M365Search:
    """Ricerca unificata su SharePoint, Posta e Teams."""

    def __init__(self, cfg: dict):
        self.cfg = cfg or {}
        self.tm = TokenM365(self.cfg)

    # ── ricerca ─────────────────────────────────────────────
    def search(self, query: str, fonti: list, max_results: int = 5) -> tuple:
        """Ritorna (testo_per_modello, riferimenti). I riferimenti portano gli
        identificativi per l'eventuale download del contenuto integrale."""
        fonti = [f for f in (fonti or []) if f in FONTI]
        if not fonti:
            return "", []
        tok = self.tm.access_token()
        if not tok:
            return "[Microsoft 365] Non connesso: collega il connettore dalla pagina Connessioni.", []
        entita = []
        for f in fonti:
            entita.extend(_ENTITY[f])
        richieste = [{
            "entityTypes": entita,
            "query": {"queryString": query},
            "from": 0,
            "size": max(1, min(int(max_results or 5), 25)),
        }]
        try:
            import requests
            r = requests.post(f"{GRAPH}/search/query",
                              headers={"Authorization": "Bearer " + tok,
                                       "Content-Type": "application/json"},
                              json={"requests": richieste}, timeout=45)
            if r.status_code == 403:
                _log(f"403 su /search/query — permessi mancanti per {fonti}")
                return ("[Microsoft 365] Permessi insufficienti per le fonti richieste: "
                        "l'amministratore deve concedere il consenso ai permessi "
                        "delegati (Sites.Read.All, Mail.Read, Chat.Read) e l'utente "
                        "deve riconnettere il connettore.", [])
            if r.status_code >= 400:
                _log(f"HTTP {r.status_code}: {r.text[:300]}")
                return (f"[Microsoft 365] Ricerca non riuscita (HTTP {r.status_code}). "
                        "Dettagli nel log connettori.", [])
            data = r.json()
        except Exception as e:
            _log(f"ECCEZIONE ricerca: {e}")
            return f"[Microsoft 365] Errore di rete verso Graph: {str(e)[:120]}", []

        blocchi, riferimenti = [], []
        for gruppo in data.get("value", []):
            for hc in gruppo.get("hitsContainers", []):
                for hit in hc.get("hits", []):
                    voce = self._formatta(hit)
                    if not voce:
                        continue
                    testo, rif = voce
                    blocchi.append(testo)
                    if rif:
                        riferimenti.append(rif)
        if not blocchi:
            return "", []
        _log(f"query={query[:60]!r} fonti={fonti} risultati={len(blocchi)}")
        return ("\n\n".join(blocchi))[:TESTO_MAX], riferimenti

    def _formatta(self, hit: dict):
        res = hit.get("resource") or {}
        tipo = (res.get("@odata.type") or "").split(".")[-1].lower()
        sunto = _strip_html(hit.get("summary") or "")[:SNIPPET_MAX]

        if tipo == "message":
            mitt = (((res.get("from") or {}).get("emailAddress") or {}).get("address") or "")
            nome = (((res.get("from") or {}).get("emailAddress") or {}).get("name") or "")
            ogg = res.get("subject") or "(senza oggetto)"
            quando = _data_breve(res.get("receivedDateTime") or "")
            rif = {"kind": "mail", "id": res.get("id") or "",
                   "titolo": ogg, "quando": quando, "da": nome or mitt}
            testo = (f"[Posta — {ogg}]\nDa: {nome} <{mitt}> · {quando}\n{sunto}")
            return testo, rif

        if tipo == "chatmessage":
            mitt = (((res.get("from") or {}).get("user") or {}).get("displayName") or "")
            quando = _data_breve(res.get("createdDateTime") or "")
            corpo = _strip_html(((res.get("body") or {}).get("content") or ""))[:SNIPPET_MAX]
            chat_id = res.get("chatId") or ""
            rif = {"kind": "teams", "id": res.get("id") or "", "chat_id": chat_id,
                   "titolo": f"Chat Teams del {quando}", "quando": quando, "da": mitt}
            testo = (f"[Teams — messaggio del {quando}]\nDa: {mitt}\n"
                     f"{corpo or sunto}")
            return testo, rif

        # driveItem / listItem → SharePoint
        nome = res.get("name") or (res.get("fields") or {}).get("title") or "documento"
        url = res.get("webUrl") or ""
        sito = ""
        try:
            sito = ((res.get("parentReference") or {}).get("siteId") or "").split(",")[0]
        except Exception:
            sito = ""
        quando = _data_breve(res.get("lastModifiedDateTime") or "")
        rif = {"kind": "sharepoint", "id": res.get("id") or "",
               "drive_id": ((res.get("parentReference") or {}).get("driveId") or ""),
               "titolo": nome, "quando": quando, "url": url, "sito": sito}
        testo = f"[SharePoint — {nome}]{(' · ' + quando) if quando else ''}\n{sunto}"
        return testo, rif

    # ── contenuto integrale (solo su richiesta dell'utente) ──
    def fetch_full(self, kind: str, ident: str, chat_id: str = "") -> tuple:
        """Scarica il contenuto INTEGRALE. Ritorna (bytes, filename, mime).
        NON passa mai dal modello: è un download diretto per l'utente."""
        tok = self.tm.access_token()
        if not tok:
            raise ValueError("Connettore Microsoft 365 non connesso.")
        import requests
        h = {"Authorization": "Bearer " + tok}

        if kind == "mail":
            r = requests.get(f"{GRAPH}/me/messages/{ident}/$value", headers=h, timeout=60)
            if r.status_code >= 400:
                raise ValueError(f"Messaggio non recuperabile (HTTP {r.status_code}).")
            meta = requests.get(f"{GRAPH}/me/messages/{ident}"
                                "?$select=subject,receivedDateTime", headers=h, timeout=30)
            ogg = "messaggio"
            if meta.status_code < 400:
                ogg = (meta.json().get("subject") or "messaggio")
            return r.content, _safe_name(ogg) + ".eml", "message/rfc822"

        if kind == "teams":
            if not chat_id:
                raise ValueError("Identificativo chat mancante per il messaggio Teams.")
            r = requests.get(f"{GRAPH}/me/chats/{chat_id}/messages/{ident}",
                             headers=h, timeout=45)
            if r.status_code >= 400:
                raise ValueError(f"Messaggio Teams non recuperabile (HTTP {r.status_code}).")
            m = r.json()
            mitt = (((m.get("from") or {}).get("user") or {}).get("displayName") or "")
            quando = _data_breve(m.get("createdDateTime") or "")
            corpo = _strip_html(((m.get("body") or {}).get("content") or ""))
            testo = (f"Messaggio Teams\nDa: {mitt}\nData: {quando}\n"
                     f"Chat: {chat_id}\n\n{corpo}\n")
            return testo.encode("utf-8"), _safe_name(f"Teams {quando} {mitt}") + ".txt", "text/plain"

        if kind == "sharepoint":
            drive_id = chat_id      # riuso del parametro per il driveId
            if not drive_id:
                raise ValueError("Identificativo drive mancante per il file SharePoint.")
            meta = requests.get(f"{GRAPH}/drives/{drive_id}/items/{ident}"
                                "?$select=name", headers=h, timeout=30)
            nome = "documento"
            if meta.status_code < 400:
                nome = meta.json().get("name") or "documento"
            r = requests.get(f"{GRAPH}/drives/{drive_id}/items/{ident}/content",
                             headers=h, timeout=120)
            if r.status_code >= 400:
                raise ValueError(f"File non scaricabile (HTTP {r.status_code}).")
            return r.content, nome, "application/octet-stream"

        raise ValueError(f"Tipo di contenuto non gestito: {kind}")


def _safe_name(s: str) -> str:
    s = re.sub(r"[^\w\s.-]", "", str(s or "")).strip()
    return (s or "contenuto")[:80]

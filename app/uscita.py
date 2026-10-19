"""
uscita.py — L'UNICA porta verso Claude (api.anthropic.com) e verso LM Studio.

Qui, e solo qui, vivono l'URL di Anthropic e i suoi header (chiave e versione
dell'API): un test statico verifica che nessun altro file di app/ li contenga.
Chi deve parlare con Claude chiama `invia(scopo, payload, chiave)` e dichiara
lo SCOPO; la tabella SCOPI dice per ogni scopo se i dati partono protetti
(segnaposto), in chiaro per scelta del CISO, o se non partono dati affatto.

Controllo in uscita (scopi «protetto»): se il chiamante passa l'Anonymizer che
ha usato (`controllo`), prima dell'invio si cerca nel payload ogni valore
originale della sua mappa (almeno 4 caratteri, a parola intera, senza
distinguere maiuscole). Se ne resta anche uno, la richiesta NON parte:
UscitaBloccata, senza valori nel messaggio. Per costruzione non dovrebbe
scattare mai: è la rete sotto i bug futuri (un testo dimenticato fuori dal
mascheramento). Le istruzioni fisse del chiamante (`fisso`) possono contenere
per caso un valore della mappa (gli esempi «di Marco Bonometti» del planner):
lì non è una fuga, quindi si conta solo ciò che eccede le occorrenze del fisso.

LM Studio: l'endpoint deve risolvere SOLO a indirizzi privati o di loopback,
sia quando l'admin lo salva sia a ogni chiamata. Un URL pubblico porterebbe
fuori i dati in chiaro (verso LM Studio non si maschera).

Log: una riga per invio con scopo, politica, caratteri, esito e millisecondi.
Mai il testo.
"""
from __future__ import annotations

import ipaddress
import re
import socket
import sys
import time
from urllib.parse import urlsplit

import requests

from .anonymizer import _SEGNAPOSTO

_URL_MESSAGGI = "https://api.anthropic.com/v1/messages"
_URL_MODELLI = "https://api.anthropic.com/v1/models"
_VERSIONE_API = "2023-06-01"

PROTETTO, CHIARO, NESSUN_DATO = "protetto", "chiaro", "nessun_dato"

# Politica di ogni scopo, con il motivo scritto: è la risposta a «cosa parte
# verso Claude, e come?» senza leggere il codice dei chiamanti.
SCOPI = {
    "chat": {"politica": PROTETTO, "motivo":
             "messaggi, contesto delle fonti, memoria ed esempi passano da UN "
             "mask_batch; le risposte si ripristinano in streaming"},
    "docgen": {"politica": PROTETTO, "motivo":
               "richiesta, note, storico e materiale mascherati una volta; la "
               "specifica JSON si ripristina foglia per foglia"},
    "cowork": {"politica": PROTETTO, "motivo":
               "UNA mappa per l'intera attività: la memoria dei passi si "
               "maschera a ogni passo"},
    "docedit": {"politica": PROTETTO, "motivo":
                "richiesta, note e testo del documento allegato mascherati; il "
                "piano di modifiche si ripristina prima di applicarlo"},
    "riscrittura": {"politica": PROTETTO, "motivo":
                    "la domanda parte mascherata; la query riscritta si "
                    "ripristina prima della ricerca locale"},
    "dynamics": {"politica": PROTETTO, "motivo":
                 "planner Dynamics: domanda, campioni ERP e osservazioni "
                 "mascherati; catalogo e istruzioni sono metadati"},
    "powerbi": {"politica": CHIARO, "motivo":
                "scelta del CISO: connettore spento in produzione; il planner "
                "riceve schema e campioni in chiaro"},
    "dub": {"politica": CHIARO, "motivo":
            "scelta del CISO: Dub Studio traduce i sottotitoli di video "
            "aziendali di formazione, funzione riservata all'admin"},
    "devchat": {"politica": CHIARO, "motivo":
                "scelta del CISO: chat sviluppatore solo admin, invia codice "
                "sorgente dell'applicazione e nessun dato utente"},
    "modelli": {"politica": NESSUN_DATO, "motivo":
                "elenco dei modelli disponibili per la chiave: nessun testo "
                "parte"},
}

MSG_BLOCCATA = "Un dato riservato non è stato sostituito: richiesta bloccata"
VALORE_MIN = 4             # valori più corti non si cercano: troppe coincidenze
_APOSTROFI = "'’ʼ"

# Reti ammesse per LM Studio: private RFC 1918, loopback, ULA IPv6 e ::1.
RETI_PRIVATE = tuple(ipaddress.ip_network(n) for n in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8",
    "fc00::/7", "::1/128"))


class UscitaBloccata(Exception):
    """Un valore della mappa è rimasto in chiaro nel payload. NON è un
    ValueError: docgen ritenta sui ValueError e la maschererebbe."""

    def __init__(self, tipo: str, conteggio: int):
        super().__init__(MSG_BLOCCATA)
        self.tipo = tipo
        self.conteggio = conteggio


class EndpointNonPrivato(RuntimeError):
    """L'endpoint di LM Studio non risolve a un indirizzo privato."""


def politica(scopo: str) -> str:
    """Politica dello scopo; scopo sconosciuto = ValueError (prima di ogni I/O)."""
    voce = SCOPI.get(scopo)
    if voce is None:
        raise ValueError(f"scopo di uscita sconosciuto: {scopo!r}")
    return voce["politica"]


def _log(scopo: str, pol: str, caratteri: int, esito: str, t0: float) -> None:
    print(f"[uscita] scopo={scopo} politica={pol} caratteri={caratteri} "
          f"esito={esito} ms={int((time.monotonic() - t0) * 1000)}", file=sys.stderr)


def _header(api_key: str) -> dict:
    return {"x-api-key": api_key, "anthropic-version": _VERSIONE_API,
            "content-type": "application/json"}


# ── Controllo in uscita ─────────────────────────────────────
def _testi(payload: dict) -> list:
    """Le stringhe che portano testo: system (stringa o blocchi) e contenuto
    dei messaggi (stringa, blocchi di testo, risultati degli strumenti)."""
    out = []

    def _blocchi(v):
        if isinstance(v, str):
            out.append(v)
        elif isinstance(v, list):
            for b in v:
                if isinstance(b, dict):
                    if isinstance(b.get("text"), str):
                        out.append(b["text"])
                    if "content" in b:          # tool_result
                        _blocchi(b["content"])
                elif isinstance(b, str):
                    out.append(b)
    _blocchi(payload.get("system"))
    for m in payload.get("messages") or []:
        if isinstance(m, dict):
            _blocchi(m.get("content"))
    return out


def _norma(v: str) -> str:
    v = re.sub(r"\s+", " ", v.strip())
    return re.sub(r"[’ʼ]", "'", v).casefold()


def _modello(val: str) -> str:
    """Un valore come regex: spazi qualsiasi fra le parole, apostrofi equivalenti."""
    parti = []
    for ch in re.sub(r"\s+", " ", val.strip()):
        if ch == " ":
            parti.append(r"\s+")
        elif ch in _APOSTROFI:
            parti.append("['’ʼ]")
        else:
            parti.append(re.escape(ch))
    return "".join(parti)


def _conta(rx, testi) -> dict:
    """Occorrenze per valore normalizzato, FUORI dai segnaposto: «Nome» nel
    dizionario non deve scattare su [NOME_001]."""
    conti: dict = {}
    for t in testi:
        if not t:
            continue
        for m in rx.finditer(_SEGNAPOSTO.sub(" ", t)):
            k = _norma(m.group(0))
            conti[k] = conti.get(k, 0) + 1
    return conti


def verifica_payload(payload: dict, controllo, fisso=()) -> None:
    """Solleva UscitaBloccata se un valore originale della mappa di `controllo`
    compare nel payload più volte che nelle istruzioni fisse. A parola intera
    come le regex del mascheramento (un «_» attaccato è parte della parola),
    senza distinguere maiuscole."""
    tipi = {}
    for tok, val in (controllo.get_map() or {}).items():
        if val and len(val.strip()) >= VALORE_MIN:
            tipi.setdefault(_norma(val), tok.strip("[]").rsplit("_", 1)[0])
    if not tipi:
        return
    ordinati = sorted(tipi, key=lambda k: (-len(k), k))
    rx = re.compile(r"(?<!\w)(?:" + "|".join(_modello(v) for v in ordinati) + r")(?!\w)",
                    re.IGNORECASE)
    nel_payload = _conta(rx, _testi(payload))
    if not nel_payload:
        return
    nel_fisso = _conta(rx, [f for f in fisso if isinstance(f, str)])
    rimasti = [k for k, n in nel_payload.items() if n > nel_fisso.get(k, 0)]
    if rimasti:
        per_tipo: dict = {}
        for k in rimasti:
            t = tipi.get(k, "?")
            per_tipo[t] = per_tipo.get(t, 0) + 1
        tipo = max(per_tipo, key=lambda t: (per_tipo[t], t))
        raise UscitaBloccata(tipo, len(rimasti))


# ── Claude ──────────────────────────────────────────────────
def invia(scopo: str, payload: dict, api_key: str, *, stream: bool = False,
          timeout: float = 120, controllo=None, fisso=()) -> requests.Response:
    """POST a /v1/messages con lo scopo dichiarato. Ritorna la Response così
    com'è (status, json, iter_lines in streaming): retry, errori e parsing
    restano ai chiamanti. Le eccezioni di rete passano invariate."""
    pol = politica(scopo)
    if pol == NESSUN_DATO:
        raise ValueError(f"lo scopo {scopo!r} non invia dati: usa elenca_modelli")
    t0 = time.monotonic()
    caratteri = sum(len(t) for t in _testi(payload))
    if pol == PROTETTO and controllo is not None:
        try:
            verifica_payload(payload, controllo, fisso)
        except UscitaBloccata as e:
            _log(scopo, pol, caratteri, f"bloccata:{e.tipo}x{e.conteggio}", t0)
            raise
    kw = {"headers": _header(api_key), "json": payload, "timeout": timeout}
    if stream:
        kw["stream"] = True
    try:
        r = requests.post(_URL_MESSAGGI, **kw)
    except Exception as e:
        _log(scopo, pol, caratteri, f"errore:{type(e).__name__}", t0)
        raise
    _log(scopo, pol, caratteri, str(getattr(r, "status_code", "?")), t0)
    return r


def elenca_modelli(api_key: str, timeout: float = 15) -> requests.Response:
    """GET /v1/models: nessun dato parte, solo la chiave."""
    pol = politica("modelli")
    t0 = time.monotonic()
    try:
        r = requests.get(_URL_MODELLI, headers={"x-api-key": api_key,
                                                "anthropic-version": _VERSIONE_API},
                         timeout=timeout)
    except Exception as e:
        _log("modelli", pol, 0, f"errore:{type(e).__name__}", t0)
        raise
    _log("modelli", pol, 0, str(getattr(r, "status_code", "?")), t0)
    return r


# ── LM Studio (locale) ──────────────────────────────────────
def _privato(ip: str) -> bool:
    try:
        a = ipaddress.ip_address(ip.split("%", 1)[0])
    except ValueError:
        return False
    if a.version == 6 and a.ipv4_mapped is not None:
        a = a.ipv4_mapped
    return any(a.version == n.version and a in n for n in RETI_PRIVATE)


def verifica_lm_url(url: str) -> str:
    """'' se l'URL è ammesso, altrimenti il motivo del rifiuto. L'host deve
    risolvere SOLO a indirizzi privati o di loopback: basta un indirizzo
    pubblico fra quelli restituiti dal DNS per rifiutarlo."""
    try:
        parti = urlsplit((url or "").strip())
        host, porta = parti.hostname, parti.port
    except ValueError:
        return "URL di LM Studio non valido."
    if parti.scheme not in ("http", "https") or not host:
        return "URL di LM Studio non valido: serve http:// o https:// con un host."
    try:
        ipaddress.ip_address(host.split("%", 1)[0])
        indirizzi = [host]
    except ValueError:
        try:
            info = socket.getaddrinfo(host, porta or (443 if parti.scheme == "https" else 80),
                                      type=socket.SOCK_STREAM)
        except (socket.gaierror, OSError, UnicodeError):
            return f"L'host di LM Studio «{host}» non si risolve: impossibile verificare che sia interno."
        indirizzi = [i[4][0] for i in info]
    if not indirizzi:
        return f"L'host di LM Studio «{host}» non si risolve: impossibile verificare che sia interno."
    if not all(_privato(ip) for ip in indirizzi):
        return (f"LM Studio deve stare su un indirizzo privato o locale: «{host}» "
                "risolve a un indirizzo pubblico.")
    return ""


def invia_locale(url: str, payload: dict, *, stream: bool = False,
                 timeout: float = 300) -> requests.Response:
    """POST all'endpoint OpenAI-compatibile di LM Studio, solo se privato."""
    t0 = time.monotonic()
    caratteri = sum(len(t) for t in _testi(payload))
    errore = verifica_lm_url(url)
    if errore:
        _log("lmstudio", "locale", caratteri, "rifiutata:endpoint_non_privato", t0)
        raise EndpointNonPrivato(errore)
    kw = {"headers": {"Content-Type": "application/json"}, "json": payload,
          "timeout": timeout}
    if stream:
        kw["stream"] = True
    try:
        r = requests.post(url, **kw)
    except Exception as e:
        _log("lmstudio", "locale", caratteri, f"errore:{type(e).__name__}", t0)
        raise
    _log("lmstudio", "locale", caratteri, str(getattr(r, "status_code", "?")), t0)
    return r

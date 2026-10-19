"""
presidio_client.py — Client HTTP verso il sidecar «iseopilot-presidio».

Il sidecar non ha stato: riceve testo e restituisce SOLO intervalli
(inizio, fine, tipo, punteggio). Dizionario e mappa dei segnaposto non
lasciano mai iseopilot. Il testo inviato è quello dopo le sole regex (prima
del dizionario): il sidecar è interno, senza stato e senza uscita.

Contratto (vincolante per app e sidecar):
  POST {PRESIDIO_URL}/v1/analyze   header X-Presidio-Token
  {"segmenti": [{"id": "p0", "testo": "..."}]}
  -> {"versione": "...", "modello": "...", "risultati": {"p0": [[0, 5, "PERSON", 0.85]]}, "ms": 12}
  Offset = indici di stringa Python sul testo inviato, fine esclusa.

Scelte:
  * segmentazione per paragrafo (righe vuote, al massimo 4.000 caratteri);
  * cache LRU di 5.000 voci con scadenza di 1 h su sha256 del paragrafo: contiene
    SOLO intervalli, mai testo;
  * UNA chiamata HTTP per richiesta, con i soli paragrafi che non sono in cache;
  * timeout di 3 s più 1 s ogni 20.000 caratteri, al massimo 20 s; la stessa
    scadenza va al sidecar (X-Presidio-Scadenza-Ms), che smette di lavorare
    per una richiesta che il client ha già abbandonato;
  * interruttore: dopo un timeout o un errore di rete Presidio si salta
    per SOSPENSIONE_S secondi (motivo «sospeso»): un sidecar bloccato non
    aggiunge l'intero timeout a ogni invio;
  * ogni anomalia (non configurato, rete, 4xx/5xx, JSON non valido, intervalli
    fuori dal testo, entità sconosciuta) solleva PresidioNonDisponibile: il
    chiamante procede con dizionario + regex e avvisa l'utente (decisione del
    CISO: niente blocco). Nei log mai il testo, solo conteggi ed esito.
"""
from __future__ import annotations

import hashlib
import os
import re
import sys
import threading
import time
from collections import OrderedDict

import requests

# Entità ammesse e soglia minima di punteggio (la applica anche il sidecar).
ENTITA = {
    "PERSON": 0.5,
    "PHONE_NUMBER": 0.4,
    "IT_IDENTITY_CARD": 0.0,
    "IT_PASSPORT": 0.0,
    "IT_DRIVER_LICENSE": 0.0,
}
MAX_PARAGRAFO = 4_000
MAX_CARATTERI = 500_000
CACHE_VOCI = 5_000
CACHE_TTL = 3600.0
TIMEOUT_BASE = 3.0
TIMEOUT_MAX = 20.0
SOSPENSIONE_S = 30.0
SOSPENSIONE_CARATTERI = 100_000

_SEPARATORE = re.compile(r"\n[ \t\r\f\v]*\n")

_cache: "OrderedDict[str, tuple[float, tuple]]" = OrderedDict()
_cache_lock = threading.Lock()
_avvisato_non_configurato = False
_sospeso_fino: dict = {}           # url -> istante (monotonic) fino a cui non si chiama


class PresidioNonDisponibile(Exception):
    """Presidio non ha lavorato. `motivo` è un codice breve, mai testo dell'utente."""

    def __init__(self, motivo: str):
        super().__init__(motivo)
        self.motivo = motivo


def _config() -> tuple[str, str]:
    """Letti a ogni chiamata da os.environ: niente riavvio per cambiarli nei test."""
    url = (os.environ.get("PRESIDIO_URL") or "").strip().rstrip("/")
    token = (os.environ.get("PRESIDIO_TOKEN") or "").strip()
    return url, token


def configurato() -> bool:
    return bool(_config()[0])


def _paragrafi(testo: str) -> list[tuple[int, str]]:
    """(offset, paragrafo) per ogni paragrafo non vuoto. Un paragrafo oltre
    4.000 caratteri si spezza a fine riga o, in mancanza, a uno spazio."""
    pezzi, inizio = [], 0
    for m in _SEPARATORE.finditer(testo):
        pezzi.append((inizio, testo[inizio:m.start()]))
        inizio = m.end()
    pezzi.append((inizio, testo[inizio:]))
    out = []
    for off, p in pezzi:
        while len(p) > MAX_PARAGRAFO:
            taglio = p.rfind("\n", 0, MAX_PARAGRAFO)
            if taglio < MAX_PARAGRAFO // 2:
                taglio = p.rfind(" ", 0, MAX_PARAGRAFO)
            if taglio < MAX_PARAGRAFO // 2:
                taglio = MAX_PARAGRAFO
            if p[:taglio].strip():
                out.append((off, p[:taglio]))
            off, p = off + taglio, p[taglio:]
        if p.strip():
            out.append((off, p))
    return out


def _chiave_cache(url: str, paragrafo: str) -> str:
    return hashlib.sha256((url + "\0" + paragrafo).encode("utf-8")).hexdigest()


def _cache_get(k: str):
    with _cache_lock:
        v = _cache.get(k)
        if v is None:
            return None
        if time.monotonic() - v[0] > CACHE_TTL:
            del _cache[k]
            return None
        _cache.move_to_end(k)
        return v[1]


def _cache_put(k: str, spans: tuple) -> None:
    with _cache_lock:
        _cache[k] = (time.monotonic(), spans)
        _cache.move_to_end(k)
        while len(_cache) > CACHE_VOCI:
            _cache.popitem(last=False)


def svuota_cache() -> None:
    """Svuota la cache e azzera l'interruttore (test, cambio di configurazione)."""
    with _cache_lock:
        _cache.clear()
        _sospeso_fino.clear()


def _sospendi_se_guasto(url: str, motivo: str, caratteri: int) -> None:
    """Timeout e rete aprono l'interruttore: sono i guasti che costano tempo a
    ogni invio. Un timeout su un testo enorme può dipendere dal testo e non dal
    sidecar: apre solo sotto SOSPENSIONE_CARATTERI. Un 503 (coda piena, non
    pronto) o un 4xx arrivano subito: niente sospensione."""
    if (motivo == "timeout" and caratteri < SOSPENSIONE_CARATTERI) or motivo.startswith("rete:"):
        with _cache_lock:
            _sospeso_fino[url] = time.monotonic() + SOSPENSIONE_S
        print(f"[presidio] sospeso per {int(SOSPENSIONE_S)} s dopo {motivo}", file=sys.stderr)


def _sospeso(url: str) -> bool:
    with _cache_lock:
        fino = _sospeso_fino.get(url)
        if fino is None:
            return False
        if time.monotonic() >= fino:
            del _sospeso_fino[url]
            return False
        return True


def _valida(grezzi, testo: str) -> tuple:
    """Intervalli del sidecar per UN paragrafo. Qualunque stranezza vale come
    guasto: meglio l'avviso all'utente che un intervallo applicato a caso."""
    if not isinstance(grezzi, list):
        raise PresidioNonDisponibile("risposta_non_valida")
    spans = []
    for x in grezzi:
        if not isinstance(x, (list, tuple)) or len(x) != 4:
            raise PresidioNonDisponibile("intervallo_non_valido")
        a, b, ent, sc = x
        if type(a) is not int or type(b) is not int or not (0 <= a < b <= len(testo)):
            raise PresidioNonDisponibile("intervallo_fuori_testo")
        if ent not in ENTITA:
            raise PresidioNonDisponibile("entita_sconosciuta")
        if isinstance(sc, bool) or not isinstance(sc, (int, float)):
            raise PresidioNonDisponibile("punteggio_non_valido")
        if sc >= ENTITA[ent]:
            spans.append((a, b, ent, float(sc)))
    return tuple(spans)


def _chiedi(url: str, token: str, paragrafi: dict) -> dict:
    """UNA chiamata per i paragrafi non in cache. Ritorna {chiave: intervalli}."""
    caratteri = sum(len(p) for p in paragrafi.values())
    if caratteri > MAX_CARATTERI:
        raise PresidioNonDisponibile("testo_oltre_limite")
    if not token:
        raise PresidioNonDisponibile("token_mancante")
    ids = {f"p{n}": k for n, k in enumerate(paragrafi)}
    body = {"segmenti": [{"id": i, "testo": paragrafi[k]} for i, k in ids.items()]}
    timeout = min(TIMEOUT_MAX, TIMEOUT_BASE + caratteri / 20_000)
    t0 = time.monotonic()
    try:
        r = requests.post(url + "/v1/analyze", json=body, timeout=timeout,
                          headers={"X-Presidio-Token": token,
                                   "X-Presidio-Scadenza-Ms": str(int(timeout * 1000))})
    except requests.Timeout:
        raise PresidioNonDisponibile("timeout")
    except requests.RequestException as e:
        raise PresidioNonDisponibile(f"rete:{type(e).__name__}")
    if r.status_code != 200:
        raise PresidioNonDisponibile(f"http_{r.status_code}")
    try:
        data = r.json()
    except ValueError:
        raise PresidioNonDisponibile("json_non_valido")
    risultati = data.get("risultati") if isinstance(data, dict) else None
    if not isinstance(risultati, dict):
        raise PresidioNonDisponibile("risposta_non_valida")
    out = {}
    for sid, k in ids.items():
        out[k] = _valida(risultati.get(sid), paragrafi[k])
    ms = int((time.monotonic() - t0) * 1000)
    print(f"[presidio] ok: paragrafi={len(ids)} caratteri={caratteri} "
          f"entita={sum(len(v) for v in out.values())} ms={ms}", file=sys.stderr)
    return out


def analizza(testi: list) -> list:
    """Per ogni testo, la lista degli intervalli (inizio, fine, tipo, punteggio)
    con offset sul testo intero. Solleva PresidioNonDisponibile su ogni anomalia."""
    global _avvisato_non_configurato
    url, token = _config()
    if not url:
        if not _avvisato_non_configurato:
            _avvisato_non_configurato = True
            print("[presidio] non configurato (PRESIDIO_URL vuoto): solo dizionario e regex",
                  file=sys.stderr)
        raise PresidioNonDisponibile("non_configurato")
    posizioni = []        # (indice testo, offset paragrafo, chiave)
    noti: dict = {}       # chiave -> intervalli già in cache
    da_chiedere: dict = {}
    for i, t in enumerate(testi):
        for off, p in _paragrafi(t or ""):
            k = _chiave_cache(url, p)
            posizioni.append((i, off, k))
            if k in noti or k in da_chiedere:
                continue
            spans = _cache_get(k)
            if spans is None:
                da_chiedere[k] = p
            else:
                noti[k] = spans
    if da_chiedere:
        # Interruttore aperto: niente chiamata (i paragrafi in cache bastano
        # solo se bastano tutti).
        if _sospeso(url):
            raise PresidioNonDisponibile("sospeso")
        try:
            nuovi = _chiedi(url, token, da_chiedere)
        except PresidioNonDisponibile as e:
            print(f"[presidio] non disponibile ({e.motivo}): paragrafi={len(da_chiedere)} "
                  f"caratteri={sum(len(p) for p in da_chiedere.values())}", file=sys.stderr)
            _sospendi_se_guasto(url, e.motivo, sum(len(p) for p in da_chiedere.values()))
            raise
        for k, spans in nuovi.items():
            _cache_put(k, spans)
        noti.update(nuovi)
    risultati = [[] for _ in testi]
    for i, off, k in posizioni:
        for a, b, ent, sc in noti[k]:
            risultati[i].append((a + off, b + off, ent, sc))
    return risultati

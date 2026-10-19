"""
app.py — API HTTP del sidecar Presidio (porta 3000, nessuno stato).

Contratto con iseopilot (app/presidio_client.py):
  GET  /health      200 {"ok": true} appena il processo è su.
  GET  /ready       200 solo se motore caricato, self-test superato e token
                    configurato (>= 32 caratteri); altrimenti 503.
  POST /v1/analyze  header X-Presidio-Token, body {"segmenti": [{"id", "testo"}]}
                    -> {"versione", "modello", "risultati": {id: [[inizio, fine,
                    tipo, punteggio]]}, "ms"}. Offset = indici di stringa Python
                    sul testo ricevuto, fine esclusa.
Codici: 401 token assente, 403 token errato, 413 oltre 500.000 caratteri,
503 non pronto, coda piena o richiesta abbandonata, 500 errore o timeout di una
regex, 400 body non valido. L'header facoltativo X-Presidio-Scadenza-Ms (il
timeout del client) fissa la scadenza della richiesta, al massimo 20 s: oltre,
o a client disconnesso, l'analisi si interrompe e il log dice «abbandonata». Se il sidecar non risponde bene, iseopilot NON blocca l'invio: procede
con dizionario e regole e avvisa l'utente in chat.

Log: solo numero di segmenti, caratteri, millisecondi ed esito. MAI il testo.
"""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import sys
import time

import anyio
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import selftest
from .motore import Motore, RegexTimeout, RichiestaScaduta  # presidio e spaCy solo in Motore()

log = logging.getLogger("presidio_svc")
if not log.handlers:
    _h = logging.StreamHandler(sys.stderr)
    _h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s presidio_svc %(message)s"))
    log.addHandler(_h)
    log.setLevel(logging.INFO)
    log.propagate = False

MAX_CARATTERI = 500_000
# Tetto sui byte del body, prima di leggerlo: nel JSON un carattere vale al
# massimo 12 byte (coppia surrogata come \udXXX\udXXX), più gli id.
MAX_BYTE = MAX_CARATTERI * 12 + 1_000_000
MAX_SEGMENTI = 20_000
MAX_ID = 200
# Una analisi alla volta: il container ha 1 CPU (due analisi insieme non vanno
# più veloci) e le memory_zone di spaCy non si sovrappongono fra thread.
CONCORRENTI = 1
CODA = 9                                # in attesa; oltre, 503
# Scadenza di una richiesta: quella dichiarata dal client, entro questi limiti.
# Il client di iseopilot aspetta al massimo 20 s: dopo, il lavoro è inutile e
# farebbe aspettare oltre il loro timeout le richieste in coda.
SCADENZA_MIN_MS = 500
SCADENZA_MAX_MS = 20_000
TOKEN_MIN = 32
RIVERIFICA_S = 300                      # /ready rifà il self-test al più ogni 5 minuti

# ── Token ───────────────────────────────────────────────────
# Senza un token robusto il servizio non lavora: /ready e /v1/analyze 503.
_TOKEN = (os.environ.get("PRESIDIO_TOKEN") or "").strip()
TOKEN_OK = len(_TOKEN) >= TOKEN_MIN
if not TOKEN_OK:
    log.error("PRESIDIO_TOKEN assente o più corto di %d caratteri: servizio non pronto", TOKEN_MIN)

# ── Motore ──────────────────────────────────────────────────
# Si carica all'import: con gunicorn --preload succede una volta nel master e il
# worker (riciclato ogni ~500 richieste) lo eredita già pronto. Un guasto qui
# non fa cadere il processo: /health risponde, /ready dice 503 e perché.
_t0 = time.monotonic()
motore = None
_motivo = "motore non caricato"
try:
    motore = Motore()
    _pronto, _motivo = selftest.esegui(motore)
except Exception as e:                   # noqa: BLE001
    _pronto, _motivo = False, f"caricamento fallito: {type(e).__name__}"
    # All'avvio non c'è testo dell'utente: il messaggio si può scrivere.
    log.error("motore non caricato: %s: %s", type(e).__name__, e)
_verificato = time.monotonic()
MS_AVVIO = int((time.monotonic() - _t0) * 1000)
if motore is not None:
    log.info("motore %s %s caricato in %d ms, self-test: %s",
             motore.versione, motore.modello, MS_AVVIO, _motivo)

app = FastAPI(title="iseopilot-presidio", docs_url=None, redoc_url=None, openapi_url=None)

_semaforo: asyncio.Semaphore | None = None
_in_corso = 0                            # ammesse: in analisi + in coda
_lock_ready: asyncio.Lock | None = None


def _sem() -> asyncio.Semaphore:
    # Creato al primo uso, dentro il loop del worker (non nel master).
    global _semaforo
    if _semaforo is None:
        _semaforo = asyncio.Semaphore(CONCORRENTI)
    return _semaforo


def _errore(codice: int, messaggio: str, **header) -> JSONResponse:
    return JSONResponse({"errore": messaggio}, status_code=codice, headers=header or None)


def _registra(segmenti: int, caratteri: int, t0: float, esito: str) -> None:
    log.info("analyze segmenti=%d caratteri=%d ms=%d esito=%s",
             segmenti, caratteri, int((time.monotonic() - t0) * 1000), esito)


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/ready")
async def ready():
    global _pronto, _motivo, _verificato, _lock_ready
    if not TOKEN_OK:
        return JSONResponse({"ok": False, "motivo": "token non configurato"}, status_code=503)
    if motore is None:
        return JSONResponse({"ok": False, "motivo": _motivo}, status_code=503)
    if time.monotonic() - _verificato > RIVERIFICA_S:
        # Riverifica periodica: il motore deve funzionare anche nel worker,
        # non solo nel master che l'ha caricato.
        if _lock_ready is None:
            _lock_ready = asyncio.Lock()
        async with _lock_ready:
            if time.monotonic() - _verificato > RIVERIFICA_S:
                _pronto, _motivo = await anyio.to_thread.run_sync(selftest.esegui, motore)
                _verificato = time.monotonic()
                if not _pronto:
                    log.error("self-test fallito: %s", _motivo)
    if not _pronto:
        return JSONResponse({"ok": False, "motivo": _motivo}, status_code=503)
    return {"ok": True, "versione": motore.versione, "modello": motore.modello}


def _leggi_segmenti(dati) -> list[tuple[str, str]] | None:
    """Valida il body. None se la forma non è quella del contratto."""
    if not isinstance(dati, dict) or not isinstance(dati.get("segmenti"), list):
        return None
    segmenti, visti = [], set()
    if len(dati["segmenti"]) > MAX_SEGMENTI:
        return None
    for s in dati["segmenti"]:
        if not isinstance(s, dict):
            return None
        sid, testo = s.get("id"), s.get("testo")
        if not isinstance(sid, str) or not sid or len(sid) > MAX_ID or sid in visti:
            return None
        if not isinstance(testo, str):
            return None
        visti.add(sid)
        segmenti.append((sid, testo))
    return segmenti


@app.post("/v1/analyze")
async def analyze(request: Request):
    global _in_corso
    t0 = time.monotonic()
    if not TOKEN_OK or motore is None or not _pronto:
        _registra(0, 0, t0, "non_pronto")
        return _errore(503, "servizio non pronto", **{"Retry-After": "30"})
    # Token: confronto a tempo costante, prima di leggere il body.
    ricevuto = request.headers.get("x-presidio-token")
    if not ricevuto:
        _registra(0, 0, t0, "token_assente")
        return _errore(401, "token assente")
    if not hmac.compare_digest(ricevuto.strip().encode("utf-8"), _TOKEN.encode("utf-8")):
        _registra(0, 0, t0, "token_errato")
        return _errore(403, "token errato")
    if _in_corso >= CONCORRENTI + CODA:
        _registra(0, 0, t0, "occupato")
        return _errore(503, "servizio occupato", **{"Retry-After": "1"})
    # Dimensione: si rifiuta prima di tenere in memoria un body enorme.
    try:
        dichiarati = int(request.headers.get("content-length") or 0)
    except ValueError:
        dichiarati = 0
    if dichiarati > MAX_BYTE:
        _registra(0, 0, t0, "troppo_grande")
        return _errore(413, "testo oltre 500.000 caratteri")
    corpo = bytearray()
    async for pezzo in request.stream():
        corpo.extend(pezzo)
        if len(corpo) > MAX_BYTE:
            _registra(0, 0, t0, "troppo_grande")
            return _errore(413, "testo oltre 500.000 caratteri")
    try:
        segmenti = _leggi_segmenti(json.loads(corpo))
    except (ValueError, UnicodeDecodeError, RecursionError):
        segmenti = None
    if segmenti is None:
        _registra(0, 0, t0, "non_valida")
        return _errore(400, "richiesta non valida")
    caratteri = sum(len(t) for _, t in segmenti)
    if caratteri > MAX_CARATTERI:
        _registra(len(segmenti), caratteri, t0, "troppo_grande")
        return _errore(413, "testo oltre 500.000 caratteri")
    # Coda: 1 analisi alla volta e 9 in attesa (ricontrollato dopo la
    # lettura del body, che può aver lasciato entrare altre richieste).
    if _in_corso >= CONCORRENTI + CODA:
        _registra(len(segmenti), caratteri, t0, "occupato")
        return _errore(503, "servizio occupato", **{"Retry-After": "1"})
    try:
        ms_scadenza = int(request.headers.get("x-presidio-scadenza-ms") or SCADENZA_MAX_MS)
    except ValueError:
        ms_scadenza = SCADENZA_MAX_MS
    scadenza = t0 + min(SCADENZA_MAX_MS, max(SCADENZA_MIN_MS, ms_scadenza)) / 1000
    _in_corso += 1
    try:
        async with _sem():
            # Dopo l'attesa in coda: se il client se n'è andato o la scadenza è
            # passata, iseopilot ha già proceduto con dizionario e regole.
            if time.monotonic() > scadenza or await request.is_disconnected():
                _registra(len(segmenti), caratteri, t0, "abbandonata")
                return _errore(503, "richiesta abbandonata")
            risultati = await anyio.to_thread.run_sync(motore.analizza, segmenti, scadenza)
    except RichiestaScaduta:
        _registra(len(segmenti), caratteri, t0, "abbandonata")
        return _errore(503, "richiesta abbandonata")
    except RegexTimeout:
        _registra(len(segmenti), caratteri, t0, "timeout_regex")
        return _errore(500, "timeout di un riconoscitore")
    except Exception as e:                # noqa: BLE001 - nessun dettaglio verso il client
        _registra(len(segmenti), caratteri, t0, f"errore_{type(e).__name__}")
        return _errore(500, "errore interno")
    finally:
        _in_corso -= 1
    ms = int((time.monotonic() - t0) * 1000)
    _registra(len(segmenti), caratteri, t0, "ok")
    return {"versione": motore.versione, "modello": motore.modello,
            "risultati": risultati, "ms": ms}

"""
Test del sidecar Presidio (riconoscitori, contesto, API, rete).
Esecuzione, dalla radice del repo nel venv del sidecar o dentro l'immagine:
    python -m presidio_svc.tests.test_riconoscitori            # test
    python -m presidio_svc.tests.test_riconoscitori --misure   # + RAM, avvio, latenze
Sul 139, prima dell'up:
    sudo docker run --rm --network none iseopilot-presidio:latest \
        python -m presidio_svc.tests.test_riconoscitori

La RETE È BLOCCATA prima di caricare il motore: ogni tentativo di connessione
(o di risoluzione DNS) viene registrato e fallisce. Il test finale verifica che
non ce ne siano stati. I socket AF_UNIX restano permessi (asyncio li usa).
"""
import os
import secrets
import socket
import sys
import time

# Lanciato anche come file (python presidio_svc/tests/test_riconoscitori.py).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

# ── Rete bloccata ───────────────────────────────────────────
TENTATIVI_RETE: list[str] = []
_connect_vero = socket.socket.connect
_connect_ex_vero = socket.socket.connect_ex


def _locale(sock) -> bool:
    return getattr(socket, "AF_UNIX", None) is not None and sock.family == socket.AF_UNIX


def _connect(self, addr, *a, **kw):
    if _locale(self):
        return _connect_vero(self, addr, *a, **kw)
    TENTATIVI_RETE.append(f"connect {addr!r}")
    raise OSError("rete bloccata dal test")


def _connect_ex(self, addr, *a, **kw):
    if _locale(self):
        return _connect_ex_vero(self, addr, *a, **kw)
    TENTATIVI_RETE.append(f"connect_ex {addr!r}")
    raise OSError("rete bloccata dal test")


def _dns(*a, **kw):
    TENTATIVI_RETE.append(f"dns {a[:1]!r}")
    raise socket.gaierror("rete bloccata dal test")


def _create_connection(addr, *a, **kw):
    TENTATIVI_RETE.append(f"create_connection {addr!r}")
    raise OSError("rete bloccata dal test")


socket.socket.connect = _connect
socket.socket.connect_ex = _connect_ex
socket.getaddrinfo = _dns
socket.gethostbyname = _dns
socket.gethostbyname_ex = _dns
socket.create_connection = _create_connection

# Token di prova: il test non usa mai quello di produzione.
TOKEN = secrets.token_hex(32)
os.environ["PRESIDIO_TOKEN"] = TOKEN

_T_IMPORT = time.monotonic()
from fastapi.testclient import TestClient  # noqa: E402

from presidio_svc import app as app_mod  # noqa: E402  (carica il motore)
from presidio_svc import contesto, motore as motore_mod, selftest  # noqa: E402

S_AVVIO = time.monotonic() - _T_IMPORT
client = TestClient(app_mod.app)
H = {"X-Presidio-Token": TOKEN}


def analizza(*testi):
    r = client.post("/v1/analyze", headers=H,
                    json={"segmenti": [{"id": f"s{i}", "testo": t} for i, t in enumerate(testi)]})
    assert r.status_code == 200, (r.status_code, r.text)
    return r.json()


def trovati(testo):
    """{(valore, tipo)} trovati dal servizio nel testo."""
    ris = analizza(testo)["risultati"]["s0"]
    return {(testo[s:e], tipo) for s, e, tipo, _ in ris}


def persone(testo):
    return {v for v, t in trovati(testo) if t == "PERSON"}


# -- Servizio --
def test_health_e_ready():
    assert client.get("/health").json() == {"ok": True}
    r = client.get("/ready")
    assert r.status_code == 200, r.text
    assert r.json()["modello"] == "it_core_news_md-3.8.0"
    assert r.json()["versione"] == "presidio-analyzer 2.2.364"


def test_selftest_motore():
    ok, motivo = selftest.esegui(app_mod.motore)
    assert ok, motivo


def test_risposta_come_da_contratto():
    d = analizza("Ho scritto a Mario Rossi.", "")
    assert set(d) == {"versione", "modello", "risultati", "ms"}
    assert d["versione"] == "presidio-analyzer 2.2.364"
    assert d["modello"] == "it_core_news_md-3.8.0"
    assert isinstance(d["ms"], int)
    assert d["risultati"]["s1"] == []
    (s, e, tipo, p), = d["risultati"]["s0"]
    assert (s, e, tipo) == (13, 24, "PERSON") and 0.5 <= p <= 1


def test_registro_esplicito_senza_parser():
    reg = app_mod.motore.engine.registry
    nomi = sorted(type(r).__name__ for r in reg.recognizers)
    assert nomi == ["ItDriverLicenseRecognizer", "ItIdentityCardRecognizer",
                    "ItPassportRecognizer", "PhoneRecognizer", "SpacyRecognizer"], nomi
    assert reg.supported_languages == ["it"]
    nlp = app_mod.motore.engine.nlp_engine.get_nlp("it")
    assert "parser" not in nlp.pipe_names and "ner" in nlp.pipe_names
    tel = next(r for r in reg.recognizers if type(r).__name__ == "PhoneRecognizer")
    assert tuple(tel.supported_regions) == ("IT", "CH", "DE", "FR", "ES", "GB", "US", "AT", "RO")


# -- Persone --
def test_persone_italiane():
    assert {"Mario Rossi", "Giovanni Verdi"} <= persone(
        "Ciao, sono Mario Rossi e lavoro con Giovanni Verdi.")
    assert "Mario Rossi" in persone("Mario Rossi")
    assert "Giovanni Verdi" in persone("Giovanni Verdi")
    assert "Francesca D'Angelo" in persone(
        "Ieri ho parlato con la dott.ssa Francesca D'Angelo dell'ufficio acquisti.")
    assert "Luca Bianchi" in persone("Puoi inoltrare la mail a Luca Bianchi entro venerdì?")


def test_luoghi_e_organizzazioni_non_restituiti():
    tipi = {t for _, t in trovati("Il fornitore Rossi Srl di Brescia spedisce a Milano.")}
    assert tipi <= {"PERSON"}, tipi            # LOC/ORG/MISC ignorati per contratto


# -- Telefoni --
def test_telefoni_italiani():
    t = "Chiamami allo 030 1234567 oppure al +39 333 1234567 o al 333 1234567."
    tel = {v for v, tipo in trovati(t) if tipo == "PHONE_NUMBER"}
    assert tel == {"030 1234567", "+39 333 1234567", "333 1234567"}, tel


def test_telefono_estero():
    tel = {v for v, t in trovati("Il collega di Zurigo risponde al +41 44 668 18 00.")
           if t == "PHONE_NUMBER"}
    assert tel == {"+41 44 668 18 00"}, tel


# -- Documenti: solo con contesto --
def test_documenti_con_contesto():
    t = "Carta d'identità CA12345AB, passaporto YA1234567, patente MI1234567A."
    f = trovati(t)
    assert ("CA12345AB", "IT_IDENTITY_CARD") in f
    assert ("YA1234567", "IT_PASSPORT") in f
    assert ("MI1234567A", "IT_DRIVER_LICENSE") in f
    assert ("AX1234567", "IT_IDENTITY_CARD") in trovati("Allego la carta di identità AX1234567.")
    assert ("1234567AB", "IT_IDENTITY_CARD") in trovati("numero CIE 1234567AB")
    assert ("CA12345AB", "IT_IDENTITY_CARD") in trovati("carta d’identità n. CA12345AB")


def test_documenti_senza_contesto_scartati():
    t = "Codice articolo YA1234567, lotto CA12345AB, matricola MI1234567A."
    tipi = {tipo for _, tipo in trovati(t)}
    assert not tipi & set(contesto.TIPI_DOCUMENTO), tipi


def test_contesto_solo_nei_40_caratteri_prima():
    lontano = "Passaporto scaduto, poi molte altre parole in mezzo qui: YA1234567"
    assert lontano.index("YA1234567") > 40
    assert not {t for _, t in trovati(lontano)} & set(contesto.TIPI_DOCUMENTO)
    dopo = "YA1234567 è il numero del passaporto"
    assert not {t for _, t in trovati(dopo)} & set(contesto.TIPI_DOCUMENTO)


# -- Allow-list --
def test_allow_list_esatta():
    p = persone("Chiedi a Claude, poi scrivi su Teams e apri Power BI con Excel e Copilot.")
    assert not p & contesto.ALLOW_PERSONE, p
    # Esatta, non «contiene»: Claude Martin resta una persona.
    assert "Claude Martin" in persone("La riunione con Claude Martin è confermata.")


def test_allow_list_funzione():
    testo = "Claude e Mario Rossi"
    out = contesto.filtra(testo, [(0, 6, "PERSON", 0.85), (9, 20, "PERSON", 0.85),
                                  (9, 20, "LOCATION", 0.85), (9, 20, "PERSON", 0.3)])
    assert out == [(9, 20, "PERSON", 0.85)], out


# -- Offset --
def test_offset_in_code_point():
    t = "😀🇮🇹 nota: Mario Rossi chiama lo 030 1234567"
    ris = analizza(t)["risultati"]["s0"]
    valori = {(t[s:e], tipo) for s, e, tipo, _ in ris}
    assert ("Mario Rossi", "PERSON") in valori and ("030 1234567", "PHONE_NUMBER") in valori


def test_testo_lungo_a_finestre():
    # Un nome a cavallo del taglio fra due finestre si trova una volta, intero.
    blocco = motore_mod.BLOCCO
    riempitivo = "Il magazzino conferma la spedizione dei pezzi. "
    prima = (riempitivo * (blocco // len(riempitivo) + 1))[:blocco - 5]
    t = prima + " Mario Rossi conferma. " + riempitivo * 400 + "Telefono 030 1234567."
    ris = analizza(t)["risultati"]["s0"]
    mario = [(s, e) for s, e, tipo, _ in ris if t[s:e] == "Mario Rossi"]
    assert len(mario) == 1, ris[:5]
    assert any(t[s:e] == "030 1234567" for s, e, _, _ in ris)
    assert all(e - s < 60 for s, e, _, _ in ris)


# -- API: token, limiti, errori --
def test_token():
    corpo = {"segmenti": [{"id": "a", "testo": "Mario Rossi"}]}
    assert client.post("/v1/analyze", json=corpo).status_code == 401
    r = client.post("/v1/analyze", json=corpo, headers={"X-Presidio-Token": "x" * 64})
    assert r.status_code == 403
    assert "Mario" not in r.text


def test_token_corto_non_pronto():
    vecchio = (app_mod.TOKEN_OK, app_mod._TOKEN)
    try:
        app_mod.TOKEN_OK, app_mod._TOKEN = False, "corto"
        assert client.get("/ready").status_code == 503
        r = client.post("/v1/analyze", headers={"X-Presidio-Token": "corto"},
                        json={"segmenti": [{"id": "a", "testo": "x"}]})
        assert r.status_code == 503
        assert client.get("/health").status_code == 200
    finally:
        app_mod.TOKEN_OK, app_mod._TOKEN = vecchio


def test_limite_caratteri():
    metà = "a" * 250_001
    r = client.post("/v1/analyze", headers=H, json={"segmenti": [
        {"id": "a", "testo": metà}, {"id": "b", "testo": metà}]})
    assert r.status_code == 413
    r = client.post("/v1/analyze", headers={**H, "Content-Length": str(app_mod.MAX_BYTE + 1)},
                    content=b"{}")
    assert r.status_code in (400, 413)          # httpx può correggere l'header


def test_body_non_valido():
    for corpo in ([], {}, {"segmenti": "x"}, {"segmenti": [{"id": "", "testo": "a"}]},
                  {"segmenti": [{"id": "a", "testo": 3}]},
                  {"segmenti": [{"id": "a", "testo": "x"}, {"id": "a", "testo": "y"}]}):
        r = client.post("/v1/analyze", headers=H, json=corpo)
        assert r.status_code == 400, (corpo, r.status_code)
    r = client.post("/v1/analyze", headers=H, content=b"{non json")
    assert r.status_code == 400


def test_coda_piena_503():
    vecchio = app_mod._in_corso
    try:
        app_mod._in_corso = app_mod.CONCORRENTI + app_mod.CODA
        r = client.post("/v1/analyze", headers=H, json={"segmenti": [{"id": "a", "testo": "x"}]})
        assert r.status_code == 503 and r.headers.get("retry-after") == "1"
    finally:
        app_mod._in_corso = vecchio


def test_concorrenza_una_analisi_alla_volta():
    # Più richieste insieme: al massimo CONCORRENTI (1) analisi nello stesso istante.
    import threading
    attive, picco, lock = [0], [0], threading.Lock()
    vero = app_mod.motore.analizza

    def lento(segmenti, scadenza=None):
        with lock:
            attive[0] += 1
            picco[0] = max(picco[0], attive[0])
        time.sleep(0.2)
        try:
            return vero(segmenti, scadenza)
        finally:
            with lock:
                attive[0] -= 1

    app_mod.motore.analizza = lento
    try:
        esiti = []
        # Un solo event loop per tutte le richieste, come il worker di gunicorn.
        import anyio

        async def tante():
            import httpx
            tr = httpx.ASGITransport(app=app_mod.app)
            async with httpx.AsyncClient(transport=tr, base_url="http://t") as c:
                async def una():
                    r = await c.post("/v1/analyze", headers=H,
                                     json={"segmenti": [{"id": "a", "testo": "Mario Rossi"}]})
                    esiti.append(r.status_code)
                async with anyio.create_task_group() as tg:
                    for _ in range(14):
                        tg.start_soon(una)
        anyio.run(tante)
    finally:
        app_mod.motore.analizza = vero
    assert picco[0] <= app_mod.CONCORRENTI, picco
    assert esiti.count(200) == app_mod.CONCORRENTI + app_mod.CODA, esiti
    assert esiti.count(503) == 14 - app_mod.CONCORRENTI - app_mod.CODA, esiti


def test_scadenza_interrompe_il_lavoro():
    # Il client ha già proceduto senza Presidio: il sidecar non finisce
    # un'analisi lunga per niente e la richiesta dopo non aspetta.
    import io
    import logging
    buf = io.StringIO()
    h = logging.StreamHandler(buf)
    lg = logging.getLogger("presidio_svc")
    lg.addHandler(h)
    testo = ("Ho sentito Mario Rossi al 333 1234567 per l'ordine 4500012345. " * 4000)[:200_000]
    try:
        t0 = time.monotonic()
        r = client.post("/v1/analyze", headers={**H, "X-Presidio-Scadenza-Ms": "500"},
                        json={"segmenti": [{"id": "a", "testo": testo}]})
        durata = time.monotonic() - t0
    finally:
        lg.removeHandler(h)
    assert r.status_code == 503 and "abbandonata" in r.text, (r.status_code, r.text)
    assert durata < 2.5, durata                      # senza scadenza: ~4 s sul Mac
    assert "esito=abbandonata" in buf.getvalue() and "Mario" not in buf.getvalue()
    # Scadenza già passata quando tocca a lei (attesa in coda): nessun lavoro.
    try:
        app_mod.motore.analizza([("a", "Mario Rossi")], time.monotonic() - 1)
        assert False, "doveva scadere"
    except motore_mod.RichiestaScaduta:
        pass
    assert ("Mario Rossi", "PERSON") in trovati("Ho scritto a Mario Rossi.")


def test_vocabolario_non_cresce():
    # memory_zone: le parole mai viste (codici, matricole, cognomi) non restano
    # nel vocabolario di spaCy; senza, il worker superava il tetto di 1 GB.
    import random
    voc = app_mod.motore.engine.nlp_engine.get_nlp(motore_mod.LINGUA).vocab
    analizza("riscaldamento Mario Rossi 030 1234567")
    prima = len(voc.strings)
    r = random.Random(7)
    for _ in range(5):
        righe = [f"Ordine {r.randint(10**9, 10**10)} matricola AB{r.randint(10**6, 10**7)} "
                 f"ticket INC{r.randint(10**6, 10**7)} cliente Zq{r.randint(10**5, 10**6)}x."
                 for _ in range(200)]
        analizza("\n".join(righe))
    assert len(voc.strings) - prima < 50, (prima, len(voc.strings))


def test_timeout_regex_diventa_500():
    import presidio_analyzer.pattern_recognizer as pr
    vecchio = pr.REGEX_TIMEOUT_SECONDS
    try:
        pr.REGEX_TIMEOUT_SECONDS = 1e-7         # ogni regex «va in timeout»
        r = client.post("/v1/analyze", headers=H, json={"segmenti": [
            {"id": "a", "testo": "passaporto YA1234567 " * 5000}]})
        assert r.status_code == 500, r.status_code
        assert "YA1234567" not in r.text
    finally:
        pr.REGEX_TIMEOUT_SECONDS = vecchio
    # Dopo il guasto il servizio torna normale.
    assert ("YA1234567", "IT_PASSPORT") in trovati("passaporto YA1234567")


def test_log_senza_testo():
    import io
    import logging
    buf = io.StringIO()
    h = logging.StreamHandler(buf)
    lg = logging.getLogger("presidio_svc")
    lg.addHandler(h)
    try:
        analizza("Canarino Teodolinda Sbrizzabarba al 030 7654321")
        client.post("/v1/analyze", headers={"X-Presidio-Token": "x" * 40},
                    json={"segmenti": [{"id": "a", "testo": "Teodolinda"}]})
    finally:
        lg.removeHandler(h)
    out = buf.getvalue()
    assert "segmenti=1" in out and "esito=ok" in out and "esito=token_errato" in out, out
    assert "Teodolinda" not in out and "7654321" not in out, out


# -- Rete --
def test_rete_bloccata_davvero():
    n = len(TENTATIVI_RETE)
    try:
        socket.create_connection(("pypi.org", 443), timeout=1)
        assert False, "connessione riuscita"
    except OSError:
        pass
    del TENTATIVI_RETE[n:]


def test_zzz_nessun_accesso_alla_rete():
    # Ultimo test (ordine alfabetico): caricamento e analisi senza rete.
    assert TENTATIVI_RETE == [], TENTATIVI_RETE


# -- Misure (solo con --misure) --
def _rss_mb() -> float:
    try:
        with open("/proc/self/status") as f:
            for riga in f:
                if riga.startswith("VmRSS:"):
                    return int(riga.split()[1]) / 1024
    except OSError:
        pass
    import subprocess
    out = subprocess.run(["ps", "-o", "rss=", "-p", str(os.getpid())],
                         capture_output=True, text=True).stdout
    return int(out.strip()) / 1024


def misure():
    print(f"\nAvvio (import + modello + self-test): {S_AVVIO:.1f} s "
          f"(motore {app_mod.motore.ms_caricamento} ms)")
    print(f"RSS dopo il caricamento e i test: {_rss_mb():.0f} MB")
    base = ("Buongiorno, ho sentito ieri Mario Rossi dell'ufficio acquisti: il fornitore chiede "
            "di richiamarlo allo 030 1234567 o al 333 1234567 entro venerdì. Giovanni Verdi "
            "conferma l'ordine 4500012345 per 12.500 euro. Il report di Power BI è in "
            "SharePoint.\n\n")
    for kb in (2, 30, 200):
        testo = (base * (kb * 1024 // len(base) + 1))[:kb * 1024]
        tempi = []
        for _ in range(3):
            t0 = time.perf_counter()
            d = analizza(testo)
            tempi.append(time.perf_counter() - t0)
        print(f"{kb:>4} KB: {len(d['risultati']['s0'])} intervalli, "
              f"min {min(tempi)*1000:.0f} ms, max {max(tempi)*1000:.0f} ms, "
              f"RSS {_rss_mb():.0f} MB")


if __name__ == "__main__":
    import inspect
    import traceback
    fns = [f for n, f in sorted(globals().items()) if n.startswith("test_") and inspect.isfunction(f)]
    failed = 0
    for f in fns:
        try:
            f()
            print(f"  PASS  {f.__name__}")
        except Exception as e:
            failed += 1
            print(f"  FAIL  {f.__name__}: {e!r}")
            traceback.print_exc()
    print(f"\n{len(fns)-failed}/{len(fns)} test superati.")
    if "--misure" in sys.argv:
        misure()
    sys.exit(1 if failed else 0)

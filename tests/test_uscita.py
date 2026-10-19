"""
Uscita verso Claude e LM Studio (app/uscita.py): l'UNICA porta.

Copre: nessun altro file di app/ conosce URL e header di Anthropic; ogni scopo
usato nel codice esiste in SCOPI; scopo sconosciuto rifiutato prima di
qualunque I/O; controllo in uscita che blocca un canarino rimasto in chiaro;
riscrittura della query mascherata e ripristinata; complete() con LM Studio
che non chiama mai Claude; lm_url solo privato (DNS finto). Nessun test va in
rete: requests e socket.getaddrinfo sono intercettati.

Esecuzione:
    PYTHONPATH=. APP_DATA_DIR=$(mktemp -d) .venv/bin/python tests/test_uscita.py
"""
import json
import os
import re
import socket
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

os.environ.setdefault("APP_DATA_DIR", "./data_test")
from cryptography.fernet import Fernet
os.environ.setdefault("APP_SECRET_KEY", Fernet.generate_key().decode())
os.environ.pop("PRESIDIO_URL", None)        # Presidio spento: solo dizionario e regex

from fastapi.testclient import TestClient

from app import auth, docgen, orchestrator, store, uscita
from app import main as app_main
from app.anonymizer import Anonymizer

store.init_db()

APP_DIR = Path(__file__).resolve().parent.parent / "app"
CANARINO = "Zefiro Canarino"


# ── Rete finta ──────────────────────────────────────────────────────────────
class _Rete:
    """requests.post intercettato: registra le chiamate, risponde come Claude
    o come LM Studio secondo l'URL."""

    def __init__(self, testo="ok", stato=200):
        self.testo, self.stato = testo, stato
        self.chiamate = []

    def __call__(self, url, json=None, headers=None, timeout=None, **kw):
        self.chiamate.append((url, json))
        r = MagicMock(status_code=self.stato, text="errore finto")
        if "anthropic.com" in url:
            r.json.return_value = {"content": [{"type": "text", "text": self.testo}]}
        else:
            r.json.return_value = {"choices": [{"message": {"content": self.testo}}]}
        return r

    def claude(self):
        return [c for c in self.chiamate if "anthropic.com" in c[0]]


def _vietato(*a, **k):
    raise AssertionError("nessun I/O atteso")


def _dns(ip):
    """getaddrinfo finto: ogni host risolve a `ip` (o a più indirizzi)."""
    ips = [ip] if isinstance(ip, str) else list(ip)

    def _f(host, porta, *a, **k):
        return [(socket.AF_INET6 if ":" in i else socket.AF_INET, socket.SOCK_STREAM,
                 6, "", (i, porta)) for i in ips]
    return _f


# ── Statici ─────────────────────────────────────────────────────────────────
def test_solo_uscita_conosce_anthropic():
    """In app/ nessun file tranne uscita.py contiene URL o header di Anthropic."""
    vietate = (b"api.anthropic.com", b"x-api-key", b"anthropic-version")
    trovati = []
    for p in APP_DIR.rglob("*"):
        if not p.is_file() or "__pycache__" in p.parts or p.name == "uscita.py":
            continue
        dati = p.read_bytes().lower()
        trovati += [f"{p.relative_to(APP_DIR)}: {v.decode()}" for v in vietate if v in dati]
    assert not trovati, trovati


def test_ogni_scopo_usato_esiste_in_scopi():
    usati = set()
    for p in APP_DIR.rglob("*.py"):
        testo = p.read_text(encoding="utf-8")
        usati |= set(re.findall(r'uscita\.invia\(\s*"([^"]+)"', testo))
        usati |= set(re.findall(r'\bscopo\s*=\s*"([^"]+)"', testo))
    attesi = {"chat", "docgen", "cowork", "docedit", "riscrittura", "dynamics",
              "powerbi", "dub", "devchat"}
    assert attesi <= usati, attesi - usati
    assert usati <= set(uscita.SCOPI), usati - set(uscita.SCOPI)
    for nome, voce in uscita.SCOPI.items():
        assert voce["politica"] in (uscita.PROTETTO, uscita.CHIARO, uscita.NESSUN_DATO)
        assert len(voce["motivo"]) > 20, nome
    assert {s for s, v in uscita.SCOPI.items() if v["politica"] == uscita.CHIARO} \
        == {"powerbi", "dub", "devchat"}


# ── Scopo sconosciuto ───────────────────────────────────────────────────────
def test_scopo_sconosciuto_valueerror_senza_io():
    payload = {"model": "m", "messages": [{"role": "user", "content": "ciao"}]}
    with patch("requests.post", _vietato), patch("requests.get", _vietato), \
            patch("socket.getaddrinfo", _vietato):
        for scopo in ("ignoto", "", "CHAT"):
            try:
                uscita.invia(scopo, payload, "k")
                raise AssertionError("atteso ValueError")
            except ValueError:
                pass
        # anche da complete(), con qualunque motore
        for motore in ("claude", "lmstudio"):
            try:
                orchestrator.complete("s", "u", {"claude_api_key": "k", "ai_engine": motore,
                                                 "lm_url": "http://127.0.0.1:1234/v1"},
                                      scopo="ignoto")
                raise AssertionError("atteso ValueError")
            except ValueError:
                pass
        # «modelli» non invia dati: invia() lo rifiuta
        try:
            uscita.invia("modelli", payload, "k")
            raise AssertionError("atteso ValueError")
        except ValueError:
            pass


# ── Controllo in uscita ─────────────────────────────────────────────────────
def _mascherato(testo, diz=(CANARINO,)):
    anon = Anonymizer()
    (out,), _ = anon.mask_batch([testo], list(diz))
    return anon, out


def test_controllo_blocca_canarino_rimasto():
    anon, m = _mascherato(f"Il budget di {CANARINO} per il 2026")
    assert CANARINO not in m
    # bug simulato: il testo originale finisce comunque nel payload (minuscolo)
    payload = {"model": "m", "system": "Sei un assistente.",
               "messages": [{"role": "user", "content": m},
                            {"role": "assistant", "content": "ok"},
                            {"role": "user", "content": [
                                {"type": "text", "text": "e " + CANARINO.lower() + "?"}]}]}
    rete = _Rete()
    with patch("requests.post", rete):
        try:
            uscita.invia("chat", payload, "k", controllo=anon)
            raise AssertionError("attesa UscitaBloccata")
        except uscita.UscitaBloccata as e:
            assert str(e) == uscita.MSG_BLOCCATA
            assert "Zefiro" not in str(e) and "Canarino" not in str(e)
            assert e.tipo == "NOME" and e.conteggio == 1
            assert not isinstance(e, ValueError)      # docgen non la ritenta
    assert rete.chiamate == []


def test_controllo_lascia_passare_payload_mascherato():
    anon, m = _mascherato(f"Il budget di {CANARINO} e di nome.cognome@example.com")
    payload = {"model": "m", "system": "Sistema. " + orchestrator.ISTRUZIONE_SEGNAPOSTO,
               "messages": [{"role": "user", "content": m}]}
    rete = _Rete()
    with patch("requests.post", rete):
        r = uscita.invia("chat", payload, "k", controllo=anon)
    assert r.status_code == 200 and len(rete.claude()) == 1
    url, inviato = rete.chiamate[0]
    assert url == "https://api.anthropic.com/v1/messages"
    assert CANARINO not in json.dumps(inviato)


def test_controllo_parola_intera_segnaposto_e_fisso():
    # «Nome» nel dizionario: [NOME_001] non è una fuga; «Canarinone» nemmeno
    anon, m = _mascherato(f"Nome e {CANARINO}", diz=("Nome", CANARINO))
    assert "[NOME_" in m
    payload = {"model": "m", "system": "Esempio: Zefiro Canarinone",
               "messages": [{"role": "user", "content": m}]}
    with patch("requests.post", _Rete()):
        uscita.invia("docgen", payload, "k", controllo=anon)
    # un esempio delle istruzioni fisse che coincide con un valore: non è una fuga…
    fisso = f"Esempio: di {CANARINO}"
    payload = {"model": "m", "system": fisso, "messages": [{"role": "user", "content": m}]}
    with patch("requests.post", _Rete()):
        uscita.invia("dynamics", payload, "k", controllo=anon, fisso=(fisso,))
    # …ma una SECONDA occorrenza nei dati sì
    payload["messages"][0]["content"] = m + " " + CANARINO
    with patch("requests.post", _vietato):
        try:
            uscita.invia("dynamics", payload, "k", controllo=anon, fisso=(fisso,))
            raise AssertionError("attesa UscitaBloccata")
        except uscita.UscitaBloccata:
            pass


def test_controllo_ignorato_per_scopi_in_chiaro():
    anon, _ = _mascherato(f"di {CANARINO}")
    payload = {"model": "m", "messages": [{"role": "user", "content": CANARINO}]}
    rete = _Rete()
    with patch("requests.post", rete):
        uscita.invia("powerbi", payload, "k", controllo=anon)
    assert len(rete.claude()) == 1


def test_complete_passa_il_controllo():
    anon, m = _mascherato(f"di {CANARINO}")
    with patch("requests.post", _vietato):
        try:
            orchestrator.complete("s", m + " " + CANARINO, {"claude_api_key": "k"},
                                  scopo="cowork", controllo=anon)
            raise AssertionError("attesa UscitaBloccata")
        except uscita.UscitaBloccata:
            pass


class _AnonDifettoso(Anonymizer):
    """Bug simulato: maschera (la mappa si riempie) ma restituisce i testi
    ORIGINALI. Il controllo in uscita deve fermare la richiesta."""

    def mask_batch(self, segmenti, dizionario=None, presidio=True):
        _, esito = super().mask_batch(segmenti, dizionario, presidio)
        return list(segmenti), esito


def test_chat_bloccata_errore_chiaro_nulla_parte():
    with patch.object(orchestrator, "Anonymizer", _AnonDifettoso), \
            patch("requests.post", _vietato):
        eventi = "".join(orchestrator.stream_reply(
            [{"role": "user", "content": f"chi è {CANARINO}?"}],
            {"claude_api_key": "k", "ai_engine": "claude"}, [CANARINO]))
    errori = [json.loads(r[6:]) for r in eventi.split("\n\n") if r.startswith("data: ")]
    errori = [e for e in errori if e.get("type") == "error"]
    assert errori and errori[0]["text"] == uscita.MSG_BLOCCATA
    assert "Presidio" not in eventi            # nulla è partito: nessun avviso


def test_mappa_condivisa_propaga_presidio_nei_lotti_successivi():
    """Attività e planner Dynamics usano UNA istanza su più lotti: un nome
    trovato da Presidio al primo lotto resta mascherato anche quando il NER
    non lo rivede o è giù. Senza, il controllo in uscita scatterebbe."""
    from app import presidio_client

    def _finto(testi):
        return [[(m.start(), m.end(), "PERSON", 0.9)
                 for m in re.finditer("Giovanni Verdi", t)] for t in testi]
    anon = Anonymizer()
    with patch.object(presidio_client, "analizza", _finto):
        (a,), _ = anon.mask_batch(["ordine di Giovanni Verdi"], [])
    assert "Giovanni Verdi" not in a
    (b,), esito = anon.mask_batch(["di nuovo GIOVANNI VERDI e Giovanni Verdi"], [],
                                  presidio=False)
    assert "verdi" not in b.lower() and esito["totale"] >= 1
    payload = {"model": "m", "messages": [{"role": "user", "content": b}]}
    with patch("requests.post", _Rete()):
        uscita.invia("cowork", payload, "k", controllo=anon)


def test_dynamics_bloccata_risale_fino_alla_chat():
    """_ask_ai non invia, annota il blocco e non invia più nulla; connectors
    rilancia l'errore invece di restituire un risultato vuoto."""
    from app import connectors
    from app.engines.dynamics_search import DynamicsSearch
    ds = DynamicsSearch({"ai_engine": "claude", "claude_api_key": "k",
                         "claude_model": "m", "anon_names": [CANARINO]})
    ds._maschera([f"cliente {CANARINO}"])
    with patch("requests.post", _vietato):
        assert ds._ask_ai("sys", f"cliente {CANARINO}") == ""
        assert isinstance(ds.uscita_bloccata, uscita.UscitaBloccata)
        assert ds._ask_ai("sys", "nulla di riservato") == ""     # più nessun invio

    class _Finta:
        def __init__(self, cfg):
            self.uscita_bloccata = uscita.UscitaBloccata("NOME", 1)

        def search(self, *a, **k):
            return "risultato parziale"
    with patch.object(connectors.dynamics_search, "DynamicsSearch", _Finta), \
            patch.object(connectors, "is_connected", lambda u, c: True), \
            patch.object(connectors, "ms_cfg", lambda c: {"client_id": "x", "tenant_id": "y",
                                                         "resource_url": "https://d365"}):
        try:
            connectors.search_with_links("u@test", "dynamics", "domanda",
                                         ai_settings={"ai_engine": "claude"})
            raise AssertionError("attesa UscitaBloccata")
        except uscita.UscitaBloccata:
            pass


def test_log_una_riga_senza_testo():
    import contextlib
    import io
    err = io.StringIO()
    anon, m = _mascherato(f"di {CANARINO}")
    with patch("requests.post", _Rete()), contextlib.redirect_stderr(err):
        uscita.invia("chat", {"model": "m", "messages": [{"role": "user", "content": m}]},
                     "chiave-segreta", controllo=anon)
    righe = [r for r in err.getvalue().splitlines() if r.startswith("[uscita]")]
    assert len(righe) == 1
    assert "scopo=chat" in righe[0] and "politica=protetto" in righe[0]
    assert "esito=200" in righe[0] and "ms=" in righe[0] and f"caratteri={len(m)}" in righe[0]
    assert "[NOME_" not in err.getvalue() and "chiave-segreta" not in err.getvalue()


# ── Riscrittura della query ─────────────────────────────────────────────────
def test_riscrittura_mascherata_e_ripristinata():
    store.set_setting("anon_dictionary", CANARINO, secret=True)
    try:
        rete = _Rete(testo="[NOME_001] budget 2026 budget plan")
        with patch("requests.post", rete):
            rq = app_main._riscrivi_query(f"qual è il budget di {CANARINO}?",
                                          {"claude_api_key": "k", "claude_model": "h"})
        assert len(rete.claude()) == 1
        inviato = json.dumps(rete.chiamate[0][1], ensure_ascii=False)
        assert CANARINO not in inviato and "[NOME_001]" in inviato
        assert rq == f"{CANARINO} budget 2026 budget plan"
    finally:
        store.set_setting("anon_dictionary", "", secret=True)


# ── LM Studio ───────────────────────────────────────────────────────────────
_LM = {"ai_engine": "lmstudio", "claude_api_key": "k",
       "lm_url": "http://lm-interno:1234/v1/chat/completions", "lm_model": "locale"}


def test_complete_lmstudio_zero_chiamate_anthropic():
    rete = _Rete(testo='{"title": "T", "sections": []}')
    with patch("requests.post", rete), patch("socket.getaddrinfo", _dns("10.1.40.7")):
        out = orchestrator.complete("sys", "testo", dict(_LM), scopo="docedit")
        spec = docgen.build_spec("docx", "crea un word", "", dict(_LM, _anon_names=[]))
    assert out.startswith("{") and spec["title"] == "T"
    assert rete.chiamate and rete.claude() == []
    assert all(u == _LM["lm_url"] for u, _ in rete.chiamate)
    p = rete.chiamate[0][1]
    assert p["stream"] is False and p["messages"][0] == {"role": "system", "content": "sys"}


def test_complete_lmstudio_guasto_errore_senza_ripiego():
    rete = _Rete(stato=500)
    with patch("requests.post", rete), patch("socket.getaddrinfo", _dns("192.168.1.9")):
        try:
            orchestrator.complete("s", "u", dict(_LM), scopo="riscrittura", timeout=5)
            raise AssertionError("atteso RuntimeError")
        except RuntimeError as e:
            assert "LM Studio" in str(e)
    assert rete.claude() == [] and len(rete.chiamate) == 1


def test_lm_url_pubblico_rifiutato_privato_accettato():
    v = uscita.verifica_lm_url
    with patch("socket.getaddrinfo", _dns("93.184.216.34")):
        assert "pubblico" in v("http://lm.example.com:1234/v1/chat/completions")
    with patch("socket.getaddrinfo", _dns(["10.0.0.5", "8.8.8.8"])):
        assert v("http://misto:1234/v1") != ""          # basta un indirizzo pubblico
    for ip in ("10.2.3.4", "172.20.0.3", "192.168.0.10", "127.0.0.1", "fd12::5", "::1"):
        with patch("socket.getaddrinfo", _dns(ip)):
            assert v("http://lm-interno:1234/v1") == "", ip
    with patch("socket.getaddrinfo", _dns("172.32.0.1")):
        assert v("http://fuori-rete:1234/v1") != ""      # fuori da 172.16/12
    with patch("socket.getaddrinfo", _dns("169.254.1.1")):
        assert v("http://link-local:1234/v1") != ""
    with patch("socket.getaddrinfo", side_effect=socket.gaierror("nessun host")):
        assert "non si risolve" in v("http://inesistente:1234/v1")
    with patch("socket.getaddrinfo", _vietato):        # IP letterali: niente DNS
        assert v("http://127.0.0.1:1234/v1") == ""
        assert v("http://[::1]:1234/v1") == ""
        assert v("http://8.8.8.8/v1") != ""
        assert v("ftp://10.0.0.1/x") != "" and v("") != ""


def test_lmstudio_pubblico_nessun_invio():
    pub = dict(_LM, lm_url="http://lm.example.com/v1/chat/completions")
    with patch("requests.post", _vietato), patch("socket.getaddrinfo", _dns("93.184.216.34")):
        try:
            orchestrator.complete("s", "u", pub, scopo="docgen")
            raise AssertionError("atteso EndpointNonPrivato")
        except uscita.EndpointNonPrivato:
            pass
        eventi = "".join(orchestrator.stream_reply(
            [{"role": "user", "content": "ciao"}], pub, []))
    assert '"type": "error"' in eventi and "pubblico" in eventi


def test_admin_rifiuta_lm_url_pubblico():
    store.create_user("adm-uscita@test", auth.hash_password("Password123!"), "IT",
                      is_admin=True)
    c = TestClient(app_main.app)
    c.post("/login", data={"username": "adm-uscita@test", "password": "Password123!"},
           follow_redirects=False)
    store.set_setting("lm_url", "http://127.0.0.1:1234/v1/chat/completions")
    with patch("socket.getaddrinfo", _dns("93.184.216.34")):
        r = c.post("/admin", data={"lm_url": "http://lm.example.com/v1/chat/completions"},
                   follow_redirects=False)
    assert r.status_code == 303 and "lm_err=" in r.headers["location"]
    assert store.get_setting("lm_url", "") == "http://127.0.0.1:1234/v1/chat/completions"
    pagina = c.get(r.headers["location"]).text
    assert "NON salvate" in pagina and "pubblico" in pagina
    with patch("socket.getaddrinfo", _dns("10.1.40.3")):
        r = c.post("/admin", data={"lm_url": "http://lm-interno:1234/v1/chat/completions"},
                   follow_redirects=False)
    assert r.headers["location"] == "/admin?saved=1"
    assert store.get_setting("lm_url", "") == "http://lm-interno:1234/v1/chat/completions"


if __name__ == "__main__":
    import inspect

    fns = [f for n, f in sorted(globals().items())
           if n.startswith("test_") and inspect.isfunction(f)]
    failed = 0
    for f in fns:
        try:
            f()
            print(f"  PASS  {f.__name__}")
        except Exception as e:
            failed += 1
            print(f"  FAIL  {f.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(fns)-failed}/{len(fns)} test superati.")
    sys.exit(1 if failed else 0)

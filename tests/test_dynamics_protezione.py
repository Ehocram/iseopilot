"""
Test della PROTEZIONE DEI DATI nel planner Dynamics 365 verso Claude:
domanda, campioni di righe ERP, aggregazioni, codici risolti da nomi e note di
dominio partono mascherati; il piano JSON di Claude si interpreta e poi si
ripristina foglia per foglia PRIMA delle query OData; un valore con apostrofo
ripristinato in un filtro resta OData valido; con LM Studio nulla cambia;
Presidio giù non blocca la ricerca. Nessun test va in rete: requests.post
(Claude, LM Studio) e requests.get (Dynamics) sono intercettati, Presidio è finto.
Esecuzione:
    PYTHONPATH=. APP_DATA_DIR=./data_test python3.11 tests/test_dynamics_protezione.py
"""
import json
import os
import re
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.parse import unquote

os.environ.setdefault("APP_DATA_DIR", "./data_test")
from cryptography.fernet import Fernet
os.environ.setdefault("APP_SECRET_KEY", Fernet.generate_key().decode())

from app import store, presidio_client, connectors  # noqa: E402  (redirige i log Dynamics)
from app.engines import dynamics_search as dmod      # noqa: E402

# Questi test verificano il meccanismo dei NOMI DI PERSONA di Presidio, che in
# produzione è spento di default (anon_ner_persone): qui lo si accende.
from app import anonymizer as _anon_mod  # noqa: E402
_ner_persone_reale = _anon_mod.ner_persone_attivo
_anon_mod.ner_persone_attivo = lambda: True
from app.engines.dynamics_search import DynamicsSearch  # noqa: E402

store.init_db()
dmod._DBG_LOG = Path(tempfile.gettempdir()) / "test_dynamics_protezione.log"

RESOURCE = "https://dyn.example.test"
DIZ = ["Progetto Fenice", "Laura Bianchi", "Marco D'Angelo"]
# Canarini: NON devono mai comparire nei payload inviati a Claude.
CANARINI = ["Progetto Fenice", "Laura Bianchi", "laura.bianchi@cliente.example",
            "Giovanni Verdi", "Verdi", "D'Angelo", "D''Angelo"]
# Matricola: valore ordinario nei campioni finché non è legata a un nome; dal
# passo in cui viene RISOLTA da un nome non deve più comparire in chiaro.
MATRICOLA = "EMP0777"

CATALOGO = {
    "SalesOrderHeaders": {"string": ["SalesOrderNumber", "CustomerName", "OrderTakerPersonnelNumber",
                                     "ContactEmail"], "date": ["OrderDate"]},
    "Workers": {"string": ["Name", "PersonnelNumber"], "date": []},
}
RIGHE_ORDINI = [
    {"@odata.etag": "x", "SalesOrderNumber": "SO-1", "CustomerName": "Laura Bianchi",
     "OrderTakerPersonnelNumber": "EMP0777", "ContactEmail": "laura.bianchi@cliente.example",
     "OrderDate": "2026-10-01"},
    {"SalesOrderNumber": "SO-2", "CustomerName": "Progetto Fenice",
     "OrderTakerPersonnelNumber": "EMP0777", "ContactEmail": "", "OrderDate": "2026-10-02"},
]
RIGHE_WORKERS = [{"Name": "Giovanni Verdi", "PersonnelNumber": "EMP0777"}]


# ── Presidio finto: trova «Giovanni Verdi» come PERSON ──────────────────────
def _presidio_finto(testi):
    out = []
    for t in testi:
        out.append([(m.start(), m.end(), "PERSON", 0.9)
                    for m in re.finditer(r"Giovanni Verdi", t or "")])
    return out


def _presidio_giu(testi):
    raise presidio_client.PresidioNonDisponibile("timeout")


def _presidio_vietato(testi):
    raise AssertionError("Presidio chiamato con LM Studio")


# ── Rete finta ──────────────────────────────────────────────────────────────
class _Claude:
    """requests.post intercettato. `piano(system, user)` decide la risposta."""

    def __init__(self, piano):
        self.piano = piano
        self.payload = []

    def __call__(self, url, headers=None, json=None, timeout=None, **kw):
        self.payload.append((url, json))
        sistema = json.get("system") or ""
        if "messages" in json and json["messages"] and json["messages"][0]["role"] == "system":
            sistema = json["messages"][0]["content"]
        utente = json["messages"][-1]["content"]
        testo = self.piano(sistema, utente)
        r = MagicMock(status_code=200)
        if "anthropic.com" in url:
            r.json.return_value = {"content": [{"type": "text", "text": testo}]}
        else:
            r.json.return_value = {"choices": [{"message": {"content": testo}}]}
        return r

    def testo_inviato(self) -> str:
        return "\n".join(_json.dumps(p, ensure_ascii=False) for _, p in self.payload)


_json = json


class _Dynamics:
    """requests.get intercettato: registra le URL OData (decodificate)."""

    def __init__(self):
        self.url = []

    def __call__(self, url, headers=None, timeout=None, **kw):
        u = unquote(url)
        self.url.append(u)
        r = MagicMock(status_code=200)
        if "/Workers" in u:
            rows = RIGHE_WORKERS
        elif "/SalesOrderHeaders" in u:
            rows = RIGHE_ORDINI
        else:
            rows = []
        r.json.return_value = {"value": rows}
        r.text = str(len(rows))
        return r


def _ds(engine="claude", diz=DIZ, **extra):
    cfg = {"ai_engine": engine, "claude_api_key": "k", "claude_model": "claude-test",
           "lm_url": "http://lm.local/v1/chat/completions", "lm_model": "locale",
           "dyn_resource_url": RESOURCE, "anon_names": list(diz)}
    cfg.update(extra)
    ds = DynamicsSearch(cfg)
    ds.tm.get_access_token = lambda: "tok"
    ds.load_catalog = lambda: CATALOGO
    ds._semantic_candidates = lambda q, cat, top_k=12: []
    ds._relations_summary = lambda ents, cat=None: ""
    ds._entity_schema_extra = lambda e: ""
    ds._write_generic_html = lambda *a, **k: ""
    ds._domain_hints = lambda max_chars=1500: "Regola: i clienti strategici sono Progetto Fenice."
    return ds


def _piano_agentico(sistema, utente):
    """Claude finto del planner a passi: query -> aggrega -> risolvi -> concludi."""
    if "parole chiave INGLESI" in sistema:
        return "sales order worker"
    passo = int(re.search(r"Passo (\d+)/", utente).group(1))
    if passo == 1:
        return json.dumps({"azione": "query", "entita": "SalesOrderHeaders", "filtro": "",
                           "campi": ["SalesOrderNumber", "CustomerName", "ContactEmail"],
                           "motivo": "guardo i dati"})
    if passo == 2:
        return json.dumps({"azione": "aggrega", "entita": "SalesOrderHeaders",
                           "campo": "CustomerName", "filtro": "", "motivo": "clienti"})
    if passo == 3:
        m = re.search(r"DOMANDA UTENTE:\n.*?(\[PERSONA_\d{3}\])", utente, re.S)
        persona = m.group(1) if m else "Giovanni Verdi"
        return json.dumps({"azione": "risolvi", "entita_lookup": "Workers", "campo_cerca": "Name",
                           "valore_cerca": persona, "campo_codice": "PersonnelNumber",
                           "motivo": "matricola"})
    codice = re.search(r"codici PersonnelNumber = \['(\[[A-Z]+_\d{3}\])'\]", utente).group(1)
    cliente = re.search(r"(\[NOME_\d{3}\])=", utente).group(1)
    m = re.search(r"(\[PERSONA_\d{3}\])", utente)
    persona = m.group(1) if m else "?"
    return json.dumps({"azione": "concludi", "entita_finale": "SalesOrderHeaders",
                       "filtro": f"OrderTakerPersonnelNumber eq '{codice}' and "
                                 f"CustomerName ne '{cliente}'",
                       "campi": ["SalesOrderNumber", "CustomerName"],
                       "spiegazione": f"Ordini presi da {persona}"})


DOMANDA = ("ordini di Giovanni Verdi per Progetto Fenice, "
           "referente laura.bianchi@cliente.example")


# ════════════════════════════════════════════════════════════
def test_agentico_canarini_assenti_e_piano_ripristinato_prima_di_odata():
    ds = _ds()
    claude, dyn = _Claude(_piano_agentico), _Dynamics()
    with patch.object(presidio_client, "analizza", _presidio_finto), \
            patch("requests.post", claude), patch("requests.get", dyn):
        out = ds.dynamic_query(DOMANDA)
    inviato = claude.testo_inviato()
    assert claude.payload and all("anthropic.com" in u for u, _ in claude.payload)
    for c in CANARINI:
        assert c not in inviato, f"canarino in chiaro verso Claude: {c!r}"
    # la matricola risolta dal nome sparisce dal passo successivo, anche dal
    # campione del passo 1 rimasto nella cronologia
    agent_payload = [p for _, p in claude.payload if "A PASSI" in p["system"]]
    assert MATRICOLA not in json.dumps(agent_payload[3], ensure_ascii=False)
    assert MATRICOLA in json.dumps(agent_payload[1], ensure_ascii=False)  # prima: campo qualunque
    # i segnaposto ci sono, e l'istruzione fissa sui codici è nel system
    assert "[PERSONA_001]" in inviato and "[NOME_" in inviato and "[EMAIL_" in inviato
    assert all("riportali identici" in p["system"] for _, p in claude.payload)
    # il catalogo (metadati) resta invariato
    agent = [p for _, p in claude.payload if "A PASSI" in p["system"]]
    assert len(agent) == 4
    assert "Campi testo: SalesOrderNumber, CustomerName, OrderTakerPersonnelNumber" \
        in agent[0]["messages"][0]["content"]
    # risolvi: il nome è tornato PURO (escape del codice esistente) nella query OData
    assert any("contains(Name,'Giovanni') and contains(Name,'Verdi')" in u for u in dyn.url)
    # concludi: segnaposto ripristinati PRIMA della query OData
    finale = [u for u in dyn.url if "OrderTakerPersonnelNumber eq" in u]
    assert finale, dyn.url
    assert "OrderTakerPersonnelNumber eq 'EMP0777' and CustomerName ne 'Laura Bianchi'" in finale[-1]
    assert "[" not in finale[-1].split("$filter=")[1]
    # la risposta della ricerca (contesto locale) ha i valori veri
    assert "Ordini presi da Giovanni Verdi" in out and "SO-1" in out
    assert ds.esito_protezione["presidio_ok"] is True
    assert ds.presidio_non_disponibile is False


def test_una_mappa_per_ricerca_segnaposto_coerenti_tra_i_passi():
    ds = _ds()
    claude, dyn = _Claude(_piano_agentico), _Dynamics()
    with patch.object(presidio_client, "analizza", _presidio_finto), \
            patch("requests.post", claude), patch("requests.get", dyn):
        ds.dynamic_query(DOMANDA)
    tok = {}
    for _, p in claude.payload:
        testo = p["messages"][-1]["content"]
        for nome in ("Progetto Fenice",):
            assert nome not in testo
        m = re.search(r"DOMANDA UTENTE:\n(.*?)\n", testo)
        if m:
            tok.setdefault("domanda", set()).add(m.group(1))
    # la domanda mascherata è identica a ogni passo (UNA istanza, UNA mappa)
    assert len(tok["domanda"]) == 1
    # nuova ricerca = nuova istanza = nuova mappa
    assert _ds()._anon is None and ds._anon is not None


def test_dangelo_nel_filtro_resta_odata_valido():
    ds = _ds()

    def piano(sistema, utente):
        if "parole chiave INGLESI" in sistema:
            return "sales order"
        nome = re.search(r"(\[NOME_\d{3}\])", utente).group(1)
        return json.dumps({"azione": "concludi", "entita_finale": "SalesOrderHeaders",
                           "filtro": f"CustomerName eq '{nome}' or contains(CustomerName,'{nome}')",
                           "campi": ["SalesOrderNumber"], "spiegazione": "ok"})
    claude, dyn = _Claude(piano), _Dynamics()
    with patch.object(presidio_client, "analizza", _presidio_finto), \
            patch("requests.post", claude), patch("requests.get", dyn):
        ds.dynamic_query("ordini del cliente Marco D'Angelo")
    assert "D'Angelo" not in claude.testo_inviato()
    filtri = [u.split("$filter=")[1].split("&$top")[0] for u in dyn.url if "$filter=" in u]
    atteso = "CustomerName eq 'Marco D''Angelo' or contains(CustomerName,'Marco D''Angelo')"
    assert atteso in filtri, filtri
    # il controllo di sola lettura accetta il filtro ripristinato
    assert DynamicsSearch._is_readonly_filter(atteso)


def test_ripristino_odata_unitario():
    ds = _ds()
    with patch.object(presidio_client, "analizza", _presidio_finto):
        (m,) = ds._maschera(["cliente Marco D'Angelo e Giovanni Verdi"])
    nome = re.search(r"\[NOME_\d{3}\]", m).group(0)
    pers = re.search(r"\[PERSONA_\d{3}\]", m).group(0)
    piano = {"join": {"entita_a": "A", "filtro_a": f"Name eq '{nome}'",
                      "spiegazione": f"per {nome}"},
             "risolvi": {"valore_cerca": nome, "filtro_extra": f"X eq {pers} and Y eq '{nome}'''"},
             "filtro": "Code eq '[NOME_999]'"}
    r = ds._ripristina_piano(piano)
    assert r["join"]["filtro_a"] == "Name eq 'Marco D''Angelo'"
    assert r["join"]["spiegazione"] == "per Marco D'Angelo"
    assert r["risolvi"]["valore_cerca"] == "Marco D'Angelo"            # puro
    # fuori da un letterale il valore diventa un letterale quotato;
    # l'apice raddoppiato finale ('') resta dentro il letterale
    assert r["risolvi"]["filtro_extra"] == \
        "X eq 'Giovanni Verdi' and Y eq 'Marco D''Angelo'''"
    assert r["filtro"] == "Code eq '[NOME_999]'"                       # mai emesso: invariato


def test_caso_d_colpo_singolo_valore_puro_e_filtro_extra_valido():
    ds = _ds(dyn_agentic=False)

    def piano(sistema, utente):
        if "parole chiave INGLESI" in sistema:
            return "sales order worker"
        nome = re.search(r"(\[NOME_\d{3}\])", utente).group(1)
        return json.dumps({"risolvi": {
            "entita_lookup": "Workers", "campo_cerca": "Name", "valore_cerca": nome,
            "campo_codice": "PersonnelNumber", "entita_target": "SalesOrderHeaders",
            "campo_target": "OrderTakerPersonnelNumber",
            "filtro_extra": f"CustomerName ne '{nome}'", "spiegazione": "ok"}})
    claude, dyn = _Claude(piano), _Dynamics()
    with patch.object(presidio_client, "analizza", _presidio_finto), \
            patch("requests.post", claude), patch("requests.get", dyn):
        ds.dynamic_query("ordini presi da Marco D'Angelo")
    assert "D'Angelo" not in claude.testo_inviato()
    # valore_cerca puro -> l'escape esistente lo raddoppia una volta sola
    assert any("contains(Name,'D''Angelo')" in u for u in dyn.url), dyn.url
    assert not any("D''''Angelo" in u for u in dyn.url)
    assert any("and (CustomerName ne 'Marco D''Angelo')" in u for u in dyn.url), dyn.url


def test_traduzione_mascherata_e_ripristinata_come_testo():
    ds = _ds(reply_lang="English")

    def piano(sistema, utente):
        assert "traduttore" in sistema.lower()
        return utente.replace("orders of", "ordini di")
    claude = _Claude(piano)
    with patch.object(presidio_client, "analizza", _presidio_finto), \
            patch("requests.post", claude):
        out = ds._translate_to_italian("orders of Giovanni Verdi for Progetto Fenice")
    assert out == "ordini di Giovanni Verdi for Progetto Fenice"
    inviato = claude.testo_inviato()
    assert "Giovanni Verdi" not in inviato and "Progetto Fenice" not in inviato


def test_lmstudio_invariato():
    ds = _ds(engine="lmstudio")
    lm, dyn = _Claude(_piano_agentico), _Dynamics()
    # lm.local risolve a un indirizzo privato (DNS finto): uscita lo ammette
    import socket
    _dns = lambda host, porta, *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "",
                                          ("192.168.10.5", porta))]
    with patch.object(presidio_client, "analizza", _presidio_vietato), \
            patch("socket.getaddrinfo", _dns), \
            patch("requests.post", lm), patch("requests.get", dyn):
        out = ds.dynamic_query("ordini di Giovanni Verdi")
    assert lm.payload and all(u == "http://lm.local/v1/chat/completions" for u, _ in lm.payload)
    kw = lm.payload[0][1]["messages"]
    assert kw[1]["content"] == "Testo: ordini di Giovanni Verdi"         # in chiaro, come prima
    assert all("riportali identici" not in p["messages"][0]["content"] for _, p in lm.payload)
    # il campione è il taglio di sempre: JSON in chiaro, al massimo 600 caratteri
    passo2 = lm.payload[2][1]["messages"][1]["content"]
    camp = re.search(r"Campione: (.*)", passo2).group(1)
    atteso = json.dumps(ds._compact_obs(RIGHE_ORDINI), ensure_ascii=False)[:600]
    assert camp == atteso
    assert ds._anon is None and ds.esito_protezione["attiva"] is False


def test_presidio_giu_la_ricerca_va_avanti():
    ds = _ds()
    claude, dyn = _Claude(_piano_agentico), _Dynamics()
    log = Path(dmod._DBG_LOG)
    prima = log.read_text(encoding="utf-8") if log.exists() else ""
    with patch.object(presidio_client, "analizza", _presidio_giu), \
            patch("requests.post", claude), patch("requests.get", dyn):
        # senza Presidio «Giovanni Verdi» non è riconosciuto: il piano usa il
        # nome che trova nel testo (qui lo cerchiamo come PERSONA o in chiaro)
        def piano(sistema, utente):
            if "A PASSI" in sistema and "Passo 3/" in utente:
                return json.dumps({"azione": "risolvi", "entita_lookup": "Workers",
                                   "campo_cerca": "Name", "valore_cerca": "Giovanni Verdi",
                                   "campo_codice": "PersonnelNumber", "motivo": "m"})
            return _piano_agentico(sistema, utente)
        claude.piano = piano
        out = ds.dynamic_query(DOMANDA)
    assert out and "SO-1" in out                                   # non bloccata
    assert ds.presidio_non_disponibile is True
    assert ds.esito_protezione["presidio_ok"] is False
    inviato = claude.testo_inviato()
    for c in ("Progetto Fenice", "Laura Bianchi", "laura.bianchi@cliente.example"):
        assert c not in inviato, c                                 # dizionario e regex lavorano
    nuovo = log.read_text(encoding="utf-8")[len(prima):]
    righe = [r for r in nuovo.splitlines() if "Presidio non disponibile" in r]
    assert len(righe) == 1 and "timeout" in righe[0]               # una riga, senza testo
    assert "Giovanni" not in righe[0] and "Fenice" not in righe[0]


def test_connettore_passa_il_dizionario():
    cfg = {"client_id": "c", "tenant_id": "t", "resource_url": RESOURCE}
    assert connectors._dyn_full_cfg(cfg, {"_anon_names": ["X Y"]})["anon_names"] == ["X Y"]
    prima = store.get_setting("anon_dictionary", "")
    try:
        store.set_setting("anon_dictionary", "Alfa Beta\n\nGamma\n", secret=True)
        assert connectors._dyn_full_cfg(cfg, {"ai_engine": "claude"})["anon_names"] == \
            ["Alfa Beta", "Gamma"]
    finally:
        store.set_setting("anon_dictionary", prima, secret=True)


if __name__ == "__main__":
    import inspect
    import sys

    fns = [f for n, f in sorted(globals().items())
           if n.startswith("test_") and inspect.isfunction(f)]
    failed = 0
    for f in fns:
        try:
            f()
            print(f"  PASS  {f.__name__}")
        except Exception as e:
            import traceback
            failed += 1
            print(f"  FAIL  {f.__name__}: {type(e).__name__}: {e}")
            traceback.print_exc()
    print(f"\n{len(fns)-failed}/{len(fns)} test superati.")
    sys.exit(1 if failed else 0)

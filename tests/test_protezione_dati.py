"""
Test della PROTEZIONE DEI DATI verso Claude (Fase 0 + client Presidio):
fuga del dizionario chiusa, cronologia e system mascherati, generazione file,
interruttore ignorato, immagini, varianti del dizionario, Presidio (trovato,
propagato, giù, risposta non valida). Nessun test va in rete: requests.post è
intercettato e Presidio è finto.
Esecuzione:
    PYTHONPATH=. APP_DATA_DIR=./data_test python tests/test_protezione_dati.py
"""
import json
import os

os.environ.setdefault("APP_DATA_DIR", "./data_test")
from cryptography.fernet import Fernet
os.environ.setdefault("APP_SECRET_KEY", Fernet.generate_key().decode())

from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient
from app.main import app
from app import store, auth, presidio_client
from app import orchestrator as orch
from app.anonymizer import Anonymizer, regex_dizionario

store.init_db()

DIZ = ["Mario Rossi", "Laura Bianchi", "Progetto Fenice"]
AVVISO = "Riconoscimento automatico dei dati personali (Presidio) non disponibile"


def _mk_user(name, dept="IT", admin=False):
    store.create_user(name, auth.hash_password("Password123!"), dept, is_admin=admin)
    c = TestClient(app)
    c.post("/login", data={"username": name, "password": "Password123!"},
           follow_redirects=False)
    return c


def _claude_stream(*chunks):
    """Risposta SSE finta di Claude, spezzata nei chunk indicati."""
    righe = ['data: {"type":"message_start"}', '']
    for ch in chunks:
        righe += ['data: ' + json.dumps({"type": "content_block_delta", "index": 0,
                                         "delta": {"type": "text_delta", "text": ch}}), '']
    righe += ['data: {"type":"message_stop"}', '']
    fake = MagicMock()
    fake.status_code = 200
    fake.iter_lines.return_value = iter(righe)
    return fake


def _presidio_risposta(risultati, status=200):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = {"versione": "presidio-analyzer 2.2.364",
                           "modello": "it_core_news_md-3.8.0",
                           "risultati": risultati, "ms": 5}
    return r


class _Rete:
    """requests.post intercettato: Claude e Presidio finti, chiamate registrate."""

    def __init__(self, claude_chunks=("ok",), presidio=None):
        self.claude_chunks = claude_chunks
        self.presidio = presidio          # funzione(body) -> risposta finta, o eccezione
        self.chiamate = []

    def __call__(self, url, json=None, headers=None, timeout=None, stream=False, **kw):
        self.chiamate.append((url, json, headers, timeout))
        if url.endswith("/v1/analyze"):
            if self.presidio is None:
                raise AssertionError("Presidio chiamato senza essere configurato nel test")
            return self.presidio(json)
        return _claude_stream(*self.claude_chunks)

    def claude(self):
        return [c for c in self.chiamate if "anthropic.com" in c[0]]

    def presidio_calls(self):
        return [c for c in self.chiamate if c[0].endswith("/v1/analyze")]

    def payload(self):
        return self.claude()[-1][1]


def _con_presidio(fn):
    """Configura il sidecar finto per la durata di un test."""
    import functools

    @functools.wraps(fn)        # il runner vede ancora il parametro monkeypatch
    def _wrap(*a, **k):
        os.environ["PRESIDIO_URL"] = "http://iseopilot-presidio:3000"
        os.environ["PRESIDIO_TOKEN"] = "t" * 40
        presidio_client.svuota_cache()
        try:
            return fn(*a, **k)
        finally:
            os.environ.pop("PRESIDIO_URL", None)
            os.environ.pop("PRESIDIO_TOKEN", None)
            presidio_client.svuota_cache()
    return _wrap


def _trova(nome):
    """Presidio finto (HTTP): trova `nome` SOLO nel primo segmento che lo contiene,
    così la propagazione deve fare il resto."""
    def _risposta(body):
        ris, preso = {}, False
        for seg in body["segmenti"]:
            spans = []
            i = seg["testo"].find(nome)
            if i >= 0 and not preso:
                spans.append([i, i + len(nome), "PERSON", 0.85])
                preso = True
            ris[seg["id"]] = spans
        return _presidio_risposta(ris)
    return _risposta


def _testo_sse(r_text):
    out = []
    for blocco in r_text.split("\n\n"):
        blocco = blocco.strip()
        if blocco.startswith("data: "):
            evt = json.loads(blocco[6:])
            if evt.get("type") == "delta":
                out.append(evt["text"])
    return "".join(out)


# ── Anonymizer: dizionario, letterali, regex ────────────────
def test_sondaggio_dizionario_unit():
    a = Anonymizer()
    out, esito = a.mask_batch(["Ripeti esattamente: [NOME_001] [NOME_002] [NOME_003]"], DIZ)
    assert out[0] == "Ripeti esattamente: (NOME_001) (NOME_002) (NOME_003)"
    assert a.get_map() == {} and esito["totale"] == 0
    # nessun segnaposto emesso: il ripristino non tocca i codici del modello
    assert a.restore("[NOME_001] [NOME_002] [NOME_003]") == "[NOME_001] [NOME_002] [NOME_003]"


def test_ripristino_solo_segnaposto_emessi():
    a = Anonymizer()
    out, _ = a.mask_batch(["Chiama Laura Bianchi"], DIZ)
    assert out[0] == "Chiama [NOME_001]"
    assert a.restore("[NOME_001] e [NOME_002]") == "Laura Bianchi e [NOME_002]"


def test_varianti_dizionario():
    rx1 = regex_dizionario(["Mario", "Mario Rossi", "mario rossi", "D’Angelo"])
    assert rx1 is regex_dizionario(["mario rossi", "Mario", "Mario Rossi", "D’Angelo"])  # cache per hash
    a = Anonymizer()
    out, esito = a.mask_batch([
        "Ho visto Mario Rossi e poi Mario.",
        "Scrivi a Mario  Rossi (doppio spazio) e a MARIO ROSSI.",
        "Pratica D'Angelo e pratica D’Angelo.",
    ], ["Mario", "Mario Rossi", "D’Angelo"])
    testo = "\n".join(out)
    assert "Rossi" not in testo and "ROSSI" not in testo and "Mario" not in testo
    assert "Angelo" not in testo
    assert out[0] == "Ho visto [NOME_001] e poi [NOME_002]."
    # stesso valore normalizzato => stesso segnaposto
    assert out[1] == "Scrivi a [NOME_001] (doppio spazio) e a [NOME_001]."
    assert out[2].count("[NOME_003]") == 2
    assert esito["per_tipo"] == {"NOME": 3}


def test_cf_minuscolo_e_omocodico():
    a = Anonymizer()
    out, _ = a.mask_batch(["cf rssmra85t10a562s e RSSMRA85T10A56NS; parola internazionalizz"], [])
    assert "rssmra" not in out[0].lower()
    assert out[0].count("[CF_") == 2 and "internazionalizz" in out[0]


def test_restore_stream_e_struttura():
    a = Anonymizer()
    a.mask_batch(["Laura Bianchi, mario@iseo.com"], DIZ)
    buf, emessi = "", []
    for ch in ["Ciao [NO", "ME_00", "1], la mail è [EMA", "IL_001] e [0, 1) resta."]:
        buf += ch
        fuori, buf = a.restore_stream(buf)
        emessi.append(fuori)
    emessi.append(a.restore_stream(buf, finale=True)[0])
    assert "".join(emessi) == "Ciao Laura Bianchi, la mail è mario@iseo.com e [0, 1) resta."
    spec = {"title": "[NOME_001] \"capo\"", "rows": [["[EMAIL_001]", 3]], "n": 1}
    assert a.restore_struttura(spec) == {"title": "Laura Bianchi \"capo\"",
                                         "rows": [["mario@iseo.com", 3]], "n": 1}


# ── Presidio: propagazione e guasti (client finto) ──────────
def test_presidio_finto_propagazione_unit(monkeypatch):
    def finto(testi):
        # trova «Giovanni Verdi» solo nel PRIMO testo che lo contiene
        out, preso = [], False
        for t in testi:
            i = t.find("Giovanni Verdi")
            if i >= 0 and not preso:
                out.append([(i, i + 14, "PERSON", 0.85)])
                preso = True
            else:
                out.append([])
        return out
    monkeypatch.setattr(presidio_client, "analizza", finto)
    a = Anonymizer()
    out, esito = a.mask_batch(["Ho parlato con Giovanni Verdi.",
                               "Poi Giovanni Verdi ha scritto a Laura Bianchi.",
                               "Allegato: firma Giovanni Verdi"], DIZ)
    assert esito["presidio_ok"] is True
    assert all("Giovanni Verdi" not in t for t in out)
    assert all("[PERSONA_001]" in t for t in out)
    assert esito["per_tipo"] == {"PERSONA": 1, "NOME": 1} and esito["occorrenze"] == 4
    assert a.restore("Saluti a [PERSONA_001]") == "Saluti a Giovanni Verdi"


def test_presidio_scarta_intervalli_su_segnaposto(monkeypatch):
    # l'intervallo tocca [NOME_001] già presente: scartato
    monkeypatch.setattr(presidio_client, "analizza",
                        lambda testi: [[(0, 12, "PERSON", 0.9)] for _ in testi])
    a = Anonymizer()
    out, esito = a.mask_batch(["Laura Bianchi arriva"], DIZ)
    assert out[0] == "[NOME_001] arriva" and esito["presidio_ok"]


@_con_presidio
def test_presidio_client_cache_segmenti_e_token():
    rete = _Rete(presidio=_trova("Giovanni Verdi"))
    testo = "Primo paragrafo senza nomi.\n\nSecondo: Giovanni Verdi firma."
    with patch.object(presidio_client.requests, "post", side_effect=rete):
        r1 = presidio_client.analizza([testo])
        r2 = presidio_client.analizza([testo])          # tutto in cache
    assert len(rete.presidio_calls()) == 1
    url, body, headers, timeout = rete.presidio_calls()[0]
    assert headers["X-Presidio-Token"] == "t" * 40 and 3.0 <= timeout <= 20.0
    assert len(body["segmenti"]) == 2                    # un segmento per paragrafo
    a, b, ent, _ = r1[0][0]
    assert testo[a:b] == "Giovanni Verdi" and ent == "PERSON" and r1 == r2


@_con_presidio
def test_presidio_anomalie_sono_guasti():
    casi = [
        lambda body: _presidio_risposta({s["id"]: [[0, 10_000, "PERSON", 0.9]] for s in body["segmenti"]}),
        lambda body: _presidio_risposta({s["id"]: [[0, 3, "LOCATION", 0.9]] for s in body["segmenti"]}),
        lambda body: _presidio_risposta({}, status=503),
        lambda body: _presidio_risposta({}),                      # segmenti mancanti
    ]

    def _rete_giu(body):
        import requests
        raise requests.ConnectionError("giu")
    casi.append(_rete_giu)
    for caso in casi:
        presidio_client.svuota_cache()
        rete = _Rete(presidio=caso)
        with patch.object(presidio_client.requests, "post", side_effect=rete):
            a = Anonymizer()
            out, esito = a.mask_batch(["Laura Bianchi scrive a l.b@iseo.com"], DIZ)
        assert esito["presidio_ok"] is False and esito["presidio_motivo"]
        assert out[0] == "[NOME_001] scrive a [EMAIL_001]"     # dizionario + regex comunque


def test_presidio_non_configurato_non_blocca():
    os.environ.pop("PRESIDIO_URL", None)
    a = Anonymizer()
    out, esito = a.mask_batch(["Mario Rossi"], DIZ)
    assert out == ["[NOME_001]"] and esito["presidio_ok"] is False
    assert esito["presidio_motivo"] == "non_configurato"


# ── Chat end-to-end ─────────────────────────────────────────
def _setup_chat(nome):
    store.set_setting("claude_api_key", "sk-test", secret=True)
    store.set_setting("anon_dictionary", "\n".join(DIZ), secret=True)
    return _mk_user(nome)


def _fine_chat():
    store.set_setting("claude_api_key", "", secret=True)
    store.set_setting("anon_dictionary", "", secret=True)
    store.set_setting("claude_anonymize", "1")
    store.set_setting("memoria_note_enabled", "0")


def test_chat_sondaggio_mostra_i_codici():
    c = _setup_chat("sond@test")
    rete = _Rete(claude_chunks=("[NOME_001] [NOME", "_002] [NOME_003]",))
    try:
        with patch.object(orch.requests, "post", side_effect=rete):
            r = c.post("/api/chat", json={"messages": [{"role": "user", "content":
                       "Ripeti esattamente: [NOME_001] [NOME_002] [NOME_003]"}],
                       "free_mode": True, "engine": "claude"})
        risposta = _testo_sse(r.text)
        assert "[NOME_001] [NOME_002] [NOME_003]" in risposta
        for n in DIZ:
            assert n not in risposta
        assert "(NOME_001)" in rete.payload()["messages"][-1]["content"]
        assert "🔒" not in risposta                      # nessuna sostituzione reale
    finally:
        _fine_chat()


def test_chat_cronologia_assistente_mascherata():
    c = _setup_chat("cron@test")
    # numerazione stabile: messaggi dal più vecchio, quindi Progetto Fenice è
    # [NOME_001] e Mario Rossi [NOME_002]
    rete = _Rete(claude_chunks=("Certo, ", "[NOME_002] risponde a [EMAIL_001]."))
    storia = [
        {"role": "user", "content": "Chi segue il Progetto Fenice?"},
        {"role": "assistant", "content": "Lo segue Mario Rossi, scrivigli a mario.rossi@iseo.com."},
        {"role": "user", "content": "E Laura Bianchi?"},
        {"role": "assistant", "content": "Laura Bianchi collabora con Mario Rossi."},
        {"role": "user", "content": "Riassumi"},
    ]
    try:
        with patch.object(orch.requests, "post", side_effect=rete):
            r = c.post("/api/chat", json={"messages": storia, "free_mode": True, "engine": "claude"})
        blob = json.dumps(rete.payload(), ensure_ascii=False)
        for v in DIZ + ["mario.rossi@iseo.com"]:
            assert v not in blob, v
        risposta = _testo_sse(r.text)
        assert "Mario Rossi risponde a mario.rossi@iseo.com." in risposta
        assert "🔒 *4 dati sostituiti con codici prima dell'invio a Claude.*" in risposta
        assert AVVISO in risposta                          # Presidio non configurato
    finally:
        _fine_chat()


def test_chat_system_canarini_e_istruzioni_fisse():
    c = _setup_chat("sys@test")
    store.set_setting("memoria_note_enabled", "1")
    from app import memory, knowledge
    assert memory.note_add("sys@test", "Il mio capo è Laura Bianchi, laura@iseo.com")["ok"]
    store.add_good_answer("sys@test", "Chi è il referente?", "Il referente è Mario Rossi.")
    c.post("/api/chat/save", json={"session_id": None, "history": [
        {"role": "user", "content": "Parlami del Progetto Fenice"},
        {"role": "assistant", "content": "Il Progetto Fenice è riservato, IP 10.1.2.3."}]})
    with patch.object(knowledge, "extract_attachment_text",
                      return_value="Verbale: presenti Mario Rossi e Laura Bianchi, cf RSSMRA85T10A562S"):
        a = c.post("/api/attach", files=[("files", ("verbale.txt", b"x", "text/plain"))]).json()["attachments"][0]
    rete = _Rete()
    try:
        with patch.object(orch.requests, "post", side_effect=rete):
            c.post("/api/chat", json={"messages": [{"role": "user", "content": "sintetizza l'allegato"}],
                                      "free_mode": True, "engine": "claude",
                                      "attachments": [{"id": a["id"], "name": "verbale.txt"}]})
        system = rete.payload()["system"]
        for v in DIZ + ["laura@iseo.com", "RSSMRA85T10A562S", "10.1.2.3"]:
            assert v not in system, v
        assert "Verbale: presenti [NOME_" in system           # l'allegato c'è, mascherato
        # istruzioni fisse identiche, compresa quella sui segnaposto
        fisso = orch.build_system("Aziendale formale", "Italiano", "", free_mode=True)
        for parte in fisso.split("\n\n"):
            assert parte in system, parte[:60]
        assert orch.ISTRUZIONE_SEGNAPOSTO in system
    finally:
        _fine_chat()


def test_chat_interruttore_db_ignorato():
    c = _setup_chat("int@test")
    store.set_setting("claude_anonymize", "0")
    rete = _Rete()
    try:
        with patch.object(orch.requests, "post", side_effect=rete):
            c.post("/api/chat", json={"messages": [{"role": "user", "content":
                   "Mario Rossi, 192.168.1.10, m.rossi@iseo.com"}], "free_mode": True, "engine": "claude"})
        blob = json.dumps(rete.payload(), ensure_ascii=False)
        assert "Mario Rossi" not in blob and "192.168.1.10" not in blob and "m.rossi@iseo.com" not in blob
    finally:
        _fine_chat()


@_con_presidio
def test_chat_presidio_trova_e_propaga():
    c = _setup_chat("pres@test")
    rete = _Rete(claude_chunks=("Ho scritto a [PERSONA_001] e a [NOME_001].",),
                 presidio=_trova("Giovanni Verdi"))
    storia = [
        {"role": "user", "content": "Scrivi a Giovanni Verdi"},
        {"role": "assistant", "content": "Fatto, Giovanni Verdi ha ricevuto la mail."},
        {"role": "user", "content": "Avvisa anche Mario Rossi e ricorda Giovanni Verdi"},
    ]
    try:
        with patch.object(orch.requests, "post", side_effect=rete):
            r = c.post("/api/chat", json={"messages": storia, "free_mode": True, "engine": "claude"})
        assert len(rete.presidio_calls()) == 1               # una sola chiamata per richiesta
        msgs = rete.payload()["messages"]
        assert all("Giovanni Verdi" not in m["content"] for m in msgs)
        assert all("[PERSONA_001]" in m["content"] for m in msgs)   # propagazione
        risposta = _testo_sse(r.text)
        assert "Ho scritto a Giovanni Verdi e a Mario Rossi." in risposta
        assert AVVISO not in risposta
        assert "🔒 *2 dati sostituiti" in risposta
    finally:
        _fine_chat()


@_con_presidio
def test_chat_presidio_giu_invia_comunque_con_avviso():
    import requests

    def _giu(body):
        raise requests.ConnectionError("rifiutata")
    c = _setup_chat("giu@test")
    rete = _Rete(claude_chunks=("Risposta per [NOME_001].",), presidio=_giu)
    try:
        with patch.object(orch.requests, "post", side_effect=rete):
            r = c.post("/api/chat", json={"messages": [{"role": "user", "content":
                       "Mario Rossi e Giovanni Verdi, mail g@iseo.com"}], "free_mode": True, "engine": "claude"})
        assert len(rete.presidio_calls()) == 1 and len(rete.claude()) == 1
        blob = json.dumps(rete.payload(), ensure_ascii=False)
        assert "Mario Rossi" not in blob and "g@iseo.com" not in blob
        assert "Giovanni Verdi" in blob                     # senza Presidio resta in chiaro
        risposta = _testo_sse(r.text)
        assert "Risposta per Mario Rossi." in risposta
        assert "⚠️ *" + AVVISO in risposta
    finally:
        _fine_chat()


@_con_presidio
def test_chat_presidio_intervalli_fuori_testo_come_giu():
    c = _setup_chat("fuori@test")
    rete = _Rete(presidio=lambda body: _presidio_risposta(
        {s["id"]: [[0, len(s["testo"]) + 50, "PERSON", 0.9]] for s in body["segmenti"]}))
    try:
        with patch.object(orch.requests, "post", side_effect=rete):
            r = c.post("/api/chat", json={"messages": [{"role": "user", "content":
                       "Giovanni Verdi e Laura Bianchi"}], "free_mode": True, "engine": "claude"})
        assert len(rete.claude()) == 1
        assert "Laura Bianchi" not in json.dumps(rete.payload(), ensure_ascii=False)
        assert AVVISO in _testo_sse(r.text)
    finally:
        _fine_chat()


def test_note_ui_avviso_tolta_dalla_cronologia():
    c = _setup_chat("note@test")
    rete = _Rete()
    storia = [
        {"role": "user", "content": "prima"},
        {"role": "assistant", "content": "Risposta.\n\n🔒 *2 dati sostituiti con codici prima dell'invio a Claude.*"
                                         "\n\n⚠️ *" + AVVISO + ": sostituiti solo i nomi del dizionario e i dati tecnici.*"},
        {"role": "user", "content": "seconda"},
    ]
    try:
        with patch.object(orch.requests, "post", side_effect=rete):
            c.post("/api/chat", json={"messages": storia, "free_mode": True, "engine": "claude"})
        blob = json.dumps(rete.payload()["messages"], ensure_ascii=False)
        assert "Risposta." in blob and "🔒" not in blob and "⚠" not in blob
    finally:
        _fine_chat()


# ── Generazione file e Attività ─────────────────────────────
def test_docgen_libera_payload_senza_nome_file_con_nome():
    c = _setup_chat("gen@test")
    spec = {"title": "Profilo di [NOME_001]",
            "sections": [{"heading": "Chi è", "paragraphs": ["[NOME_001] guida il \"[NOME_002]\"."]}]}
    risposta = MagicMock()
    risposta.status_code = 200
    risposta.json.return_value = {"content": [{"type": "text", "text": json.dumps(spec)}]}
    inviati, salvati = [], {}

    def _post(url, json=None, headers=None, timeout=None, **kw):
        inviati.append(json)
        return risposta
    from app import connectors

    def _reg(uid, path, fname):
        salvati["path"], salvati["fname"] = path, fname
        return "tok-gen"
    try:
        with patch.object(orch.requests, "post", side_effect=_post), \
             patch.object(connectors, "register_download", side_effect=_reg):
            r = c.post("/api/chat", json={"messages": [{"role": "user", "content":
                       "creami un Word su Mario Rossi e il Progetto Fenice"}],
                       "free_mode": True, "engine": "claude"})
        assert inviati, r.text[:300]
        blob = json.dumps(inviati[-1], ensure_ascii=False)
        assert "Mario Rossi" not in blob and "Progetto Fenice" not in blob
        assert orch.ISTRUZIONE_SEGNAPOSTO in inviati[-1]["system"]
        from docx import Document
        testo = "\n".join(p.text for p in Document(salvati["path"]).paragraphs)
        assert "Mario Rossi guida il \"Progetto Fenice\"." in testo
        assert "[NOME_" not in testo and "Mario Rossi" in salvati["fname"]
        assert "🔒" in r.text and AVVISO in r.text
    finally:
        _fine_chat()


def test_cowork_protezione_sempre_attiva(monkeypatch):
    from app import cowork, knowledge as kn
    visti = []

    def finto_complete(system, user, settings, max_tokens=2500, timeout=120, **_kw):
        visti.append(user)
        return '{"azione": "rispondi", "testo": "Il referente è [NOME_001]."}'
    monkeypatch.setattr(cowork, "complete", finto_complete)
    monkeypatch.setattr(kn, "kb_list", lambda dept: [{"name": "Doc.pdf", "chunks": 1}])
    eventi = "".join(cowork.run("IT", "cw.prot@test", "chi è il referente di Mario Rossi?",
                                {"claude_api_key": "sk", "claude_anonymize": False}, DIZ))
    assert visti and all("Mario Rossi" not in u for u in visti)
    assert "Il referente è Mario Rossi." in _testo_sse(eventi)
    assert AVVISO in eventi


# ── Admin ───────────────────────────────────────────────────
def test_admin_senza_interruttore_dizionario_cifrato_e_audit():
    c = _mk_user("adm.prot@test", admin=True)
    html = c.get("/admin").text
    assert 'name="claude_anonymize"' not in html
    assert "Protezione dati verso Claude: sempre attiva" in html
    store.set_setting("anon_dictionary", "", secret=True)
    r = c.post("/admin", data={"anon_dictionary": "Mario Rossi\nLaura Bianchi\n",
                               "claude_anonymize": "0"}, follow_redirects=False)
    assert r.status_code == 303
    import sqlite3
    with sqlite3.connect(store.DB_PATH) as cx:
        val, secret = cx.execute("SELECT value, secret FROM app_settings WHERE key='anon_dictionary'").fetchone()
    assert secret == 1 and "Mario" not in val
    assert store.get_setting("anon_dictionary") == "Mario Rossi\nLaura Bianchi\n"
    righe = store.audit_query(username="adm.prot@test", action="anon_dizionario")
    assert righe and righe[0]["detail"] == "voci: 0 → 2"
    assert "Mario" not in json.dumps(righe, ensure_ascii=False)
    store.set_setting("anon_dictionary", "", secret=True)


def test_testi_ui_allineati():
    c = _mk_user("ui.prot@test")
    html = c.get("/").text
    assert "anonimizzati" not in html and "non passano dall'anonimizzazione" not in html
    assert "sostituiti con codici" in html
    login = TestClient(app).get("/login").text
    assert "anonimizzati" not in login and "riconosciuti da Presidio" in login


# ── Correzioni dopo la revisione ────────────────────────────
def _ner_finto(nomi):
    """Presidio finto con il comportamento del NER: trova un nome SOLO nella
    forma «Nome Cognome» con spazi e iniziali maiuscole (come it_core_news_md),
    non con TAB, «;», «_» o tutto in maiuscolo. Registra i testi ricevuti."""
    ricevuti = []

    def finto(testi):
        ricevuti.extend(testi)
        out = []
        for t in testi:
            spans = []
            for n in nomi:
                i = t.find(n)
                while i >= 0:
                    spans.append((i, i + len(n), "PERSON", 0.85))
                    i = t.find(n, i + 1)
            out.append(spans)
        return out
    return finto, ricevuti


def test_ner_vede_tabelle_ed_elenchi_in_maiuscolo(monkeypatch):
    from app.anonymizer import testo_per_ner
    t = "Giovanni\tVerdi\tAcquisti\nGALLI;FRANCESCA;HR | ISEO [TEL_001] CV_Paolo_Neri.pdf"
    n = testo_per_ner(t)
    assert len(n) == len(t)
    assert n == "Giovanni Verdi Acquisti\nGalli Francesca HR   ISEO [TEL_001] CV Paolo Neri.pdf"
    finto, ricevuti = _ner_finto(["Giovanni Verdi", "Galli Francesca", "Paolo Neri"])
    monkeypatch.setattr(presidio_client, "analizza", finto)
    a = Anonymizer()
    out, esito = a.mask_batch([
        "Nome\tCognome\tReparto\nGiovanni\tVerdi\tAcquisti\nGALLI;FRANCESCA;HR",
        "ELENCO: GIOVANNI VERDI; in minuscolo giovanni verdi",   # propagazione senza maiuscole
        "| Giovanni Verdi | Buyer |",
        "Allegato CV_Paolo_Neri.pdf",
    ], [])
    blob = "\n".join(out)
    for frammento in ("Giovanni", "Verdi", "VERDI", "giovanni", "GALLI", "Paolo", "Neri"):
        assert frammento not in blob, (frammento, out)
    assert "Acquisti" in blob and "Buyer" in blob                      # niente di più del necessario
    assert "Giovanni Verdi Acquisti" in ricevuti[0]                    # il NER ha visto la riga
    # ogni forma ha il suo codice e torna IDENTICA
    assert a.restore(out[0]) == "Nome\tCognome\tReparto\nGiovanni\tVerdi\tAcquisti\nGALLI;FRANCESCA;HR"
    assert a.restore(out[3]) == "Allegato CV_Paolo_Neri.pdf"
    assert esito["presidio_ok"]


def test_presidio_prima_del_dizionario_niente_cognomi_in_chiaro(monkeypatch):
    finto, ricevuti = _ner_finto(["Laura Bianchi Ferraro", "Giuseppe Rossi Mancini"])
    monkeypatch.setattr(presidio_client, "analizza", finto)
    a = Anonymizer()
    out, esito = a.mask_batch(["sostituisce Laura Bianchi Ferraro da lunedì",
                               "Giuseppe Rossi Mancini è il nuovo responsabile",
                               "Laura Bianchi resta in copia"], ["Laura Bianchi", "Rossi"])
    # Presidio ha visto i nomi interi (prima del dizionario)
    assert "Laura Bianchi Ferraro" in ricevuti[0] and "Giuseppe Rossi Mancini" in ricevuti[1]
    assert out[0] == "sostituisce [PERSONA_001] da lunedì"
    assert out[1] == "[PERSONA_002] è il nuovo responsabile"
    assert out[2] == "[NOME_001] resta in copia"
    assert a.restore(out[0]) == "sostituisce Laura Bianchi Ferraro da lunedì"


def test_dizionario_nomi_file_e_nomi_utente():
    a = Anonymizer()
    righe = ["- CV_Laura_Bianchi.pdf", "- LauraBianchi_contratto.pdf",
             "- Progetto_Fenice_budget.xlsx", "login laura_bianchi, account rossi2024",
             "Rossini e Bianchini restano"]
    out, _ = a.mask_batch(righe, ["Laura Bianchi", "Progetto Fenice", "Rossi"])
    blob = "\n".join(out)
    assert "Laura" not in blob and "laura" not in blob and "Fenice" not in blob
    assert "rossi2024" not in blob and "[NOME_" in out[3]
    assert out[4] == "Rossini e Bianchini restano"
    # il nome del file torna identico: l'Attività lo ritrova
    assert [a.restore(o) for o in out[:3]] == righe[:3]


def test_andata_e_ritorno_di_ogni_tipo():
    a = Anonymizer()
    campioni = {
        "IP": "10.1.40.3", "IPV6": "2001:db8:85a3:0:0:8a2e:370:7334",
        "EMAIL": "mario@iseo.com", "CVE": "CVE-2024-29018", "MAC": "00-1a-2b-3c-4d-5e",
        "CF": "RSSMRA85T10A562S", "PIVA": "IT01234567890", "TEL": "030 1234567",
        "HASH": "d41d8cd98f00b204e9800998ecf8427e",
        "TOKEN": "abcdefghijklmnopqrstuvwxyzABCDEFGHIJ_123", "HOST": "srv01.iseo.local",
    }
    assert set(campioni) == {t for t, _ in Anonymizer.PATTERNS}
    testo = " ; ".join(f"{k}: {v}" for k, v in campioni.items())
    out, esito = a.mask_batch([testo], [])
    for tipo, v in campioni.items():
        assert v not in out[0], (tipo, out[0])
        assert f"[{tipo}_001]" in out[0], (tipo, out[0])
    assert a.restore(out[0]) == testo
    # oltre 999 valori dello stesso tipo il contatore continua e si ripristina
    a._counters["TEL"] = 9999
    tok = a._emetti("TEL", "0301234567")
    assert tok == "[TEL_10000]" and a.restore(f"x {tok} y") == "x 0301234567 y"
    assert a.restore_stream("chiama [TEL_100")[1] == "[TEL_100"


def test_letterali_neutralizzati_solo_nella_forma_esatta():
    a = Anonymizer()
    codice = "x = riga[col_1] + riga[col_2]; Vendite[Anno_2024]; CALCULATE(SUM(Sales[Qty_1])); [nome_001]"
    out, _ = a.mask_batch([codice + " [NOME_001] [IPV6_002]"], [])
    assert out[0] == codice + " (NOME_001) (IPV6_002)"


def test_importi_non_sono_telefoni():
    a = Anonymizer()
    out, _ = a.mask_batch(["fatturato 2024 = 1250000, 2025 = 1380000; matricola 123456; "
                           "ordine 4500012345; data 03/05/2024",
                           "tel 030 1234567, +39 333 1234567, 3351234567, 02 12345678, 0039 06 1234 5678"], [])
    assert out[0] == ("fatturato 2024 = 1250000, 2025 = 1380000; matricola 123456; "
                      "ordine 4500012345; data 03/05/2024")
    assert all(n not in out[1] for n in ("1234567", "3351234567", "12345678", "5678")), out[1]
    assert out[1].count("[TEL_") == 5


def test_docgen_importi_in_chiaro_verso_claude(monkeypatch):
    from app import docgen
    visti = []

    def finto_complete(system, user, settings, max_tokens=4000, timeout=120, **_kw):
        visti.append(user)
        return json.dumps({"title": "Fatturato", "sheets": []})
    monkeypatch.setattr(orch, "complete", finto_complete)
    docgen.build_spec("xlsx", "Crea un Excel: fatturato 2024 = 1250000, 2025 = 1380000; "
                      "calcola la crescita percentuale", "", {"_anon_names": DIZ}, "")
    assert "1250000" in visti[0] and "1380000" in visti[0] and "[TEL_" not in visti[0]


@_con_presidio
def test_presidio_interruttore_dopo_timeout():
    import requests
    chiamate = []

    def _lento(url, json=None, headers=None, timeout=None, **kw):
        chiamate.append(headers)
        raise requests.Timeout("lento")
    with patch.object(presidio_client.requests, "post", side_effect=_lento):
        _, e1 = Anonymizer().mask_batch(["Giovanni Verdi"], [])
        _, e2 = Anonymizer().mask_batch(["Paolo Neri"], [])        # interruttore aperto
    assert len(chiamate) == 1 and e1["presidio_motivo"] == "timeout"
    assert e2["presidio_ok"] is False and e2["presidio_motivo"] == "sospeso"
    assert 3000 <= int(chiamate[0]["X-Presidio-Scadenza-Ms"]) <= 20000
    # passato il tempo (qui: azzerato) si ritenta
    presidio_client.svuota_cache()
    rete = _Rete(presidio=_trova("Paolo Neri"))
    with patch.object(presidio_client.requests, "post", side_effect=rete):
        out, e3 = Anonymizer().mask_batch(["Paolo Neri"], [])
    assert e3["presidio_ok"] and out == ["[PERSONA_001]"]


@_con_presidio
def test_cowork_non_richiama_presidio_dopo_un_guasto(monkeypatch):
    from app import cowork, knowledge as kn
    passi = []

    def finto_complete(system, user, settings, max_tokens=2500, timeout=120, **_kw):
        passi.append(user)
        if len(passi) == 1:
            # il modello cita il file con il codice: il ripristino deve ridare il nome vero
            import re
            doc = re.search(r"CV_\[NOME_\d{3}\]\.pdf", user).group(0)
            return json.dumps({"azione": "leggi", "documento": doc})
        if len(passi) < 4:
            return '{"azione": "cerca", "query": "referente"}'
        return '{"azione": "rispondi", "testo": "Fatto."}'
    letti = []

    class _P:
        def __init__(self, nome):
            letti.append(nome)

        def is_file(self):
            return False
    monkeypatch.setattr(cowork, "complete", finto_complete)
    monkeypatch.setattr(kn, "kb_list", lambda dept: [{"name": "CV_Laura_Bianchi.pdf", "chunks": 1}])
    monkeypatch.setattr(kn, "kb_search", lambda dept, q: ("nessun dato", []))
    monkeypatch.setattr(kn, "kb_file_path", lambda dept, nome: _P(nome))
    rete = _Rete(presidio=lambda body: _presidio_risposta({}, status=503))
    with patch.object(presidio_client.requests, "post", side_effect=rete):
        eventi = "".join(cowork.run("IT", "cw.giu@test", "riassumi il CV di Laura Bianchi",
                                    {"claude_api_key": "sk"}, DIZ))
    assert len(passi) == 4 and len(rete.presidio_calls()) == 1, len(rete.presidio_calls())
    assert letti == ["CV_Laura_Bianchi.pdf"]
    assert all("Laura" not in p for p in passi)
    assert AVVISO in eventi


def test_chat_log_senza_testo_utente():
    import contextlib
    import io
    c = _setup_chat("log@test")
    rete = _Rete()
    err = io.StringIO()
    try:
        with patch.object(orch.requests, "post", side_effect=rete), contextlib.redirect_stderr(err):
            c.post("/api/chat", json={"messages": [{"role": "user", "content":
                   "Prepara un riassunto della pratica di Teodolinda Sbrizzabarba, CF RSSMRA85T10A562S."}],
                   "free_mode": True, "engine": "claude"})
        log = err.getvalue()
        assert "[chat] generazione" in log, log
        assert "Teodolinda" not in log and "RSSMRA85T10A562S" not in log, log
    finally:
        _fine_chat()


def test_chat_errore_claude_mostra_comunque_avviso():
    c = _setup_chat("err@test")

    def _errore(url, json=None, headers=None, timeout=None, stream=False, **kw):
        raise orch.requests.HTTPError("529 Server Error: Overloaded")
    try:
        with patch.object(orch.requests, "post", side_effect=_errore):
            r = c.post("/api/chat", json={"messages": [{"role": "user", "content":
                       "Riassumi la pratica di Francesca Galli"}], "free_mode": True, "engine": "claude"})
        assert "Errore chiamata Claude" in r.text and AVVISO in r.text
    finally:
        _fine_chat()


if __name__ == "__main__":
    import inspect
    import sys

    class _MP:
        """monkeypatch minimale: ripristina gli attributi a fine test."""
        def __init__(self):
            self._salvati = []

        def setattr(self, obj, name, value):
            self._salvati.append((obj, name, getattr(obj, name)))
            setattr(obj, name, value)

        def undo(self):
            for obj, name, v in reversed(self._salvati):
                setattr(obj, name, v)

    fns = [f for n, f in sorted(globals().items())
           if n.startswith("test_") and inspect.isfunction(f)]
    failed = 0
    for f in fns:
        mp = _MP()
        try:
            kwargs = {}
            if "monkeypatch" in inspect.signature(f).parameters:
                kwargs["monkeypatch"] = mp
            f(**kwargs)
            print(f"  PASS  {f.__name__}")
        except Exception as e:
            failed += 1
            import traceback
            traceback.print_exc()
            print(f"  FAIL  {f.__name__}: {e}")
        finally:
            mp.undo()
    print(f"\n{len(fns)-failed}/{len(fns)} test superati.")
    sys.exit(1 if failed else 0)

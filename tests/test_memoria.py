"""
Test NOTE PERSONALI (Incremento 8): memoria di regole per-utente, esplicita,
kill-switch admin, iniettata in chat, generazione documenti e Attività.
Esecuzione:
    PYTHONPATH=. APP_DATA_DIR=./data_test python -m pytest tests/ -q
"""
import json
import os

os.environ.setdefault("APP_DATA_DIR", "./data_test")
from cryptography.fernet import Fernet
os.environ.setdefault("APP_SECRET_KEY", Fernet.generate_key().decode())

from fastapi.testclient import TestClient
from app.main import app
from app import store, auth, memory, docgen

store.init_db()


def _mk_user(name, dept="IT", is_admin=False):
    store.create_user(name, auth.hash_password("Password123"), dept, is_admin=is_admin)
    c = TestClient(app)
    c.post("/login", data={"username": name, "password": "Password123"},
           follow_redirects=False)
    return c


def _on():
    store.set_setting("memoria_note_enabled", "1")


def _off():
    store.set_setting("memoria_note_enabled", "0")


# ── Modulo: limiti, isolamento, kill-switch ─────────────────
def test_note_add_list_delete_clear_roundtrip():
    _on()
    memory.note_clear("nt_a@test")
    assert memory.note_add("nt_a@test", "rispondimi sempre in spagnolo")["ok"]
    assert memory.note_add("nt_a@test", "  rispondimi   sempre in spagnolo ")\
        .get("duplicata") is True                       # dedup normalizzato
    note = memory.note_list("nt_a@test")
    assert len(note) == 1 and note[0]["testo"] == "rispondimi sempre in spagnolo"
    assert memory.note_list("nt_b@test") == []          # isolamento per-utente
    assert memory.note_delete("nt_a@test", note[0]["id"]) is True
    assert memory.note_list("nt_a@test") == []
    memory.note_add("nt_a@test", "uno")
    memory.note_add("nt_a@test", "due")
    assert memory.note_clear("nt_a@test") == 2


def test_note_limiti_parlanti():
    _on()
    memory.note_clear("nt_lim@test")
    r = memory.note_add("nt_lim@test", "x" * (memory.NOTA_MAX_CHARS + 1))
    assert r["ok"] is False and "limite" in r["errore"].lower()
    for i in range(memory.NOTE_MAX):
        assert memory.note_add("nt_lim@test", f"nota {i}")["ok"]
    r = memory.note_add("nt_lim@test", "una di troppo")
    assert r["ok"] is False and str(memory.NOTE_MAX) in r["errore"]
    memory.note_clear("nt_lim@test")


def test_note_kill_switch_blocca_scritture_e_iniezione():
    _on()
    memory.note_clear("nt_off@test")
    assert memory.note_add("nt_off@test", "preferenza")["ok"]
    _off()
    r = memory.note_add("nt_off@test", "altra")
    assert r["ok"] is False and "non sono abilitate" in r["errore"]
    assert memory.build_note_context("nt_off@test") == ""   # spento = niente iniezione
    _on()
    assert "[NOTE PERSONALI" in memory.build_note_context("nt_off@test")
    memory.note_clear("nt_off@test")


def test_trigger_espliciti_positivi_e_negativi():
    pos = {
        "Ricordati che preferisco le risposte in spagnolo": "preferisco le risposte in spagnolo",
        "ricorda che le specifiche vanno sul template SC": "le specifiche vanno sul template SC",
        "D'ora in poi rispondi in inglese": "rispondi in inglese",
        "Remember that I want specs in Spanish": "I want specs in Spanish",
        "From now on, use British English": "use British English",
        "Recuerda que trabajo en Calidad": "trabajo en Calidad",
    }
    for msg, atteso in pos.items():
        assert memory.estrai_nota(msg) == atteso, msg
    neg = [
        "ricordami di chiamare Samir domani",
        "ti ricordi cosa abbiamo detto ieri?",
        "mi ricordo che avevamo un documento su questo",
        "qual è la scadenza NIS2?",
    ]
    for msg in neg:
        assert memory.estrai_nota(msg) == "", msg


# ── API e UI ────────────────────────────────────────────────
def test_api_note_roundtrip_e_audit():
    _on()
    c = _mk_user("nt_api@test")
    st = c.get("/api/memoria/note").json()
    assert st["enabled"] is True and st["note"] == []
    r = c.post("/api/memoria/note", data={"testo": "rispondimi in spagnolo"}).json()
    assert r["ok"] is True and len(r["note"]) == 1
    nid = r["note"][0]["id"]
    r = c.post("/api/memoria/note/delete", data={"nota_id": nid}).json()
    assert r["ok"] is True and r["note"] == []
    c.post("/api/memoria/note", data={"testo": "a"})
    c.post("/api/memoria/note", data={"testo": "b"})
    r = c.post("/api/memoria/note/delete", data={"nota_id": "all"}).json()
    assert r["note"] == []
    azioni = {a["action"] for a in store.audit_query(username="nt_api@test")}
    assert {"memoria_nota_aggiunta", "memoria_nota_rimossa",
            "memoria_note_svuotate"} <= azioni


def test_ui_connessioni_gated_dal_flag():
    _off()
    c = _mk_user("nt_ui@test")
    assert 'id="nota-add"' not in c.get("/settings").text
    assert c.get("/api/memoria/note").json()["enabled"] is False
    _on()
    html = c.get("/settings").text
    assert 'id="nota-add"' in html and 'id="note-clear"' in html
    _off()


def test_admin_ha_kill_switch_note():
    c = _mk_user("nt_adm@test", is_admin=True)
    assert 'name="memoria_note_enabled"' in c.get("/admin").text


# ── Iniezione: chat, generazione, Attività ──────────────────
def test_chat_trigger_salva_e_conferma(monkeypatch):
    _on()
    c = _mk_user("nt_chat@test")
    catturato = {}

    def fake_stream(messages, settings, anon_names, context="", free_mode=False,
                    mem_ctx="", fb_ctx="", images=None):
        catturato["mem_ctx"] = mem_ctx
        yield 'data: {"type": "delta", "text": "ok"}\n\n'
        yield 'data: {"type": "done"}\n\n'

    from app import main as main_mod
    monkeypatch.setattr(main_mod, "stream_reply", fake_stream)
    r = c.post("/api/chat", json={"messages": [{"role": "user", "content":
               "Ricordati che preferisco le risposte in spagnolo"}],
               "engine": "claude", "free_mode": True})
    assert r.status_code == 200
    assert "Aggiunto alle tue note personali" in r.text          # conferma 📌 visibile
    note = memory.note_list("nt_chat@test")
    assert len(note) == 1 and "spagnolo" in note[0]["testo"]
    # seconda richiesta qualunque: la nota è iniettata nel system
    r = c.post("/api/chat", json={"messages": [{"role": "user", "content": "ciao"}],
               "engine": "claude", "free_mode": True})
    assert "[NOTE PERSONALI" in catturato["mem_ctx"]
    assert "spagnolo" in catturato["mem_ctx"]
    memory.note_clear("nt_chat@test")
    _off()


def test_chat_trigger_inerte_a_flag_spento(monkeypatch):
    _off()
    c = _mk_user("nt_chatoff@test")

    def fake_stream(messages, settings, anon_names, context="", free_mode=False,
                    mem_ctx="", fb_ctx="", images=None):
        yield 'data: {"type": "done"}\n\n'

    from app import main as main_mod
    monkeypatch.setattr(main_mod, "stream_reply", fake_stream)
    r = c.post("/api/chat", json={"messages": [{"role": "user", "content":
               "Ricordati che preferisco le risposte in spagnolo"}],
               "engine": "claude", "free_mode": True})
    assert "Aggiunto alle tue note" not in r.text
    assert memory.note_list("nt_chatoff@test") == []


def test_build_spec_riceve_note(monkeypatch):
    catturato = {}

    def fake_complete(system, user, settings, max_tokens=4000, timeout=120):
        catturato["user"] = user
        return '{"title": "x", "sections": []}'

    monkeypatch.setattr(docgen.orchestrator, "complete", fake_complete)
    docgen.build_spec("docx", "specifica tecnica", "", {"claude_api_key": "k"},
                      note_utente="[NOTE PERSONALI DELL'UTENTE]\n- in spagnolo")
    assert "[NOTE PERSONALI" in catturato["user"] and "in spagnolo" in catturato["user"]


def test_cowork_riceve_note(monkeypatch):
    catture = []

    def fake_complete(system, user, settings, max_tokens=2500, timeout=120):
        catture.append(user)
        return '{"azione": "rispondi", "testo": "Hecho. [Fonte: Doc.docx]"}'

    from app import cowork
    monkeypatch.setattr(cowork.knowledge, "kb_list", lambda dept: [{"name": "Doc.docx"}])
    monkeypatch.setattr(cowork, "complete", fake_complete)
    events = list(cowork.run("IT", "nt_cw@test", "prepara la specifica",
                             {"claude_api_key": "k"}, [],
                             note_utente="[NOTE PERSONALI DELL'UTENTE]\n- specifiche in spagnolo"))
    assert "[NOTE PERSONALI" in catture[0] and "specifiche in spagnolo" in catture[0]
    assert any('"type": "done"' in e for e in events)


if __name__ == "__main__":
    import inspect
    import sys

    class _MP:
        def setattr(self, obj, name, value):
            setattr(obj, name, value)

    fns = [f for n, f in sorted(globals().items())
           if n.startswith("test_") and inspect.isfunction(f)]
    failed = 0
    for f in fns:
        try:
            kwargs = {}
            if "monkeypatch" in inspect.signature(f).parameters:
                kwargs["monkeypatch"] = _MP()
            f(**kwargs)
            print(f"  PASS  {f.__name__}")
        except Exception as e:
            failed += 1
            print(f"  FAIL  {f.__name__}: {e}")
    print(f"\n{len(fns)-failed}/{len(fns)} test superati.")
    sys.exit(1 if failed else 0)

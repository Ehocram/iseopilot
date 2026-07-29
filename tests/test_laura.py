"""
Test "caso Laura": la modifica di un documento già generato deve rigenerare il
file (link di download), e ISEOPilot non deve inventare auto-supporto.
Esecuzione:
    PYTHONPATH=. APP_DATA_DIR=./data_test python -m pytest tests/ -q
"""
import os

os.environ.setdefault("APP_DATA_DIR", "./data_test")
from cryptography.fernet import Fernet
os.environ.setdefault("APP_SECRET_KEY", Fernet.generate_key().decode())

from fastapi.testclient import TestClient
from app.main import app
from app import store, auth, docgen

store.init_db()

_GEN = {"role": "assistant",
        "content": "Ho preparato **Piano di crescita Cristian.docx**. Puoi scaricarlo qui sotto."}
_GEN_PDF = {"role": "assistant",
            "content": "Ho preparato **Report NIS2.pdf**. Puoi scaricarlo qui sotto."}


def _mk_user(name, dept="HR"):
    store.create_user(name, auth.hash_password("Password123"), dept, is_admin=False)
    c = TestClient(app)
    c.post("/login", data={"username": name, "password": "Password123"},
           follow_redirects=False)
    return c


# ── Rilevatore: ramo modifica ───────────────────────────────
def test_modifica_dopo_generazione_eredita_il_formato():
    casi = [
        "perfetto, ora togli la parte sulle certificazioni e aggiorna il documento",
        "modifica il piano: aggiungi gli obiettivi 2027",
        "riscrivilo più sintetico",
        "aggiornalo con le date nuove",
        "please update the document with the new dates",
        "sostituisci la sezione 2 del file con questa versione",
    ]
    for msg in casi:
        assert docgen.detect_request_with_history(msg, [_GEN]) == "docx", msg


def test_modifica_eredita_dal_file_giusto():
    # l'estensione del marcatore comanda: pdf resta pdf
    assert docgen.detect_request_with_history(
        "aggiorna il documento con i dati di luglio", [_GEN_PDF]) == "pdf"
    # con più generazioni vince la PIÙ RECENTE
    hist = [_GEN_PDF, {"role": "user", "content": "ora fammi il piano"}, _GEN]
    assert docgen.detect_request_with_history(
        "togli la parte finale e aggiorna il documento", hist) == "docx"


def test_modifica_non_scatta_senza_generazione_precedente():
    # nessun file mai generato: nessuna invenzione
    hist = [{"role": "assistant", "content": "Ecco il piano in sintesi…"}]
    assert docgen.detect_request_with_history(
        "aggiorna il documento con le date nuove", hist) is None


def test_modifica_non_scatta_su_domande_e_chat_normale():
    negativi = [
        "spiegami come posso modificare il documento",     # domanda, non ordine
        "che tempo fa domani?",                            # fuori tema
        "cosa ne pensi del piano?",                        # nessun verbo di modifica
    ]
    for msg in negativi:
        assert docgen.detect_request_with_history(msg, [_GEN]) is None, msg


def test_marcatore_fuori_finestra_non_scatta():
    hist = [_GEN] + [{"role": "user", "content": f"messaggio {i}"} for i in range(12)]
    assert docgen.detect_request_with_history(
        "aggiorna il documento", hist) is None   # oltre gli ultimi 10 turni


# ── E2E: il secondo giro produce il file (il bug di Laura) ──
def test_flusso_laura_secondo_giro_genera_il_file(monkeypatch, tmp_path):
    c = _mk_user("laura@test")
    visto = {}
    out = tmp_path / "gen.docx"
    out.write_bytes(b"PK\x03\x04finto")

    def fake_generate(fmt, req, ctx, st, hist="", templates=None, note_utente=""):
        visto["fmt"] = fmt
        visto["hist"] = hist
        return str(out), "Piano di crescita Cristian v2.docx"

    from app import main as main_mod
    monkeypatch.setattr(main_mod.docgen, "generate", fake_generate)
    r = c.post("/api/chat", json={"engine": "claude", "free_mode": True, "messages": [
        {"role": "user", "content": "creami il word del piano di crescita di Cristian"},
        _GEN,
        {"role": "user", "content": "perfetto: togli la parte certificazioni e aggiorna il documento"},
    ]})
    assert r.status_code == 200
    assert visto["fmt"] == "docx"                      # la modifica ha rigenerato
    assert "Ho preparato" in r.text                    # link/download presente
    assert "creami il word del piano" in visto["hist"]  # continuità col primo giro


# ── Anti auto-supporto inventato ────────────────────────────
def test_build_system_regola_su_iseopilot_stesso():
    from app.orchestrator import build_system
    out = build_system("Aziendale formale", "Italiano")
    assert "SU ISEOPILOT STESSO" in out
    assert "NON inventare" in out and "amministratore" in out


if __name__ == "__main__":
    import inspect
    import sys
    import tempfile
    from pathlib import Path

    class _MP:
        def setattr(self, obj, name, value):
            setattr(obj, name, value)

    fns = [f for n, f in sorted(globals().items())
           if n.startswith("test_") and inspect.isfunction(f)]
    failed = 0
    for f in fns:
        try:
            kwargs = {}
            sig = inspect.signature(f)
            td = None
            if "tmp_path" in sig.parameters:
                td = tempfile.TemporaryDirectory()
                kwargs["tmp_path"] = Path(td.name)
            if "monkeypatch" in sig.parameters:
                kwargs["monkeypatch"] = _MP()
            f(**kwargs)
            print(f"  PASS  {f.__name__}")
            if td:
                td.cleanup()
        except Exception as e:
            failed += 1
            print(f"  FAIL  {f.__name__}: {e}")
    print(f"\n{len(fns)-failed}/{len(fns)} test superati.")
    sys.exit(1 if failed else 0)

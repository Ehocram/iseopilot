"""
Test connettore Power BI (quinta fonte). Tutti OFFLINE: le chiamate HTTP verso
Microsoft sono simulate. Esecuzione:
    PYTHONPATH=. APP_DATA_DIR=./data_test python -m pytest tests/ -q
oppure standalone:
    PYTHONPATH=. APP_DATA_DIR=./data_test python tests/test_powerbi.py
"""
import json
import os
from pathlib import Path

os.environ.setdefault("APP_DATA_DIR", "./data_test")
from cryptography.fernet import Fernet
os.environ.setdefault("APP_SECRET_KEY", Fernet.generate_key().decode())

from fastapi.testclient import TestClient
from app.main import app
from app import store, auth, connectors
from app.engines import powerbi_search

store.init_db()


def fresh_client():
    return TestClient(app)


def login(c, username, password):
    return c.post("/login", data={"username": username, "password": password},
                  follow_redirects=False)


def _mk_user(name, dept="IT", is_admin=False):
    store.create_user(name, auth.hash_password("Password123"), dept, is_admin=is_admin)
    c = fresh_client()
    login(c, name, "Password123")
    return c


def _fake_connect(username):
    """Simula un account Power BI connesso: scrive un token per-utente."""
    p = store.user_token_path(username, "powerbi")
    p.write_text(json.dumps({"access_token": "tok-finto", "refresh_token": "r",
                             "expires_at": 9999999999}), encoding="utf-8")
    return p


def _flag(on: bool):
    store.set_setting("pbi_enabled", "1" if on else "0")


# ── Registrazione del connettore e kill-switch admin ────────
def test_powerbi_registered_e_flag_admin():
    assert "powerbi" in connectors.CONNECTORS
    _flag(False)
    assert connectors.is_configured("powerbi") is False  # SPENTO di default
    _flag(True)
    assert connectors.is_configured("powerbi") is True   # identificatori ISEO presenti


def test_ms_cfg_powerbi_scope_separato():
    cfg = connectors.ms_cfg("powerbi")
    assert "analysis.windows.net/powerbi/api" in cfg["scope"]
    assert "offline_access" in cfg["scope"]
    # separazione: scope diversi da OneDrive (Graph) e Dynamics (F&O)
    assert cfg["scope"] != connectors.ms_cfg("onedrive")["scope"]
    assert cfg["scope"] != connectors.ms_cfg("dynamics")["scope"]


# ── Interfaccia: Connessioni, chat, admin ───────────────────
def test_settings_page_shows_powerbi_controls():
    _flag(True)
    c = _mk_user("pbi_ui@test")
    html = c.get("/settings").text
    assert 'data-conn="powerbi"' in html
    assert 'name="use_powerbi"' in html
    assert "box-powerbi" in html


def test_settings_page_flag_spento_superficie_zero():
    _flag(False)
    c = _mk_user("pbi_ui_off@test")
    html = c.get("/settings").text
    assert 'data-conn="powerbi"' not in html
    assert 'name="use_powerbi"' not in html


def test_settings_use_powerbi_roundtrip():
    c = _mk_user("pbi_tg@test")
    _fake_connect("pbi_tg@test")  # il toggle si salva solo se ha senso attivarlo
    r = c.post("/settings", data={"use_powerbi": "1", "ajax": "1"})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert store.get_user_setting("pbi_tg@test", "use_powerbi", "0") == "1"
    r = c.post("/settings", data={"use_kb": "1", "ajax": "1"})  # non inviato -> 0
    assert store.get_user_setting("pbi_tg@test", "use_powerbi", "1") == "0"


def test_chat_page_shows_powerbi_pill():
    _flag(True)
    c = _mk_user("pbi_pill@test")
    html = c.get("/").text
    assert 'value="powerbi"' in html


def test_chat_page_flag_spento_niente_pill():
    _flag(False)
    c = _mk_user("pbi_pill_off@test")
    assert 'value="powerbi"' not in c.get("/").text


def test_chat_powerbi_not_connected_errore_esplicito():
    _flag(True)
    c = _mk_user("pbi_chat@test")
    r = c.post("/api/chat", json={"messages": [{"role": "user", "content": "fatturato?"}],
                                  "engine": "claude", "source": "powerbi"})
    assert r.status_code == 200
    assert "Power BI non è connesso" in r.text


def test_chat_powerbi_flag_spento_errore_esplicito():
    _flag(False)
    c = _mk_user("pbi_chat_off@test")
    _fake_connect("pbi_chat_off@test")  # anche già connesso: spento vince
    r = c.post("/api/chat", json={"messages": [{"role": "user", "content": "fatturato?"}],
                                  "engine": "claude", "source": "powerbi"})
    assert r.status_code == 200
    assert "disabilitato dall'amministratore" in r.text


def test_connect_start_powerbi_gestito_senza_rete():
    _flag(True)
    c = _mk_user("pbi_start@test")
    r = c.post("/connect/powerbi/start")
    assert r.status_code == 200
    j = r.json()
    assert j["ok"] is False and j.get("error")  # errore parlante, mai 500


def test_admin_page_shows_powerbi_config():
    c = _mk_user("pbi_admin@test", is_admin=True)
    html = c.get("/admin").text
    assert 'name="pbi_client_id"' in html and 'name="pbi_tenant_id"' in html
    assert 'name="pbi_enabled"' in html
    assert "pbiDiagBtn" in html


def test_catalog_routes_richiedono_flag_e_connessione():
    c = _mk_user("pbi_cat@test")
    _flag(False)
    j = c.post("/connect/powerbi/catalog/start").json()
    assert j["ok"] is False and "disabilitato" in j["errore"]
    _flag(True)
    j = c.post("/connect/powerbi/catalog/start").json()
    assert j["ok"] is False and "non connesso" in j["errore"]
    st = c.get("/connect/powerbi/catalog/status").json()
    assert st["running"] is False and st["catalog"]["present"] is False


def test_search_branch_flag_spento_messaggio_esplicito():
    _flag(False)
    _fake_connect("pbi_srch_off@test")  # token presente ma connettore spento
    text, links = connectors.search_with_links("pbi_srch_off@test", "powerbi", "fatturato")
    assert "disabilitato dall'amministratore" in text and links == []


# ── Unità del motore powerbi_search ─────────────────────────
def _pbi(tmp_path: Path) -> powerbi_search.PowerBISearch:
    tok = tmp_path / "powerbi_token.json"
    tok.write_text(json.dumps({"access_token": "tok", "expires_at": 9999999999}),
                   encoding="utf-8")
    return powerbi_search.PowerBISearch({
        "pbi_client_id": "cid", "pbi_tenant_id": "tid",
        "pbi_token_file": str(tok),
        "pbi_catalog_file": str(tmp_path / "powerbi_catalog.json"),
        "ai_engine": "claude", "claude_api_key": "k",
    })


def test_clean_key_e_pick():
    assert powerbi_search._clean_key("Sales[Amount]") == "Amount"
    assert powerbi_search._clean_key("[Totale]") == "Totale"
    assert powerbi_search._clean_key("Semplice") == "Semplice"
    row = {"[Table Name]": "Vendite", "[Column Name]": "Regione", "[Cardinality]": 7}
    assert powerbi_search._pick(row, "table name") == "Vendite"
    assert powerbi_search._pick(row, "columnname") == "Regione"
    assert powerbi_search._pick(row, "cardinality") == "7"


def test_execute_dax_rifiuta_non_evaluate(tmp_path):
    pbi = _pbi(tmp_path)
    res = pbi._execute_dax("", "ds1", "DROP TABLE x", "tok")
    assert res["ok"] is False and "EVALUATE" in res["errore"]
    res = pbi._execute_dax("", "ds1", "", "tok")
    assert res["ok"] is False


def test_friendly_http_error_parla_chiaro(tmp_path):
    pbi = _pbi(tmp_path)
    assert "Build" in pbi._friendly_http_error(401, "PowerBINotAuthorizedException")
    assert "Dataset Execute Queries REST API" in pbi._friendly_http_error(
        403, "ExecuteQueries feature is disabled")
    assert "catalogo" in pbi._friendly_http_error(404, "not found").lower()


def test_harvest_schema_da_columnstatistics(tmp_path, monkeypatch):
    pbi = _pbi(tmp_path)
    rows = [
        {"[Table Name]": "Vendite", "[Column Name]": "Regione", "[Cardinality]": 12},
        {"[Table Name]": "Vendite", "[Column Name]": "Importo", "[Cardinality]": 900},
        {"[Table Name]": "LocalDateTable_abc", "[Column Name]": "Date", "[Cardinality]": 1},
    ]
    monkeypatch.setattr(pbi, "_execute_dax",
                        lambda gid, did, dax, tok, timeout=60: {"ok": True, "rows": rows,
                                                                "status": 200, "errore": ""})
    sch = pbi._harvest_schema("", "ds1", "tok")
    assert sch["ok"] is True
    assert set(sch["tabelle"].keys()) == {"Vendite"}          # auto-date filtrata
    assert sch["tabelle"]["Vendite"]["colonne"] == ["Regione", "Importo"]


def test_rank_datasets_per_pertinenza(tmp_path):
    pbi = _pbi(tmp_path)
    catalog = {"items": [
        {"workspace": "Finance", "dataset": "Bilancio", "tabelle": {}, "misure": []},
        {"workspace": "Sales", "dataset": "Vendite Italia",
         "tabelle": {"Ordini": {"colonne": ["Regione", "Fatturato"]}}, "misure": ["Fatturato Totale"]},
    ]}
    top = pbi.rank_datasets(catalog, "qual è il fatturato delle vendite per regione?")
    assert top[0]["dataset"] == "Vendite Italia"


def test_format_rows_applica_tetto(tmp_path):
    pbi = _pbi(tmp_path)
    rows = [{"Vendite[N]": i} for i in range(pbi.HARD_LIMIT + 20)]
    out = pbi._format_rows(rows)
    assert out.count("• ") == pbi.HARD_LIMIT
    assert "20 righe ulteriori non mostrate" in out


def test_search_fail_loud_su_catalogo_assente(tmp_path):
    pbi = _pbi(tmp_path)
    out = pbi.search("fatturato 2025")
    assert "Catalogo" in out and "Connessioni" in out


def test_search_fail_loud_su_non_connesso(tmp_path):
    pbi = powerbi_search.PowerBISearch({
        "pbi_client_id": "cid", "pbi_tenant_id": "tid",
        "pbi_token_file": str(tmp_path / "assente.json"),
        "pbi_catalog_file": str(tmp_path / "cat.json"),
    })
    assert "non connesso" in pbi.search("qualsiasi")


def test_planner_concludi_end_to_end(tmp_path, monkeypatch):
    pbi = _pbi(tmp_path)
    catalog = {"versione": "1.0", "generato": "2026-07-16", "workspaces": 1, "items": [{
        "workspace": "Sales", "group_id": "g1", "dataset": "Vendite Italia",
        "dataset_id": "d1",
        "web_url": "https://app.powerbi.com/groups/g1/datasets/d1/details",
        "schema_ok": True, "schema_note": "",
        "tabelle": {"Ordini": {"colonne": ["Regione", "Fatturato"], "cardinalita": {}}},
        "misure": ["Fatturato Totale"], "misure_note": "",
    }]}
    Path(pbi.catalog_file).write_text(json.dumps(catalog), encoding="utf-8")
    monkeypatch.setattr(pbi, "_ask_ai", lambda s, u, max_tokens=700: json.dumps({
        "azione": "concludi", "dataset": "Vendite Italia",
        "dax": "EVALUATE SUMMARIZECOLUMNS('Ordini'[Regione], \"Tot\", SUM('Ordini'[Fatturato]))",
        "spiegazione": "Fatturato per regione."}))
    monkeypatch.setattr(pbi, "_execute_dax",
                        lambda gid, did, dax, tok, timeout=60: {
                            "ok": True, "status": 200, "errore": "",
                            "rows": [{"Ordini[Regione]": "Lombardia", "[Tot]": 1200},
                                     {"Ordini[Regione]": "Lazio", "[Tot]": 800}]})
    out = pbi.search("fatturato per regione")
    assert out.startswith("[Power BI — Sales / Vendite Italia]")
    assert "2 righe" in out and "Lombardia" in out
    assert pbi.last_links and "app.powerbi.com" in pbi.last_links[0][1]


def test_planner_errore_accesso_interrompe_e_parla(tmp_path, monkeypatch):
    pbi = _pbi(tmp_path)
    catalog = {"items": [{"workspace": "Sales", "group_id": "g1",
                          "dataset": "Vendite Italia", "dataset_id": "d1",
                          "web_url": "u", "schema_ok": True, "schema_note": "",
                          "tabelle": {"Ordini": {"colonne": ["Regione"]}},
                          "misure": [], "misure_note": ""}]}
    Path(pbi.catalog_file).write_text(json.dumps(catalog), encoding="utf-8")
    monkeypatch.setattr(pbi, "_ask_ai", lambda s, u, max_tokens=700: json.dumps({
        "azione": "dax", "dataset": "Vendite Italia", "dax": "EVALUATE 'Ordini'"}))
    monkeypatch.setattr(pbi, "_execute_dax",
                        lambda gid, did, dax, tok, timeout=60: {
                            "ok": False, "status": 401, "rows": [],
                            "errore": pbi._friendly_http_error(401, "")})
    out = pbi.search("elenco ordini")
    assert "Build" in out  # l'errore di permessi arriva all'utente, subito


def test_build_catalog_con_http_simulato(tmp_path, monkeypatch):
    pbi = _pbi(tmp_path)

    class _R:
        def __init__(self, status, payload=None, content=b""):
            self.status_code = status
            self._payload = payload or {}
            self.content = content
            self.headers = {}
            self.text = json.dumps(self._payload)

        def json(self):
            return self._payload

    def fake_req(method, url, token, payload=None, timeout=60):
        if url.endswith("/groups?$top=5000"):
            return _R(200, {"value": [{"id": "g1", "name": "Sales"}]})
        if url.endswith("/groups/g1/datasets"):
            return _R(200, {"value": [{"id": "d1", "name": "Vendite Italia",
                                       "webUrl": "https://app.powerbi.com/groups/g1/datasets/d1"}]})
        if url.endswith("/datasets"):  # area personale
            return _R(200, {"value": []})
        if url.endswith("/executeQueries"):
            return _R(200, {"results": [{"tables": [{"rows": [
                {"[Table Name]": "Ordini", "[Column Name]": "Regione", "[Cardinality]": 5},
                {"[Table Name]": "Ordini", "[Column Name]": "Fatturato", "[Cardinality]": 99},
            ]}]}]})
        if url.endswith("/executeDaxQueries"):  # capacità non dedicata
            return _R(404, {"error": {"code": "NotFound", "message": "capacity"}})
        raise AssertionError("URL inatteso: " + url)

    monkeypatch.setattr(pbi, "_req", fake_req)
    res = pbi.build_catalog()
    assert res.get("ok") and res["datasets"] == 1 and res["interrogabili"] == 1
    cat = json.loads(Path(pbi.catalog_file).read_text(encoding="utf-8"))
    item = cat["items"][0]
    assert item["tabelle"]["Ordini"]["colonne"] == ["Regione", "Fatturato"]
    assert item["misure"] == []
    assert item["misure_note"]  # motivo dichiarato, non silenzio


def test_pbi_full_cfg_percorsi_per_utente():
    cfg_a = connectors._pbi_full_cfg("utenteA@test", None)
    cfg_b = connectors._pbi_full_cfg("utenteB@test", None)
    assert cfg_a["pbi_token_file"] != cfg_b["pbi_token_file"]
    assert cfg_a["pbi_catalog_file"] != cfg_b["pbi_catalog_file"]
    assert Path(cfg_a["pbi_catalog_file"]).parent == Path(cfg_a["pbi_token_file"]).parent


if __name__ == "__main__":
    import inspect
    import sys
    fns = [f for n, f in sorted(globals().items())
           if n.startswith("test_") and inspect.isfunction(f)]
    failed = 0

    class _MP:
        def setattr(self, obj, name, value):
            setattr(obj, name, value)

    import tempfile
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
            import traceback
            print(f"  FAIL  {f.__name__}: {e}")
    print(f"\n{len(fns)-failed}/{len(fns)} test superati.")
    sys.exit(1 if failed else 0)

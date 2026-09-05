"""
Test connettore MICROSOFT 365 (Incremento 11): ricerca unificata SharePoint,
Posta e Teams con identità delegata; kill-switch, interruttori per fonte,
concessione individuale, minimizzazione verso il modello, download integrale.
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
from app import store, auth, connectors
from app.engines import m365_search

store.init_db()


def _mk_user(name, dept="IT", is_admin=False):
    store.create_user(name, auth.hash_password("Password123"), dept, is_admin=is_admin)
    c = TestClient(app)
    c.post("/login", data={"username": name, "password": "Password123"},
           follow_redirects=False)
    return c


def _on(**fonti):
    store.set_setting("m365_enabled", "1")
    for f in ("sharepoint", "mail", "teams"):
        store.set_setting(f"m365_src_{f}", "1" if fonti.get(f) else "0")


def _off():
    store.set_setting("m365_enabled", "0")
    for f in ("sharepoint", "mail", "teams"):
        store.set_setting(f"m365_src_{f}", "0")


def _finto_token(user):
    import time
    p = store.user_token_path(user, "m365")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"access_token": "T", "refresh_token": "R",
                             "expires_at": time.time() + 3600}), encoding="utf-8")


# risposta Graph realistica: un file SharePoint, una mail, un messaggio Teams
_GRAPH_HITS = {"value": [{"hitsContainers": [{"hits": [
    {"summary": "Il contratto quadro prevede l'adeguamento annuale.",
     "resource": {"@odata.type": "#microsoft.graph.driveItem",
                  "id": "ITEM1", "name": "Contratto quadro.docx",
                  "webUrl": "https://iseo.sharepoint.com/x/Contratto.docx",
                  "lastModifiedDateTime": "2026-08-01T10:00:00Z",
                  "parentReference": {"driveId": "DRIVE1", "siteId": "site,1,2"}}},
    {"summary": "Confermo la consegna per il 12 settembre.",
     "resource": {"@odata.type": "#microsoft.graph.message",
                  "id": "MSG1", "subject": "Consegna ordine 4471",
                  "receivedDateTime": "2026-08-20T09:15:00Z",
                  "from": {"emailAddress": {"name": "Anna Verdi",
                                            "address": "anna@cliente.it"}}}},
    {"summary": "",
     "resource": {"@odata.type": "#microsoft.graph.chatMessage",
                  "id": "CHAT1", "chatId": "CID1",
                  "createdDateTime": "2026-08-21T14:00:00Z",
                  "from": {"user": {"displayName": "Samir"}},
                  "body": {"content": "<p>La VM è stata <b>riavviata</b>.</p>"}}},
]}]}]}


class _Resp:
    def __init__(self, status=200, payload=None, content=b""):
        self.status_code = status
        self._payload = payload if payload is not None else {}
        self.content = content
        self.text = json.dumps(self._payload)

    def json(self):
        return self._payload


# ── Gate: kill-switch, fonti, concessione individuale ───────
def test_kill_switch_spento_nessuna_ricerca():
    _off()
    assert connectors.is_configured("m365") is False
    _finto_token("m3a@test")
    testo, links = connectors.search_with_links("m3a@test", "m365", "contratto")
    assert "disabilitato dall'amministratore" in testo and links == []


def test_serve_concessione_individuale():
    _on(sharepoint=True)
    _finto_token("m3b@test")
    store.set_user_setting("m3b@test", "m365_access", "0")
    testo, _ = connectors.search_with_links("m3b@test", "m365", "contratto")
    assert "non abilitato per la tua utenza" in testo
    store.set_user_setting("m3b@test", "m365_access", "1")
    assert connectors.m365_user_allowed("m3b@test") is True
    _off()


def test_nessuna_fonte_attiva_dichiarato():
    store.set_setting("m365_enabled", "1")
    for f in ("sharepoint", "mail", "teams"):
        store.set_setting(f"m365_src_{f}", "0")
    _finto_token("m3c@test")
    store.set_user_setting("m3c@test", "m365_access", "1")
    testo, _ = connectors.search_with_links("m3c@test", "m365", "x")
    assert "Nessuna fonte abilitata" in testo
    _off()


def test_default_fonti_posta_e_teams_spente():
    for k in ("m365_src_sharepoint", "m365_src_mail", "m365_src_teams"):
        store.set_setting(k, "")
        store.set_setting(k, store.get_setting(k, ""))
    store.set_setting("m365_src_sharepoint", "1")
    store.set_setting("m365_src_mail", "0")
    store.set_setting("m365_src_teams", "0")
    assert connectors.m365_fonti_attive() == ["sharepoint"]


# ── Motore: entità richieste e formattazione ────────────────
def test_entita_richieste_seguono_le_fonti(monkeypatch):
    catturato = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        catturato["url"] = url
        catturato["body"] = json
        return _Resp(200, {"value": []})

    import requests
    monkeypatch.setattr(requests, "post", fake_post)
    m = m365_search.M365Search({"client_id": "c", "tenant_id": "t",
                                "token_path": str(store.user_token_path("m3d@test", "m365"))})
    _finto_token("m3d@test")
    m.tm._data = {"access_token": "T", "expires_at": 9e9}
    m.search("contratto", ["sharepoint"])
    assert catturato["url"].endswith("/search/query")
    ent = catturato["body"]["requests"][0]["entityTypes"]
    assert set(ent) == {"driveItem", "listItem"}          # solo SharePoint
    m.search("contratto", ["sharepoint", "mail", "teams"])
    ent = catturato["body"]["requests"][0]["entityTypes"]
    assert "message" in ent and "chatMessage" in ent


def test_formattazione_e_riferimenti(monkeypatch):
    import requests
    monkeypatch.setattr(requests, "post",
                        lambda *a, **k: _Resp(200, _GRAPH_HITS))
    m = m365_search.M365Search({"client_id": "c", "tenant_id": "t", "token_path": "/tmp/x.json"})
    m.tm._data = {"access_token": "T", "expires_at": 9e9}
    testo, rif = m.search("consegna", ["sharepoint", "mail", "teams"])
    assert "[SharePoint — Contratto quadro.docx]" in testo
    assert "[Posta — Consegna ordine 4471]" in testo and "Anna Verdi" in testo
    assert "[Teams — messaggio del 2026-08-21 14:00]" in testo
    # HTML del messaggio Teams ripulito, mai passato grezzo al modello
    assert "La VM è stata riavviata." in testo and "<b>" not in testo
    tipi = {r["kind"] for r in rif}
    assert tipi == {"sharepoint", "mail", "teams"}
    mail = next(r for r in rif if r["kind"] == "mail")
    assert mail["id"] == "MSG1"
    teams = next(r for r in rif if r["kind"] == "teams")
    assert teams["chat_id"] == "CID1"


def test_snippet_troncato_verso_il_modello(monkeypatch):
    lungo = {"value": [{"hitsContainers": [{"hits": [
        {"summary": "x" * 5000,
         "resource": {"@odata.type": "#microsoft.graph.message", "id": "M",
                      "subject": "S", "receivedDateTime": "2026-08-20T09:00:00Z",
                      "from": {"emailAddress": {"name": "N", "address": "a@b.c"}}}}]}]}]}
    import requests
    monkeypatch.setattr(requests, "post", lambda *a, **k: _Resp(200, lungo))
    m = m365_search.M365Search({"client_id": "c", "tenant_id": "t", "token_path": "/tmp/x.json"})
    m.tm._data = {"access_token": "T", "expires_at": 9e9}
    testo, _ = m.search("q", ["mail"])
    assert len(testo) <= m365_search.SNIPPET_MAX + 400      # minimizzazione applicata


def test_403_graph_messaggio_parlante(monkeypatch):
    import requests
    monkeypatch.setattr(requests, "post", lambda *a, **k: _Resp(403, {"error": "denied"}))
    m = m365_search.M365Search({"client_id": "c", "tenant_id": "t", "token_path": "/tmp/x.json"})
    m.tm._data = {"access_token": "T", "expires_at": 9e9}
    testo, rif = m.search("q", ["mail"])
    assert "Permessi insufficienti" in testo and "Mail.Read" in testo
    assert rif == []


def test_senza_token_non_finge():
    m = m365_search.M365Search({"client_id": "c", "tenant_id": "t", "token_path": "/tmp/inesistente.json"})
    testo, rif = m.search("q", ["mail"])
    assert "Non connesso" in testo and rif == []


# ── Download integrale: non passa dal modello ───────────────
def test_download_mail_eml(monkeypatch):
    import requests

    def fake_get(url, headers=None, timeout=None):
        if url.endswith("/$value"):
            return _Resp(200, {}, content=b"From: a@b.c\r\nSubject: Consegna\r\n\r\ncorpo")
        return _Resp(200, {"subject": "Consegna ordine 4471"})

    monkeypatch.setattr(requests, "get", fake_get)
    m = m365_search.M365Search({"client_id": "c", "tenant_id": "t", "token_path": "/tmp/x.json"})
    m.tm._data = {"access_token": "T", "expires_at": 9e9}
    blob, fname, mime = m.fetch_full("mail", "MSG1")
    assert b"corpo" in blob and fname.endswith(".eml") and mime == "message/rfc822"
    assert "Consegna ordine 4471" in fname


def test_download_teams_richiede_chat_id(monkeypatch):
    import pytest
    m = m365_search.M365Search({"client_id": "c", "tenant_id": "t", "token_path": "/tmp/x.json"})
    m.tm._data = {"access_token": "T", "expires_at": 9e9}
    with pytest.raises(ValueError) as e:
        m.fetch_full("teams", "CHAT1")
    assert "chat mancante" in str(e.value)


def test_rotta_download_gated():
    _off()
    c = _mk_user("m3dl@test")
    assert c.get("/m365/full?kind=mail&id=X").status_code == 404      # kill-switch
    _on(mail=True)
    assert c.get("/m365/full?kind=mail&id=X").status_code == 403      # senza grant
    store.set_user_setting("m3dl@test", "m365_access", "1")
    assert c.get("/m365/full?kind=mail&id=X").status_code == 400      # non connesso
    _finto_token("m3dl@test")
    assert c.get("/m365/full?kind=pippo&id=X").status_code == 400     # tipo non valido
    _off()


def test_rotta_download_audit(monkeypatch):
    _on(mail=True)
    c = _mk_user("m3aud@test")
    store.set_user_setting("m3aud@test", "m365_access", "1")
    _finto_token("m3aud@test")
    monkeypatch.setattr(m365_search.M365Search, "fetch_full",
                        lambda self, k, i, r="": (b"x", "Messaggio.eml", "message/rfc822"))
    r = c.get("/m365/full?kind=mail&id=MSG1")
    assert r.status_code == 200
    assert "attachment" in r.headers.get("content-disposition", "")
    assert r.headers.get("cache-control") == "no-store"
    azioni = {a["action"] for a in store.audit_query(username="m3aud@test")}
    assert "m365_download" in azioni
    _off()


# ── Superfici e non-regressione ─────────────────────────────
def test_connessione_gated_lato_server():
    _off()
    c = _mk_user("m3conn@test")
    r = c.post("/connect/m365/start")
    assert r.status_code == 403 and "disabilitato" in r.json()["error"]
    _on(sharepoint=True)
    r = c.post("/connect/m365/start")
    assert r.status_code == 403 and "non abilitato per la tua utenza" in r.json()["error"]
    _off()


def test_pannello_connessioni_e_pill_chat_gated():
    _off()
    c = _mk_user("m3ui@test")
    # da spento: nessuna superficie, né in Connessioni né come fonte in chat
    assert 'data-conn="m365"' not in c.get("/settings").text
    assert 'value="m365"' not in c.get("/").text
    _on(sharepoint=True)
    store.set_user_setting("m3ui@test", "m365_access", "1")
    assert 'data-conn="m365"' in c.get("/settings").text
    html = c.get("/").text
    assert 'value="m365"' in html and "disabled" in html   # visibile ma non connesso
    _off()


def test_admin_ha_campi_client_tenant_e_salva():
    c = _mk_user("m3id@test", is_admin=True)
    html = c.get("/admin").text
    assert 'name="m365_client_id"' in html and 'name="m365_tenant_id"' in html
    nuovo = "11111111-2222-3333-4444-555555555555"
    c.post("/admin", data={"claude_model": "claude-opus-4-8",
                           "m365_client_id": nuovo}, follow_redirects=False)
    assert connectors.ms_cfg("m365")["client_id"] == nuovo
    # il cambio di app registration invalida i token: deve restare traccia
    det = " ".join(a["detail"] for a in store.audit_query(username="m3id@test")
                   if a["action"] == "m365_config")
    assert "client_id" in det and "riconnettere" in det
    # campo vuoto = ritorno al default, non stringa vuota
    c.post("/admin", data={"claude_model": "claude-opus-4-8", "m365_client_id": ""},
           follow_redirects=False)
    assert connectors.ms_cfg("m365")["client_id"]


def test_admin_ha_kill_switch_e_interruttori():
    c = _mk_user("m3adm@test", is_admin=True)
    html = c.get("/admin").text
    for nome in ("m365_enabled", "m365_src_sharepoint", "m365_src_mail", "m365_src_teams"):
        assert f'name="{nome}"' in html
    assert 'name="m365_access"' in c.get("/admin/users").text


def test_modifica_interruttori_tracciata_in_audit():
    c = _mk_user("m3cfg@test", is_admin=True)
    c.post("/admin", data={"claude_model": "claude-opus-4-8",
                           "m365_enabled": "1", "m365_src_mail": "1"},
           follow_redirects=False)
    azioni = [a for a in store.audit_query(username="m3cfg@test")
              if a["action"] == "m365_config"]
    assert azioni and any("m365_src_mail" in a["detail"] for a in azioni)
    _off()


def test_altre_fonti_non_toccate():
    """Non-regressione: l'aggiunta di m365 non altera le fonti esistenti."""
    assert "m365" in connectors.CONNECTORS
    for c in ("onedrive", "dynamics", "powerbi"):
        assert c in connectors.CONNECTORS
    assert connectors.ms_cfg("onedrive")["scope"] == "Files.Read.All offline_access"


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

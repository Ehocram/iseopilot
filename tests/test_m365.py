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


_PEOPLE = {"value": [
    {"id": "AAD-LS-0001", "displayName": "Leonardo Salcuni",
     "scoredEmailAddresses": [{"address": "leonardo.salcuni@iseo.com"}]},
    {"id": "AAD-AV-0002", "displayName": "Anna Verdi",
     "scoredEmailAddresses": [{"address": "anna@cliente.it"}]},
]}
# stessa rubrica senza id AAD: il filtro lato Graph non è possibile e si deve
# ripiegare sulla scansione paginata dell'elenco chat
_PEOPLE_NO_ID = {"value": [
    {"displayName": "Leonardo Salcuni",
     "scoredEmailAddresses": [{"address": "leonardo.salcuni@iseo.com"}]},
]}


def _router(mappa, contatore=None):
    """GET finto instradato per URL: /me/people, /me/chats, /me/messages…"""
    def _get(url, headers=None, timeout=None, params=None):
        if contatore is not None:
            contatore.append(url)
        for chiave, resp in mappa.items():
            if chiave in url:
                return resp() if callable(resp) else resp
        return _Resp(200, {"value": []})
    return _get


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
def test_una_chiamata_per_fonte_mai_tipi_mescolati(monkeypatch):
    """Graph combina solo driveItem/listItem: message e chatMessage vanno
    richiesti SEPARATAMENTE, altrimenti risponde HTTP 400. Una chiamata per
    fonte, mai entityTypes mescolati."""
    chiamate = []

    def fake_post(url, headers=None, json=None, timeout=None):
        chiamate.append(json["requests"][0]["entityTypes"])
        return _Resp(200, {"value": []})

    import requests
    monkeypatch.setattr(requests, "post", fake_post)
    m = m365_search.M365Search({"client_id": "c", "tenant_id": "t", "token_path": "/tmp/x.json"})
    m.tm._data = {"access_token": "T", "expires_at": 9e9}

    m.search("contratto", ["sharepoint"])
    assert chiamate == [["driveItem", "listItem"]]

    chiamate.clear()
    m.search("contratto", ["sharepoint", "mail", "teams"])
    assert len(chiamate) == 3                       # una per fonte
    assert ["driveItem", "listItem"] in chiamate
    assert ["message"] in chiamate and ["chatMessage"] in chiamate
    for ent in chiamate:                            # mai mescolati
        assert not ({"message", "chatMessage"} & set(ent) and
                    {"driveItem", "listItem"} & set(ent))


def test_fonte_in_errore_non_azzera_le_altre(monkeypatch):
    """Se Teams fallisce, SharePoint risponde comunque e il fallimento è
    dichiarato nel contesto: mai un silenzio che sembra 'nessun risultato'."""
    import requests

    def fake_post(url, headers=None, json=None, timeout=None):
        ent = json["requests"][0]["entityTypes"]
        if "chatMessage" in ent:
            return _Resp(400, {"error": {"message": "entity type not supported"}})
        return _Resp(200, _GRAPH_HITS)

    monkeypatch.setattr(requests, "post", fake_post)
    m = m365_search.M365Search({"client_id": "c", "tenant_id": "t", "token_path": "/tmp/x.json"})
    m.tm._data = {"access_token": "T", "expires_at": 9e9}
    testo, rif = m.search("q", ["sharepoint", "teams"])
    assert "[SharePoint — Contratto quadro.docx]" in testo      # l'altra fonte funziona
    assert "fonti NON interrogate" in testo and "teams" in testo
    assert "entity type not supported" in testo                 # dettaglio Graph riportato
    assert rif


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


_CHATS = {"value": [
    {"id": "CID_A", "topic": "",
     "lastMessagePreview": {"id": "M_A", "createdDateTime": "2026-09-04T17:30:00Z",
                            "from": {"user": {"displayName": "Leonardo Salcuni"}},
                            "body": {"content": "<p>Ci vediamo <b>lunedì</b> per il piano manufacturing.</p>"}}},
    {"id": "CID_B", "topic": "Progetto NIS2",
     "lastMessagePreview": {"id": "M_B", "createdDateTime": "2026-09-05T08:00:00Z",
                            "from": {"user": {"displayName": "Samir"}},
                            "body": {"content": "VM riavviata, tutto ok."}}},
    {"id": "CID_C", "topic": "",
     "lastMessagePreview": {"id": "M_C", "createdDateTime": "2026-09-03T10:00:00Z",
                            "from": {}, "body": {"content": "utente aggiunto"}}},
]}


def test_teams_chat_recenti_per_domande_di_recency(monkeypatch):
    """Caso reale: la ricerca full-text su chatMessage non trova i messaggi di
    una persona (il suo nome non è nel testo). Le domande di recency devono
    essere completate con l'ultimo messaggio delle chat."""
    import requests
    monkeypatch.setattr(requests, "post", lambda *a, **k: _Resp(200, {"value": []}))
    monkeypatch.setattr(requests, "get", _router(
        {"/me/people": _Resp(200, {"value": []}), "/me/chats": _Resp(200, _CHATS)}))
    m = m365_search.M365Search({"client_id": "c", "tenant_id": "t", "token_path": "/tmp/x.json"})
    m.tm._data = {"access_token": "T", "expires_at": 9e9}
    testo, rif = m.search("cosa mi hanno scritto nell'ultimo messaggio?", ["teams"])
    assert "Leonardo Salcuni" in testo and "Ci vediamo lunedì" in testo
    assert "<b>" not in testo                      # HTML ripulito
    assert "Progetto NIS2" in testo                # topic della chat riportato
    assert "utente aggiunto" not in testo          # messaggi di sistema esclusi
    # ordinamento dal più recente
    assert testo.index("Samir") < testo.index("Leonardo Salcuni")
    # riferimenti scaricabili con chat_id valorizzato
    t = [r for r in rif if r["kind"] == "teams"]
    assert t and all(r["chat_id"] for r in t)


def test_teams_una_sola_chiamata_bounded(monkeypatch):
    import requests
    chiamate = []

    monkeypatch.setattr(requests, "post", lambda *a, **k: _Resp(200, {"value": []}))
    monkeypatch.setattr(requests, "get", _router(
        {"/me/people": _Resp(200, {"value": []}), "/me/chats": _Resp(200, _CHATS)}, chiamate))
    m = m365_search.M365Search({"client_id": "c", "tenant_id": "t", "token_path": "/tmp/x.json"})
    m.tm._data = {"access_token": "T", "expires_at": 9e9}
    m.search("ultimo messaggio", ["teams"])
    chat_calls = [u for u in chiamate if "/me/chats" in u]
    assert len(chat_calls) == 1                    # nessuna enumerazione dei messaggi
    assert "lastMessagePreview" in chat_calls[0]
    assert f"$top={m365_search.CHAT_RECENTI}" in chat_calls[0]


def test_teams_recenti_non_scatta_se_la_ricerca_basta(monkeypatch):
    """Domanda NON di recency e senza persona, con risultati dalla ricerca:
    nessuna chiamata supplementare alle chat, niente contesto gonfiato."""
    import requests
    urls = []
    monkeypatch.setattr(requests, "post", lambda *a, **k: _Resp(200, _GRAPH_HITS))
    monkeypatch.setattr(requests, "get", _router(
        {"/me/people": _Resp(200, {"value": []}), "/me/chats": _Resp(200, _CHATS)}, urls))
    m = m365_search.M365Search({"client_id": "c", "tenant_id": "t", "token_path": "/tmp/x.json"})
    m.tm._data = {"access_token": "T", "expires_at": 9e9}
    m.search("clausola di adeguamento del contratto", ["teams"])
    assert not any("/me/chats" in u for u in urls)


def test_teams_recenti_errore_dichiarato(monkeypatch):
    import requests
    monkeypatch.setattr(requests, "post", lambda *a, **k: _Resp(200, {"value": []}))
    monkeypatch.setattr(requests, "get",
                        lambda *a, **k: _Resp(403, {"error": {"message": "no Chat.Read"}}))
    m = m365_search.M365Search({"client_id": "c", "tenant_id": "t", "token_path": "/tmp/x.json"})
    m.tm._data = {"access_token": "T", "expires_at": 9e9}
    testo, _ = m.search("ultimo messaggio", ["teams"])
    assert "permessi insufficienti per le chat" in testo and "Chat.Read" in testo
    assert "ricollega l'account" in testo


_CHATS_MEMBRI = {"value": [
    {"id": "CID_LS", "topic": "",
     "members": [{"displayName": "Leonardo Salcuni", "email": "leonardo.salcuni@iseo.com"},
                 {"displayName": "Marco Bonometti", "email": "marco.bonometti@iseo.com"}],
     "lastMessagePreview": {"createdDateTime": "2026-09-04T17:30:00Z"}},
    {"id": "CID_X", "topic": "Altro",
     "members": [{"displayName": "Samir", "email": "samir@iseo.com"}],
     "lastMessagePreview": {"createdDateTime": "2026-09-05T08:00:00Z"}},
]}
_MSG_CHAT = {"value": [
    {"id": "M1", "createdDateTime": "2026-09-04T17:30:00Z",
     "from": {"user": {"displayName": "Leonardo Salcuni"}},
     "body": {"content": "<p>Ci vediamo <b>lunedì</b> per il piano manufacturing.</p>"}},
    {"id": "M2", "createdDateTime": "2026-09-04T17:00:00Z",
     "from": {"user": {"displayName": "Marco Bonometti"}},
     "body": {"content": "ok perfetto"}},
]}
_MAIL_DA = {"value": [
    {"id": "E1", "subject": "Allineamento ISMS", "receivedDateTime": "2026-09-02T09:00:00Z",
     "from": {"emailAddress": {"name": "Leonardo Salcuni", "address": "leonardo.salcuni@iseo.com"}},
     "bodyPreview": "Ti mando il punto sulle responsabilità."},
]}


def test_persona_risolta_solo_se_nominata(monkeypatch):
    import requests
    monkeypatch.setattr(requests, "get", _router({"/me/people": _Resp(200, _PEOPLE)}))
    m = m365_search.M365Search({"client_id": "c", "tenant_id": "t", "token_path": "/tmp/x.json"})
    p = m._risolvi_persona("T", "cosa mi ha scritto leonardo salcuni?")
    assert p and p["mail"] == "leonardo.salcuni@iseo.com"
    # la ricerca people è fuzzy: se il nome NON compare nella domanda, si scarta
    assert m._risolvi_persona("T", "come va il progetto NIS2?") is None


def test_people_senza_permesso_degrada(monkeypatch):
    import requests
    monkeypatch.setattr(requests, "get",
                        _router({"/me/people": _Resp(403, {"error": {"message": "no People.Read"}})}))
    m = m365_search.M365Search({"client_id": "c", "tenant_id": "t", "token_path": "/tmp/x.json"})
    assert m._risolvi_persona("T", "cosa scrive leonardo salcuni") is None   # nessuna eccezione


def test_teams_recupero_mirato_per_persona(monkeypatch):
    """Il caso che la ricerca full-text NON risolve: i messaggi di una persona
    non contengono il suo nome."""
    import requests
    urls = []
    monkeypatch.setattr(requests, "post", lambda *a, **k: _Resp(200, {"value": []}))
    monkeypatch.setattr(requests, "get", _router({
        "/me/people": _Resp(200, _PEOPLE),
        "/messages": _Resp(200, _MSG_CHAT),
        "/me/chats": _Resp(200, _CHATS_MEMBRI),
    }, urls))
    m = m365_search.M365Search({"client_id": "c", "tenant_id": "t", "token_path": "/tmp/x.json"})
    m.tm._data = {"access_token": "T", "expires_at": 9e9}
    testo, rif = m.search("cosa mi ha scritto leonardo salcuni nell'ultimo messaggio?", ["teams"])
    assert "recupero MIRATO su «Leonardo Salcuni»" in testo
    assert "Ci vediamo lunedì per il piano manufacturing." in testo
    assert "ok perfetto" not in testo                  # solo i messaggi SUOI
    assert any(u for u in urls if "/me/chats/CID_LS/messages" in u)   # solo la chat giusta
    assert not any("CID_X/messages" in u for u in urls)
    t = [r for r in rif if r["kind"] == "teams"]
    assert t and t[0]["chat_id"] == "CID_LS"


def test_mail_recupero_mirato_per_mittente(monkeypatch):
    import requests
    urls = []
    monkeypatch.setattr(requests, "get", _router({
        "/me/people": _Resp(200, _PEOPLE),
        "/me/messages": _Resp(200, _MAIL_DA),
    }, urls))
    m = m365_search.M365Search({"client_id": "c", "tenant_id": "t", "token_path": "/tmp/x.json"})
    m.tm._data = {"access_token": "T", "expires_at": 9e9}
    testo, rif = m.search("ultima mail di leonardo salcuni", ["mail"])
    assert "Allineamento ISMS" in testo and "responsabilità" in testo
    chiamata = next(u for u in urls if "/me/messages" in u)
    assert "from/emailAddress/address eq 'leonardo.salcuni@iseo.com'" in chiamata
    assert "$orderby=receivedDateTime desc" in chiamata      # per data, non rilevanza
    assert rif and rif[0]["kind"] == "mail" and rif[0]["id"] == "E1"


def test_mail_ripiego_su_ordinamento_troppo_complesso(monkeypatch):
    """Exchange rifiuta filtro sul mittente + ordinamento per data insieme
    («The restriction or sort order is too complex»). Il connettore ripiega su
    filtro senza ordinamento e riordina lato proprio: stesso risultato."""
    import requests
    urls = []
    fuori_ordine = {"value": [
        {"id": "E_OLD", "subject": "Vecchia", "receivedDateTime": "2025-05-16T09:00:00Z",
         "from": {"emailAddress": {"name": "Leonardo Salcuni", "address": "leonardo.salcuni@iseo.com"}},
         "bodyPreview": "vecchio messaggio"},
        {"id": "E_NEW", "subject": "Recente", "receivedDateTime": "2026-09-02T09:00:00Z",
         "from": {"emailAddress": {"name": "Leonardo Salcuni", "address": "leonardo.salcuni@iseo.com"}},
         "bodyPreview": "messaggio recente"},
    ]}

    def fake_get(url, headers=None, timeout=None, params=None):
        if "/me/people" in url:
            return _Resp(200, _PEOPLE)
        urls.append(url)
        if "$orderby" in url:
            return _Resp(400, {"error": {"message":
                "The restriction or sort order is too complex for this operation."}})
        return _Resp(200, fuori_ordine)

    monkeypatch.setattr(requests, "get", fake_get)
    m = m365_search.M365Search({"client_id": "c", "tenant_id": "t", "token_path": "/tmp/x.json"})
    m.tm._data = {"access_token": "T", "expires_at": 9e9}
    testo, rif = m.search("ultima mail di leonardo salcuni", ["mail"])
    assert len(urls) == 2                                   # tentativo + ripiego
    assert "$orderby" in urls[0] and "$orderby" not in urls[1]
    assert "$filter" in urls[1]
    assert testo.index("Recente") < testo.index("Vecchia")  # riordino locale
    assert rif and rif[0]["id"] == "E_NEW"


def test_mail_recency_senza_persona(monkeypatch):
    import requests
    urls = []
    monkeypatch.setattr(requests, "get", _router({
        "/me/people": _Resp(200, {"value": []}),
        "/me/messages": _Resp(200, _MAIL_DA),
    }, urls))
    m = m365_search.M365Search({"client_id": "c", "tenant_id": "t", "token_path": "/tmp/x.json"})
    m.tm._data = {"access_token": "T", "expires_at": 9e9}
    testo, _ = m.search("ultime mail ricevute", ["mail"])
    chiamata = next(u for u in urls if "/me/messages" in u)
    assert "$orderby=receivedDateTime desc" in chiamata and "$filter" not in chiamata
    assert "Allineamento ISMS" in testo


def test_sharepoint_persona_usa_kql_author(monkeypatch):
    import requests
    corpi = []
    monkeypatch.setattr(requests, "get", _router({"/me/people": _Resp(200, _PEOPLE)}))

    def fake_post(url, headers=None, json=None, timeout=None):
        corpi.append(json["requests"][0]["query"]["queryString"])
        return _Resp(200, _GRAPH_HITS)

    monkeypatch.setattr(requests, "post", fake_post)
    m = m365_search.M365Search({"client_id": "c", "tenant_id": "t", "token_path": "/tmp/x.json"})
    m.tm._data = {"access_token": "T", "expires_at": 9e9}
    m.search("documenti di leonardo salcuni", ["sharepoint"])
    assert corpi and 'author:"Leonardo Salcuni"' in corpi[0]


def test_nessun_risultato_per_persona_dichiarato(monkeypatch):
    import requests
    monkeypatch.setattr(requests, "get", _router({
        "/me/people": _Resp(200, _PEOPLE),
        "/me/messages": _Resp(200, {"value": []}),
    }))
    m = m365_search.M365Search({"client_id": "c", "tenant_id": "t", "token_path": "/tmp/x.json"})
    m.tm._data = {"access_token": "T", "expires_at": 9e9}
    testo, rif = m.search("ultima mail di leonardo salcuni", ["mail"])
    assert "nessun contenuto trovato da Leonardo Salcuni" in testo and rif == []


def test_scope_include_people_read():
    assert "People.Read" in m365_search.M365_SCOPE
    for s_ in ("Mail.Read", "Chat.Read", "Sites.Read.All", "Files.Read.All"):
        assert s_ in m365_search.M365_SCOPE


def test_chats_ordinate_e_paginate(monkeypatch):
    """Senza ordinamento Graph non garantisce che le prime chat siano le più
    recenti: una conversazione poteva restare invisibile (caso Salcuni)."""
    import requests
    urls = []
    pagina2 = {"value": [{"id": "CID_Z", "topic": "",
                          "lastMessagePreview": {"id": "MZ",
                              "createdDateTime": "2026-08-01T09:00:00Z",
                              "from": {"user": {"displayName": "Zeta"}},
                              "body": {"content": "vecchio ma presente"}}}]}
    stato = {"n": 0}

    def fake_get(url, headers=None, timeout=None, params=None):
        urls.append(url)
        if "/me/people" in url:
            return _Resp(200, {"value": []})
        stato["n"] += 1
        if stato["n"] == 1:
            d = dict(_CHATS)
            d["@odata.nextLink"] = "https://graph.microsoft.com/v1.0/me/chats?page=2"
            return _Resp(200, d)
        return _Resp(200, pagina2)

    monkeypatch.setattr(requests, "post", lambda *a, **k: _Resp(200, {"value": []}))
    monkeypatch.setattr(requests, "get", fake_get)
    m = m365_search.M365Search({"client_id": "c", "tenant_id": "t", "token_path": "/tmp/x.json"})
    m.tm._data = {"access_token": "T", "expires_at": 9e9}
    testo, _ = m.search("ultimi messaggi", ["teams"])
    prima = next(u for u in urls if "/me/chats" in u)
    assert "$orderby=lastMessagePreview/createdDateTime desc" in prima
    assert "vecchio ma presente" in testo          # la seconda pagina è stata letta


def test_teams_persona_filtro_per_partecipante(monkeypatch):
    """Prima si tenta il filtro lato Graph sui membri: preciso e immediato."""
    import requests
    params_visti = []

    def fake_get(url, headers=None, timeout=None, params=None):
        if "/me/people" in url:
            return _Resp(200, _PEOPLE)
        if "/messages" in url:
            return _Resp(200, _MSG_CHAT)
        params_visti.append(params or {})
        return _Resp(200, _CHATS_MEMBRI)

    monkeypatch.setattr(requests, "post", lambda *a, **k: _Resp(200, {"value": []}))
    monkeypatch.setattr(requests, "get", fake_get)
    m = m365_search.M365Search({"client_id": "c", "tenant_id": "t", "token_path": "/tmp/x.json"})
    m.tm._data = {"access_token": "T", "expires_at": 9e9}
    testo, _ = m.search("cosa mi ha scritto leonardo salcuni?", ["teams"])
    assert params_visti and "$filter" in params_visti[0]
    assert "aadUserConversationMember" in params_visti[0]["$filter"]
    assert "Ci vediamo lunedì" in testo


def test_teams_persona_senza_id_ripiega_su_scansione(monkeypatch):
    """Senza id AAD il filtro non è possibile: si scandisce l'elenco chat e la
    persona va comunque trovata (per email o nome del partecipante)."""
    import requests
    urls = []

    def fake_get(url, headers=None, timeout=None, params=None):
        urls.append(url)
        if "/me/people" in url:
            return _Resp(200, _PEOPLE_NO_ID)
        if "/messages" in url:
            return _Resp(200, _MSG_CHAT)
        return _Resp(200, _CHATS_MEMBRI)

    monkeypatch.setattr(requests, "post", lambda *a, **k: _Resp(200, {"value": []}))
    monkeypatch.setattr(requests, "get", fake_get)
    m = m365_search.M365Search({"client_id": "c", "tenant_id": "t", "token_path": "/tmp/x.json"})
    m.tm._data = {"access_token": "T", "expires_at": 9e9}
    testo, _ = m.search("cosa mi ha scritto leonardo salcuni?", ["teams"])
    assert "Ci vediamo lunedì" in testo
    assert any("$orderby" in u for u in urls if "/me/chats" in u)   # scansione ordinata


def test_estrazione_nome_ignora_articoli_elisi():
    """Il bug che mandava a vuoto la rubrica: da «nell'ultimo» si ricavava un
    finto pezzo di nome, e la ricerca in rubrica non trovava più nessuno."""
    m = m365_search.M365Search({"client_id": "c", "tenant_id": "t", "token_path": "/tmp/x.json"})
    q = "mi dici cosa scrive in teams leonardo salcuni nell'ultimo messaggio che mi ha inviato?"
    assert m._termini_nome(q) == "leonardo salcuni"
    assert m._token_nome("qual e' la clausola dell'accordo?") == ["clausola", "accordo"]
    # un singolo termine residuo non è un nome: la nota di assenza NON si emette
    assert len(m._token_nome("ultimi messaggi ricevuti")) < 2


def test_persona_individuata_fra_i_partecipanti(monkeypatch):
    """Se la rubrica non conosce la persona, la si individua fra i PARTECIPANTI
    delle chat: la ricerca mirata funziona lo stesso."""
    import requests

    def fake_get(url, headers=None, timeout=None, params=None):
        if "/me/people" in url:
            return _Resp(200, {"value": []})        # rubrica muta
        if "/messages" in url:
            return _Resp(200, _MSG_CHAT)
        return _Resp(200, _CHATS_MEMBRI)

    monkeypatch.setattr(requests, "post", lambda *a, **k: _Resp(200, {"value": []}))
    monkeypatch.setattr(requests, "get", fake_get)
    m = m365_search.M365Search({"client_id": "c", "tenant_id": "t", "token_path": "/tmp/x.json"})
    m.tm._data = {"access_token": "T", "expires_at": 9e9}
    testo, rif = m.search("cosa mi ha scritto leonardo salcuni nell'ultimo messaggio?", ["teams"])
    assert "Ci vediamo lunedì" in testo
    assert "ok perfetto" not in testo               # solo i suoi messaggi
    assert rif and rif[0]["chat_id"] == "CID_LS"


def test_assenza_accertata_non_confusa_con_limite(monkeypatch):
    """Scansionati TUTTI i partecipanti senza trovarlo: è un dato accertato,
    e va detto così — non come «forse non riesco a cercare»."""
    import requests

    def fake_get(url, headers=None, timeout=None, params=None):
        if "/me/people" in url:
            return _Resp(200, {"value": []})
        return _Resp(200, _CHATS_MEMBRI)

    monkeypatch.setattr(requests, "post", lambda *a, **k: _Resp(200, {"value": []}))
    monkeypatch.setattr(requests, "get", fake_get)
    m = m365_search.M365Search({"client_id": "c", "tenant_id": "t", "token_path": "/tmp/x.json"})
    m.tm._data = {"access_token": "T", "expires_at": 9e9}
    testo, _ = m.search("cosa mi ha scritto mario rossi nell'ultimo messaggio?", ["teams"])
    assert "nessun partecipante corrispondente a «mario rossi»" in testo
    assert "chat esaminate" in testo and "persone distinte" in testo
    assert "dato accertato" in testo


def test_people_non_disponibile_viene_dichiarato(monkeypatch):
    """Se la rubrica non è interrogabile, l'assenza di messaggi di una persona
    NON deve sembrare un'assenza di fatti: va detto che la ricerca mirata non
    è mai partita, con il rimedio."""
    import requests

    def fake_get(url, headers=None, timeout=None, params=None):
        if "/me/people" in url:
            return _Resp(403, {"error": {"message": "Insufficient privileges"}})
        return _Resp(200, _CHATS)

    monkeypatch.setattr(requests, "post", lambda *a, **k: _Resp(200, {"value": []}))
    monkeypatch.setattr(requests, "get", fake_get)
    m = m365_search.M365Search({"client_id": "c", "tenant_id": "t", "token_path": "/tmp/x.json"})
    m.tm._data = {"access_token": "T", "expires_at": 9e9}
    testo, _ = m.search("cosa mi ha scritto leonardo salcuni?", ["teams"])
    assert "ricerca MIRATA per persona non è disponibile" in testo
    assert "People.Read" in testo and "ricollegare l'account" in testo
    assert "NON significa che non esistano" in testo


def test_teams_persona_senza_chat_lo_dichiara(monkeypatch):
    """Se non c'è scambio su Teams con quella persona va DETTO, non lasciato
    intendere come assenza di dati."""
    import requests

    def fake_get(url, headers=None, timeout=None, params=None):
        if "/me/people" in url:
            return _Resp(200, _PEOPLE)
        return _Resp(200, {"value": []})

    monkeypatch.setattr(requests, "post", lambda *a, **k: _Resp(200, {"value": []}))
    monkeypatch.setattr(requests, "get", fake_get)
    m = m365_search.M365Search({"client_id": "c", "tenant_id": "t", "token_path": "/tmp/x.json"})
    m.tm._data = {"access_token": "T", "expires_at": 9e9}
    testo, _ = m.search("cosa mi ha scritto leonardo salcuni su teams?", ["teams"])
    assert "nessun messaggio di Leonardo Salcuni" in testo
    assert "nessuna conversazione" in testo and "dichiaralo" in testo.lower()


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
    assert "fonti NON interrogate" in testo and "permessi insufficienti" in testo
    assert "ricollega" in testo
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

"""
Ricerca unificata Microsoft 365 (Incremento 11) — SharePoint, Posta, Teams.

Usa l'API di ricerca unificata di Graph (POST /search/query) con l'identità
DELEGATA dell'utente: ciascuno vede esclusivamente ciò a cui ha già accesso in
Microsoft 365. Nessuna elevazione di privilegio, nessun account di servizio.

Principio di minimizzazione, esplicito nel disegno:
  • al MODELLO va solo lo SNIPPET restituito da Graph più i metadati
    (mittente, data, oggetto) — mai il corpo integrale del messaggio;
  • il CORPO INTEGRALE è scaricabile dall'utente su richiesta, recuperato da
    Graph al momento del download e servito come file: non transita
    dall'API del modello.

Governance: kill-switch globale, interruttori per singola fonte e concessione
per-utente vivono in connectors.py/main.py — qui c'è solo il motore.
"""
import datetime
import html
import json
import re
import time
from pathlib import Path

GRAPH = "https://graph.microsoft.com/v1.0"
# Scope delegati: SharePoint (Sites.Read.All + Files.Read.All), Posta
# (Mail.Read), Teams (Chat.Read). offline_access per il refresh.
M365_SCOPE = ("Files.Read.All Sites.Read.All Mail.Read Chat.Read "
              "People.Read User.Read offline_access")

FONTI = ("sharepoint", "mail", "teams")
_ENTITY = {
    "sharepoint": ["driveItem", "listItem"],
    "mail": ["message"],
    "teams": ["chatMessage"],
}
# La ricerca Graph su chatMessage indicizza il TESTO del messaggio, non il
# mittente: "cosa mi ha scritto Leonardo" non trova nulla, perché nei suoi
# messaggi il suo nome non compare. Per queste domande serve l'elenco delle
# chat recenti con l'anteprima dell'ultimo messaggio (una sola chiamata).
_RECENTI_RE = re.compile(
    r"\b(ultim\w+|recent\w+|nuov\w+|scritto|scrive|inviato|manda\w*|"
    r"mandato|detto|ieri|oggi|stamattina|poco\s+fa|"
    r"last|latest|recent|wrote|sent|told|today|yesterday)\b", re.IGNORECASE)
CHAT_RECENTI = 50           # chat per pagina
CHAT_PAGINE = 4             # pagine massime (fino a 200 chat): bounded ma ampio
MSG_PER_CHAT = 20           # messaggi letti per chat individuata
# Parole da scartare quando si cerca il NOME di una persona nella domanda.
_STOP = {
    "cosa", "che", "chi", "come", "quando", "dove", "perche", "perché", "quale",
    "quali", "mi", "me", "ti", "ci", "si", "ha", "hai", "ho", "hanno", "sono",
    "del", "della", "dei", "delle", "nel", "nella", "nei", "con", "per", "tra",
    "fra", "the", "what", "who", "when", "where", "which", "did", "does", "has",
    "have", "was", "were", "from", "about", "last", "latest", "recent", "message",
    "messaggio", "messaggi", "mail", "email", "posta", "teams", "chat", "scritto",
    "scrive", "inviato", "invia", "mandato", "manda", "detto", "dice", "ultimo",
    "ultima", "ultimi", "ultime", "recente", "recenti", "dimmi", "dici", "sent",
    "wrote", "told", "documento", "documenti", "file", "sharepoint", "riassumi",
    "trova", "cerca", "cercami", "mostrami", "leggimi", "una", "uno", "gli", "lui",
    "lei", "loro", "quel", "quello", "questa", "questo", "nostro", "nostra",
    # articoli elisi e preposizioni articolate spezzate dall'apostrofo
    "nell", "dell", "sull", "all", "dall", "quell", "quest", "coll", "coi",
    "coni", "gli", "gia", "già", "gliel", "gliela", "coso", "gliene",
    "qual", "qualè", "sono", "essere", "stato", "stata", "molto", "poco",
    "anche", "solo", "ancora", "sempre", "mai", "piu", "più", "meno",
    "ricevuti", "ricevute", "ricevuto", "inviati", "inviate", "mandati",
    "arrivati", "arrivate", "letti", "nuovi", "nuove", "tutti", "tutte",
}
SNIPPET_MAX = 1200          # per singolo risultato, verso il modello
TESTO_MAX = 14000           # totale del blocco di contesto


def _log(msg: str) -> None:
    import sys
    print(f"[m365] {msg}", file=sys.stderr)


class TokenM365:
    """Token per-utente su file (percorso nel cfg: nessun globale condiviso,
    stessa scelta fatta per Power BI)."""

    def __init__(self, cfg: dict):
        self.client_id = cfg.get("client_id", "")
        self.tenant_id = cfg.get("tenant_id", "")
        self.path = Path(cfg.get("token_path", ""))
        self._data = {}
        try:
            if self.path.exists():
                self._data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            self._data = {}

    def _save(self, d: dict) -> None:
        try:
            self._data = d
            self.path.write_text(json.dumps(d, indent=2), encoding="utf-8")
        except Exception:
            pass

    def access_token(self) -> str:
        if not self._data:
            return ""
        tok = self._data.get("access_token", "")
        if tok and time.time() < self._data.get("expires_at", 0) - 300:
            return tok
        rt = self._data.get("refresh_token", "")
        if not rt:
            return tok
        try:
            import requests
            r = requests.post(
                f"https://login.microsoftonline.com/{self.tenant_id}/oauth2/v2.0/token",
                data={"client_id": self.client_id, "grant_type": "refresh_token",
                      "refresh_token": rt, "scope": M365_SCOPE}, timeout=20)
            resp = r.json()
            if "access_token" in resp:
                resp["expires_at"] = time.time() + resp.get("expires_in", 3600)
                self._save(resp)
                return resp["access_token"]
            _log(f"refresh FALLITO: {str(resp.get('error_description', resp))[:180]}")
        except Exception as e:
            _log(f"refresh ECCEZIONE: {e}")
        return tok


def _strip_html(s: str) -> str:
    s = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", s or "", flags=re.S | re.I)
    s = re.sub(r"<br\s*/?>|</p>|</div>|</tr>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)
    s = re.sub(r"[ \t]+", " ", s)
    # i tag rimossi lasciano spazi prima della punteggiatura ("riavviata .")
    s = re.sub(r"\s+([,.;:!?%»)\]])", r"\1", s)
    s = re.sub(r"([«(\[])\s+", r"\1", s)
    return re.sub(r"\n{3,}", "\n\n", s).strip()


def _data_breve(iso: str) -> str:
    if not iso:
        return ""
    try:
        return datetime.datetime.fromisoformat(
            iso.replace("Z", "+00:00")).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(iso)[:16]


class M365Search:
    """Ricerca unificata su SharePoint, Posta e Teams."""

    def __init__(self, cfg: dict):
        self.cfg = cfg or {}
        self.tm = TokenM365(self.cfg)
        self._people_errore = ""

    # ── ricerca ─────────────────────────────────────────────
    def search(self, query: str, fonti: list, max_results: int = 5) -> tuple:
        """Ritorna (testo_per_modello, riferimenti).

        Strategia a tre vie, per fonte (Graph non consente di combinare
        message/chatMessage con driveItem: una chiamata per fonte, sempre):
          1. se la domanda nomina una PERSONA nota, recupero MIRATO per
             mittente/partecipante — la ricerca full-text non lo farebbe mai,
             perché indicizza il testo e non chi ha scritto;
          2. se la domanda è di RECENCY, elenco per data decrescente;
          3. altrimenti ricerca full-text classica.
        Una fonte in errore non azzera le altre: il fallimento è dichiarato."""
        fonti = [f for f in (fonti or []) if f in FONTI]
        if not fonti:
            return "", []
        tok = self.tm.access_token()
        if not tok:
            return "[Microsoft 365] Non connesso: collega il connettore dalla pagina Connessioni.", []

        recency = bool(_RECENTI_RE.search(query or ""))
        self._people_errore = ""
        persona = self._risolvi_persona(tok, query)

        blocchi, riferimenti, errori = [], [], []
        for f in fonti:
            try:
                if f == "mail":
                    b, r, e = self._mail(tok, query, persona, recency, max_results)
                elif f == "teams":
                    b, r, e = self._teams(tok, query, persona, recency, max_results)
                else:
                    b, r, e = self._sharepoint(tok, query, persona, recency, max_results)
            except Exception as ex:
                _log(f"[{f}] ECCEZIONE: {ex}")
                b, r, e = [], [], f"errore interno ({str(ex)[:60]})"
            if e:
                errori.append(f"{f}: {e}")
            blocchi.extend(b)
            riferimenti.extend(r)

        testa, avviso_people = "", ""
        if not persona and self._people_errore:
            # la domanda nominava una persona ma la rubrica non è interrogabile:
            # senza questo avviso l'utente crede che i messaggi non esistano,
            # mentre in realtà la ricerca mirata non è mai partita.
            avviso_people = ("[Microsoft 365 — ATTENZIONE: la ricerca MIRATA per persona non è "
                     f"disponibile ({self._people_errore}). I risultati qui sotto vengono "
                     "da ricerca per parole ed elenchi recenti, quindi l'assenza di "
                             "messaggi di una persona NON significa che non esistano. "
                             "Dichiaralo esplicitamente nella risposta.]\n\n")
        testa = avviso_people
        if persona:
            testa = (f"[Microsoft 365 — recupero MIRATO su «{persona['nome']}»"
                     f" ({persona['mail']}): i risultati qui sotto sono i suoi "
                     "messaggi/documenti più recenti, non una ricerca per parole.]\n\n")
        coda = ""
        if errori:
            coda = ("\n\n[Microsoft 365 — fonti NON interrogate: "
                    + "; ".join(errori) + ". Dichiaralo nella risposta.]")
        if not blocchi:
            if avviso_people:
                return avviso_people.strip() + coda, []
            if persona:
                coda = (f"\n\n[Microsoft 365 — nessun contenuto trovato da "
                        f"{persona['nome']} nelle fonti attive." + coda[2:] if coda
                        else f"\n\n[Microsoft 365 — nessun contenuto trovato da "
                             f"{persona['nome']} nelle fonti attive.]")
            return (coda.strip() or ""), []
        _log(f"query={query[:60]!r} fonti={fonti} persona={persona['nome'] if persona else '-'} "
             f"recency={recency} risultati={len(blocchi)} errori={len(errori)}")
        return (testa + "\n\n".join(blocchi))[:TESTO_MAX] + coda, riferimenti

    # ── risoluzione della PERSONA (come fa Copilot) ─────────
    def _termini_nome(self, query: str) -> str:
        """Parole della domanda che possono essere un NOME. L'apostrofo separa
        (altrimenti "nell'ultimo" entrava intero nella ricerca in rubrica e la
        mandava a vuoto); gli articoli elisi sono scartati."""
        parole = re.findall(r"[A-Za-zÀ-ÿ]{3,}", query or "")
        utili = [p for p in parole if p.lower() not in _STOP]
        return " ".join(utili[:6])

    def _token_nome(self, query: str) -> list:
        return [p.lower() for p in self._termini_nome(query).split() if len(p) >= 3]

    def _risolvi_persona(self, tok: str, query: str):
        """Trova la persona nominata nella domanda usando la rubrica di
        rilevanza dell'utente (/me/people). Ritorna {nome, mail} o None.
        Degrada in silenzio (con log) se il permesso People.Read manca: le
        altre modalità continuano a funzionare."""
        termini = self._termini_nome(query)
        if not termini:
            return None
        try:
            import requests
            r = requests.get(f"{GRAPH}/me/people",
                             params={"$search": f'"{termini}"', "$top": "10"},
                             headers={"Authorization": "Bearer " + tok}, timeout=25)
        except Exception as e:
            _log(f"[people] ECCEZIONE: {e}")
            self._people_errore = f"rubrica non raggiungibile ({str(e)[:60]})"
            return None
        if r.status_code >= 400:
            det = _dettaglio(r)
            _log(f"[people] HTTP {r.status_code}: {det} "
                 "(serve People.Read; senza, il recupero per persona è disattivo)")
            if r.status_code in (401, 403):
                self._people_errore = ("manca il permesso delegato People.Read, oppure "
                                       "l'account è stato collegato prima della concessione: "
                                       "serve il consenso amministratore e poi disconnettere "
                                       "e ricollegare l'account dalla pagina Connessioni")
            else:
                self._people_errore = f"errore HTTP {r.status_code} dalla rubrica"
            return None
        ql = (query or "").lower()
        for p in (r.json() or {}).get("value", []):
            nome = p.get("displayName") or ""
            pezzi = [x.lower() for x in re.split(r"[\s,]+", nome) if len(x) >= 3]
            if not pezzi:
                continue
            # richiede che almeno un pezzo del nome compaia DAVVERO nella
            # domanda: la ricerca people è fuzzy e va disciplinata
            if not any(x in ql for x in pezzi):
                continue
            mails = p.get("scoredEmailAddresses") or []
            indirizzo = (mails[0].get("address") if mails else "") or ""
            if not indirizzo:
                continue
            _log(f"[people] persona risolta: {nome} <{indirizzo}>")
            return {"nome": nome, "mail": indirizzo, "id": p.get("id") or ""}
        return None

    def _persona_da_chat(self, chats: list, query: str):
        """Individua la persona fra i PARTECIPANTI delle chat già scaricate:
        non dipende dalla rubrica, quindi funziona anche se /me/people non
        conosce quella persona. Richiede che TUTTI i token del nome cercato
        compaiano nel nome del partecipante: niente omonimie approssimate."""
        token = self._token_nome(query)
        if not token:
            return None
        for ch in chats:
            for mem in (ch.get("members") or []):
                dn = (mem.get("displayName") or "").lower()
                if not dn:
                    continue
                if all(t in dn for t in token):
                    persona = {"nome": mem.get("displayName") or "",
                               "mail": (mem.get("email") or ""),
                               "id": mem.get("userId") or ""}
                    _log(f"[persona-da-chat] individuata: {persona['nome']}")
                    return persona
        return None

    # ── POSTA ───────────────────────────────────────────────
    def _mail(self, tok, query, persona, recency, n):
        """Posta mirata per mittente o per data.

        Exchange NON accetta filtro su una proprietà annidata (il mittente) e
        ordinamento per data nella stessa richiesta: risponde 400 «The
        restriction or sort order is too complex». Si tenta con l'ordinamento e,
        su quell'errore, si ripiega su filtro senza ordinamento con una finestra
        più ampia, riordinando poi lato nostro. Nessun silenzio: l'esito è lo
        stesso, la strada cambia."""
        import requests
        h = {"Authorization": "Bearer " + tok}
        sel = "id,subject,receivedDateTime,from,bodyPreview"
        top = max(1, min(int(n or 5), 15))
        if persona:
            indirizzo = persona["mail"].replace("'", "''")
            filtro = f"from/emailAddress/address eq '{indirizzo}'"
            tentativi = [
                f"{GRAPH}/me/messages?$filter={filtro}"
                f"&$orderby=receivedDateTime desc&$top={top}&$select={sel}",
                # ripiego: niente $orderby, finestra più ampia, riordino locale
                f"{GRAPH}/me/messages?$filter={filtro}&$top={max(top, 50)}&$select={sel}",
            ]
        elif recency:
            tentativi = [f"{GRAPH}/me/messages?$orderby=receivedDateTime desc"
                         f"&$top={top}&$select={sel}"]
        else:
            return self._search_fonte("mail", query, tok, n)

        ultimo_err = ""
        dati = None
        for i, url in enumerate(tentativi):
            try:
                r = requests.get(url, headers=h, timeout=45)
            except Exception as e:
                return [], [], f"errore di rete ({str(e)[:60]})"
            if r.status_code < 400:
                dati = (r.json() or {}).get("value", [])
                if i > 0:
                    _log("[mail-mirato] ordinamento non supportato con il filtro: "
                         "ripiego riuscito, riordino locale")
                break
            det = _dettaglio(r)
            ultimo_err = f"HTTP {r.status_code} ({det or 'nessun dettaglio'})"
            _log(f"[mail-mirato] {ultimo_err}")
            if r.status_code in (401, 403):
                return [], [], ("permessi insufficienti — serve Mail.Read con consenso "
                                "amministratore, poi ricollega l'account")
            if r.status_code != 400:
                return [], [], ultimo_err
        if dati is None:
            return [], [], ultimo_err

        dati.sort(key=lambda m: m.get("receivedDateTime") or "", reverse=True)
        blocchi, rifs = [], []
        for m in dati[:top]:
            ea = ((m.get("from") or {}).get("emailAddress") or {})
            quando = _data_breve(m.get("receivedDateTime") or "")
            ogg = m.get("subject") or "(senza oggetto)"
            corpo = _strip_html(m.get("bodyPreview") or "")[:SNIPPET_MAX]
            blocchi.append(f"[Posta — {ogg}]\nDa: {ea.get('name','')} "
                           f"<{ea.get('address','')}> · {quando}\n{corpo}")
            rifs.append({"kind": "mail", "id": m.get("id") or "", "titolo": ogg,
                         "quando": quando, "da": ea.get("name", "")})
        return blocchi, rifs, ""

    # ── TEAMS ───────────────────────────────────────────────
    def _teams(self, tok, query, persona, recency, n):
        if persona:
            return self._teams_da_persona(tok, persona, n)
        token = self._token_nome(query)
        # La scansione dei partecipanti si fa solo quando la domanda parla di
        # messaggi scambiati ("cosa mi ha scritto…", "ultimo messaggio di…"):
        # su una domanda di contenuto ("clausola del contratto") resta la
        # ricerca full-text, senza chiamate inutili.
        if recency and token:
            chats, err = self._chats(tok, con_membri=True)
            if err and not chats:
                return [], [], err
            p2 = self._persona_da_chat(chats, query)
            if p2:
                return self._teams_da_persona(tok, p2, n, chats=chats)
            if len(token) < 2:
                # un solo termine che non corrisponde a nessuno: non è un nome
                # (es. "ultimi messaggi ricevuti"). Nessuna nota fuorviante.
                return self._teams_recenti(tok, chats=chats)
            nomi = sorted({(m.get("displayName") or "")
                           for ch in chats for m in (ch.get("members") or [])
                           if m.get("displayName")})
            nota = (f"[Teams — nessun partecipante corrispondente a «{' '.join(token)}» "
                    f"fra le {len(chats)} chat esaminate ({len(nomi)} persone distinte). "
                    "Con quella persona NON risulta alcuna conversazione su Teams: "
                    "dichiaralo come dato accertato, non come limite di ricerca.]")
            b, r, e = self._teams_recenti(tok, chats=chats)
            return ([nota] + b), r, e
        if recency:
            return self._teams_recenti(tok)
        b, r, e = self._search_fonte("teams", query, tok, n)
        if not b and not e:                 # full-text a vuoto: completa con le recenti
            return self._teams_recenti(tok)
        return b, r, e

    def _teams_da_persona(self, tok, persona, n, chats=None):
        """Chat con quella persona → suoi messaggi più recenti.
        Prima si tenta il FILTRO lato Graph sui partecipanti (preciso e
        immediato); se il tenant non lo supporta si ripiega sulla scansione
        paginata dell'elenco chat. Al massimo 3 chat interrogate."""
        import requests
        h = {"Authorization": "Bearer " + tok}
        mail_p = (persona.get("mail") or "").lower()
        nome_p = (persona.get("nome") or "").lower()
        uid_p = persona.get("id") or ""

        chats, err = (chats or []), ""
        if not chats and uid_p:
            filtro = ("members/any(m:m/microsoft.graph.aadUserConversationMember"
                      f"/userId eq '{uid_p}')")
            try:
                rf = requests.get(f"{GRAPH}/me/chats",
                                  params={"$expand": "members,lastMessagePreview",
                                          "$filter": filtro, "$top": str(CHAT_RECENTI)},
                                  headers=h, timeout=45)
                if rf.status_code < 400:
                    chats = (rf.json() or {}).get("value", [])
                    _log(f"[teams-persona] filtro per userId: {len(chats)} chat")
                else:
                    _log(f"[teams-persona] filtro non supportato "
                         f"(HTTP {rf.status_code}): scansione elenco")
            except Exception as e:
                _log(f"[teams-persona] filtro fallito: {e}")
        if not chats:
            chats, err = self._chats(tok, con_membri=True)
            if err and not chats:
                return [], [], err

        candidate = []
        for ch in chats:
            for mem in (ch.get("members") or []):
                em = (mem.get("email") or "").lower()
                dn = (mem.get("displayName") or "").lower()
                uid_m = (mem.get("userId") or "")
                if ((em and em == mail_p) or (dn and dn == nome_p)
                        or (uid_p and uid_m == uid_p)):
                    lmp = ch.get("lastMessagePreview") or {}
                    candidate.append((lmp.get("createdDateTime") or "",
                                      ch.get("id") or "", ch.get("topic") or ""))
                    break
        candidate.sort(reverse=True)
        blocchi, rifs = [], []
        for _q, chat_id, topic in candidate[:3]:
            try:
                rm = requests.get(f"{GRAPH}/me/chats/{chat_id}/messages"
                                  f"?$top={MSG_PER_CHAT}", headers=h, timeout=45)
            except Exception:
                continue
            if rm.status_code >= 400:
                _log(f"[teams-persona] messaggi HTTP {rm.status_code}: {_dettaglio(rm)}")
                continue
            msgs = (rm.json() or {}).get("value", [])
            msgs.sort(key=lambda m: m.get("createdDateTime") or "", reverse=True)
            presi = 0
            for m in msgs:
                u = ((m.get("from") or {}).get("user") or {})
                if (u.get("displayName") or "").lower() != nome_p:
                    continue
                corpo = _strip_html(((m.get("body") or {}).get("content") or ""))
                if not corpo:
                    continue
                quando = _data_breve(m.get("createdDateTime") or "")
                dove = f" · {topic}" if topic else ""
                blocchi.append(f"[Teams — {persona['nome']}{dove} · {quando}]\n"
                               f"{corpo[:SNIPPET_MAX]}")
                rifs.append({"kind": "teams", "id": m.get("id") or "",
                             "chat_id": chat_id,
                             "titolo": f"Messaggio Teams di {persona['nome']}",
                             "quando": quando, "da": persona["nome"]})
                presi += 1
                if presi >= max(1, min(int(n or 5), 10)):
                    break
        if not blocchi:
            _log(f"[teams-persona] nessun messaggio di {persona['nome']} "
                 f"(chat candidate: {len(candidate)}, chat esaminate: {len(chats)})")
            nota = (f"[Teams — nessun messaggio di {persona['nome']}: "
                    + (f"trovate {len(candidate)} conversazioni con lui/lei ma nessun "
                       "suo messaggio recente" if candidate
                       else f"nessuna conversazione con lui/lei fra le {len(chats)} "
                            "chat esaminate")
                    + ". Potrebbe non esserci scambio su Teams: dichiaralo.]")
            return [nota], [], ""
        return blocchi, rifs, ""

    # ── SHAREPOINT ──────────────────────────────────────────
    def _sharepoint(self, tok, query, persona, recency, n):
        termini = self._termini_nome(query)
        if recency and not termini:
            import requests
            try:
                r = requests.get(f"{GRAPH}/me/drive/recent",
                                 headers={"Authorization": "Bearer " + tok}, timeout=45)
            except Exception as e:
                return [], [], f"errore di rete ({str(e)[:60]})"
            if r.status_code >= 400:
                return self._search_fonte("sharepoint", query, tok, n)
            blocchi, rifs = [], []
            for it in ((r.json() or {}).get("value", []))[:max(1, min(int(n or 5), 10))]:
                nome = it.get("name") or "documento"
                quando = _data_breve(it.get("lastModifiedDateTime") or "")
                blocchi.append(f"[SharePoint — {nome}]{(' · ' + quando) if quando else ''}\n"
                               "(documento usato di recente)")
                rifs.append({"kind": "sharepoint", "id": it.get("id") or "",
                             "drive_id": ((it.get("parentReference") or {}).get("driveId") or ""),
                             "titolo": nome, "quando": quando,
                             "url": it.get("webUrl") or "", "sito": ""})
            return blocchi, rifs, ""
        q = query
        if persona:
            # documenti della persona: KQL author, con i termini della domanda
            q = f'author:"{persona["nome"]}"'
        return self._search_fonte("sharepoint", q, tok, n)

    def _search_fonte(self, fonte: str, query: str, tok: str, max_results: int):
        """Ricerca full-text su UNA fonte via /search/query. Graph combina solo
        driveItem/listItem: message e chatMessage vanno richiesti separatamente,
        altrimenti risponde HTTP 400. Ritorna (blocchi, riferimenti, errore)."""
        corpo = {"requests": [{
            "entityTypes": _ENTITY[fonte],
            "query": {"queryString": query},
            "from": 0,
            "size": max(1, min(int(max_results or 5), 25)),
        }]}
        try:
            import requests
            r = requests.post(f"{GRAPH}/search/query",
                              headers={"Authorization": "Bearer " + tok,
                                       "Content-Type": "application/json"},
                              json=corpo, timeout=45)
        except Exception as e:
            _log(f"[{fonte}] ECCEZIONE rete: {e}")
            return [], [], f"errore di rete ({str(e)[:60]})"

        if r.status_code >= 400:
            dettaglio = _dettaglio(r)
            _log(f"[{fonte}] HTTP {r.status_code}: {dettaglio}")
            if r.status_code == 403:
                return [], [], ("permessi insufficienti — servono i permessi delegati "
                                "con consenso amministratore, poi disconnetti e "
                                "ricollega l'account")
            if r.status_code == 401:
                return [], [], "sessione scaduta: disconnetti e ricollega l'account"
            return [], [], f"HTTP {r.status_code} da Graph ({dettaglio or 'nessun dettaglio'})"

        try:
            data = r.json()
        except Exception as e:
            return [], [], f"risposta non leggibile ({str(e)[:60]})"

        blocchi, rifs = [], []
        for gruppo in data.get("value", []):
            for hc in gruppo.get("hitsContainers", []):
                for hit in hc.get("hits", []):
                    voce = self._formatta(hit)
                    if not voce:
                        continue
                    testo, rif = voce
                    blocchi.append(testo)
                    if rif:
                        rifs.append(rif)
        return blocchi, rifs, ""

    def _chats(self, tok: str, con_membri: bool = False):
        """Elenco chat dell'utente, ORDINATE per data dell'ultimo messaggio e
        paginato (fino a CHAT_PAGINE pagine). Senza ordinamento Graph non
        garantisce che le prime siano le più recenti: era il motivo per cui una
        conversazione poteva restare invisibile. Ritorna (chats, errore)."""
        import requests
        h = {"Authorization": "Bearer " + tok}
        exp = "members,lastMessagePreview" if con_membri else "lastMessagePreview"
        url = (f"{GRAPH}/me/chats?$expand={exp}"
               f"&$orderby=lastMessagePreview/createdDateTime desc"
               f"&$top={CHAT_RECENTI}")
        out, pagine = [], 0
        while url and pagine < CHAT_PAGINE:
            try:
                r = requests.get(url, headers=h, timeout=45)
            except Exception as e:
                return out, f"errore di rete ({str(e)[:60]})"
            if r.status_code >= 400:
                det = _dettaglio(r)
                _log(f"[chats] HTTP {r.status_code}: {det}")
                if pagine == 0 and "orderby" in det.lower():
                    # alcuni tenant rifiutano l'ordinamento: ripiego non ordinato
                    _log("[chats] ordinamento non supportato: ripiego senza $orderby")
                    url = f"{GRAPH}/me/chats?$expand={exp}&$top={CHAT_RECENTI}"
                    pagine += 1
                    continue
                if r.status_code in (401, 403):
                    return out, ("permessi insufficienti per le chat — serve Chat.Read "
                                 "con consenso amministratore, poi ricollega l'account")
                return out, f"HTTP {r.status_code} ({det or 'nessun dettaglio'})"
            try:
                data = r.json() or {}
            except Exception as e:
                return out, f"risposta non leggibile ({str(e)[:60]})"
            out.extend(data.get("value", []))
            url = data.get("@odata.nextLink") or ""
            pagine += 1
        _log(f"[chats] esaminate {len(out)} chat in {pagine} pagine")
        return out, ""

    def _teams_recenti(self, tok: str, limite: int = CHAT_RECENTI, chats=None):
        """Ultimo messaggio di ciascuna chat recente dell'utente, dalla più
        recente. Nessuna enumerazione dei messaggi: solo l'anteprima."""
        if chats is None:
            chats, err = self._chats(tok)
            if err:
                return [], [], err

        righe = []
        for ch in chats:
            lmp = ch.get("lastMessagePreview") or {}
            if not lmp:
                continue
            mitt = (((lmp.get("from") or {}).get("user") or {}).get("displayName") or "")
            if not mitt:
                continue                     # messaggi di sistema: ignorati
            corpo = _strip_html(((lmp.get("body") or {}).get("content") or ""))
            if not corpo:
                continue
            quando_iso = lmp.get("createdDateTime") or ""
            righe.append({
                "quando_iso": quando_iso,
                "quando": _data_breve(quando_iso),
                "mitt": mitt,
                "corpo": corpo[:SNIPPET_MAX],
                "chat_id": ch.get("id") or "",
                "msg_id": lmp.get("id") or "",
                "topic": ch.get("topic") or "",
            })
        righe.sort(key=lambda x: x["quando_iso"], reverse=True)

        blocchi, rifs = [], []
        for x in righe:
            dove = f" · {x['topic']}" if x["topic"] else ""
            blocchi.append(f"[Teams — ultimo messaggio della chat con {x['mitt']}"
                           f"{dove} · {x['quando']}]\n{x['corpo']}")
            rifs.append({"kind": "teams", "id": x["msg_id"], "chat_id": x["chat_id"],
                         "titolo": f"Chat Teams con {x['mitt']}",
                         "quando": x["quando"], "da": x["mitt"]})
        if blocchi:
            _log(f"[teams-recenti] chat con anteprima: {len(blocchi)}")
        return blocchi, rifs, ""

    def _formatta(self, hit: dict):
        res = hit.get("resource") or {}
        tipo = (res.get("@odata.type") or "").split(".")[-1].lower()
        sunto = _strip_html(hit.get("summary") or "")[:SNIPPET_MAX]

        if tipo == "message":
            mitt = (((res.get("from") or {}).get("emailAddress") or {}).get("address") or "")
            nome = (((res.get("from") or {}).get("emailAddress") or {}).get("name") or "")
            ogg = res.get("subject") or "(senza oggetto)"
            quando = _data_breve(res.get("receivedDateTime") or "")
            rif = {"kind": "mail", "id": res.get("id") or "",
                   "titolo": ogg, "quando": quando, "da": nome or mitt}
            testo = (f"[Posta — {ogg}]\nDa: {nome} <{mitt}> · {quando}\n{sunto}")
            return testo, rif

        if tipo == "chatmessage":
            mitt = (((res.get("from") or {}).get("user") or {}).get("displayName") or "")
            quando = _data_breve(res.get("createdDateTime") or "")
            corpo = _strip_html(((res.get("body") or {}).get("content") or ""))[:SNIPPET_MAX]
            chat_id = res.get("chatId") or ""
            rif = {"kind": "teams", "id": res.get("id") or "", "chat_id": chat_id,
                   "titolo": f"Chat Teams del {quando}", "quando": quando, "da": mitt}
            testo = (f"[Teams — messaggio del {quando}]\nDa: {mitt}\n"
                     f"{corpo or sunto}")
            return testo, rif

        # driveItem / listItem → SharePoint
        nome = res.get("name") or (res.get("fields") or {}).get("title") or "documento"
        url = res.get("webUrl") or ""
        sito = ""
        try:
            sito = ((res.get("parentReference") or {}).get("siteId") or "").split(",")[0]
        except Exception:
            sito = ""
        quando = _data_breve(res.get("lastModifiedDateTime") or "")
        rif = {"kind": "sharepoint", "id": res.get("id") or "",
               "drive_id": ((res.get("parentReference") or {}).get("driveId") or ""),
               "titolo": nome, "quando": quando, "url": url, "sito": sito}
        testo = f"[SharePoint — {nome}]{(' · ' + quando) if quando else ''}\n{sunto}"
        return testo, rif

    # ── contenuto integrale (solo su richiesta dell'utente) ──
    def fetch_full(self, kind: str, ident: str, chat_id: str = "") -> tuple:
        """Scarica il contenuto INTEGRALE. Ritorna (bytes, filename, mime).
        NON passa mai dal modello: è un download diretto per l'utente."""
        tok = self.tm.access_token()
        if not tok:
            raise ValueError("Connettore Microsoft 365 non connesso.")
        import requests
        h = {"Authorization": "Bearer " + tok}

        if kind == "mail":
            r = requests.get(f"{GRAPH}/me/messages/{ident}/$value", headers=h, timeout=60)
            if r.status_code >= 400:
                raise ValueError(f"Messaggio non recuperabile (HTTP {r.status_code}).")
            meta = requests.get(f"{GRAPH}/me/messages/{ident}"
                                "?$select=subject,receivedDateTime", headers=h, timeout=30)
            ogg = "messaggio"
            if meta.status_code < 400:
                ogg = (meta.json().get("subject") or "messaggio")
            return r.content, _safe_name(ogg) + ".eml", "message/rfc822"

        if kind == "teams":
            if not chat_id:
                raise ValueError("Identificativo chat mancante per il messaggio Teams.")
            r = requests.get(f"{GRAPH}/me/chats/{chat_id}/messages/{ident}",
                             headers=h, timeout=45)
            if r.status_code >= 400:
                raise ValueError(f"Messaggio Teams non recuperabile (HTTP {r.status_code}).")
            m = r.json()
            mitt = (((m.get("from") or {}).get("user") or {}).get("displayName") or "")
            quando = _data_breve(m.get("createdDateTime") or "")
            corpo = _strip_html(((m.get("body") or {}).get("content") or ""))
            testo = (f"Messaggio Teams\nDa: {mitt}\nData: {quando}\n"
                     f"Chat: {chat_id}\n\n{corpo}\n")
            return testo.encode("utf-8"), _safe_name(f"Teams {quando} {mitt}") + ".txt", "text/plain"

        if kind == "sharepoint":
            drive_id = chat_id      # riuso del parametro per il driveId
            if not drive_id:
                raise ValueError("Identificativo drive mancante per il file SharePoint.")
            meta = requests.get(f"{GRAPH}/drives/{drive_id}/items/{ident}"
                                "?$select=name", headers=h, timeout=30)
            nome = "documento"
            if meta.status_code < 400:
                nome = meta.json().get("name") or "documento"
            r = requests.get(f"{GRAPH}/drives/{drive_id}/items/{ident}/content",
                             headers=h, timeout=120)
            if r.status_code >= 400:
                raise ValueError(f"File non scaricabile (HTTP {r.status_code}).")
            return r.content, nome, "application/octet-stream"

        raise ValueError(f"Tipo di contenuto non gestito: {kind}")


def _dettaglio(r) -> str:
    """Messaggio d'errore di Graph, per log e contesto: mai un nudo codice."""
    try:
        return str((r.json().get("error") or {}).get("message") or "")[:200]
    except Exception:
        return (getattr(r, "text", "") or "")[:200]


def _safe_name(s: str) -> str:
    s = re.sub(r"[^\w\s.-]", "", str(s or "")).strip()
    return (s or "contenuto")[:80]

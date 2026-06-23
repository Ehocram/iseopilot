"""
main.py — Applicazione FastAPI (ISEOPilot) — Incremento 2.

Autenticazione locale gestita dall'admin (sessioni con cookie firmato).
Tutte le pagine sono protette dal login, tranne /login e /healthz.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from starlette.middleware.sessions import SessionMiddleware
from starlette.background import BackgroundTask

from . import auth, connectors, docgen, i18n, knowledge, memory, store
from .connectors import (DEF_OD_CLIENT_ID, DEF_OD_TENANT_ID, DEF_DYN_CLIENT_ID,
                         DEF_DYN_TENANT_ID, DEF_DYN_RESOURCE_URL)
from .orchestrator import TONES, LANG_INSTR, stream_reply

BASE_DIR = Path(__file__).resolve().parent.parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

app = FastAPI(title="ISEOPilot")

# Sessione firmata. Richiede APP_SECRET_KEY (fail loudly se assente).
_secret = os.environ.get("APP_SECRET_KEY", "").strip()
if not _secret:
    raise RuntimeError("APP_SECRET_KEY non impostata: necessaria per firmare le sessioni.")
app.add_middleware(
    SessionMiddleware,
    secret_key=_secret,
    same_site="lax",
    https_only=os.environ.get("SESSION_HTTPS_ONLY", "0") == "1",
    max_age=60 * 60 * 8,  # 8 ore
)

app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


@app.on_event("startup")
def _startup():
    store.init_db()
    auth.bootstrap_admin()
    # Come l'app desktop: indicizza le cartelle configurate all'avvio, in
    # background (anche le share di rete), così sono subito ricercabili.
    knowledge.reindex_all_async()


def _ui_lang(request: Request, user: dict | None = None) -> str:
    """Lingua interfaccia: preferenza utente se loggato, altrimenti sessione, default 'it'."""
    if user:
        return i18n.normalize(store.get_user_setting(user["username"], "ui_lang", "it"))
    return i18n.normalize(request.session.get("ui_lang", "it"))


def _i18n_ctx(request: Request, user: dict | None = None) -> dict:
    lang = _ui_lang(request, user)
    return {
        "t": (lambda key: i18n.t(key, lang)),
        "ui_lang": lang,
        "next_path": request.url.path,
    }


def _ctx(request: Request, user: dict, **extra) -> dict:
    base = {"user": user["username"], "is_admin": bool(user["is_admin"]),
            "department": user.get("department") or "—"}
    base.update(_i18n_ctx(request, user))
    base.update(extra)
    return base


# ── Login / Logout ──────────────────────────────────────────
@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if auth.current_user(request):
        return RedirectResponse(url="/", status_code=303)
    return templates.TemplateResponse(request, "login.html",
                                      {"error": None, **_i18n_ctx(request)})


@app.post("/login", response_class=HTMLResponse)
def login_submit(request: Request, username: str = Form(...), password: str = Form(...)):
    user = auth.authenticate(username, password)
    if not user:
        return templates.TemplateResponse(
            request, "login.html",
            {"error": "Credenziali non valide o utenza disattivata.", **_i18n_ctx(request)},
            status_code=401,
        )
    request.session["user"] = user["username"]
    return RedirectResponse(url="/", status_code=303)


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/login", status_code=303)


@app.get("/ui-lang")
def set_ui_lang(request: Request):
    """Cambia la lingua dell'interfaccia (it/en). Persistente per utente se loggato."""
    to = i18n.normalize(request.query_params.get("to", "it"))
    nxt = request.query_params.get("next", "/")
    request.session["ui_lang"] = to
    user = auth.current_user(request)
    if user:
        store.set_user_setting(user["username"], "ui_lang", to)
    if not nxt.startswith("/"):
        nxt = "/"
    return RedirectResponse(url=nxt, status_code=303)


# ── Helper impostazioni motore ──────────────────────────────
def admin_settings() -> dict:
    return {
        "claude_api_key": store.get_setting("claude_api_key", ""),
        "claude_model": store.get_setting("claude_model", "claude-opus-4-8"),
        "claude_anonymize": store.get_setting("claude_anonymize", "1") == "1",
        "lm_url": store.get_setting("lm_url", ""),
        "lm_model": store.get_setting("lm_model", ""),
    }


def anon_names() -> list:
    raw = store.get_setting("anon_dictionary", "")
    return [n.strip() for n in raw.splitlines() if n.strip()]


# ── Chat ────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
def chat_page(request: Request):
    user = auth.current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    return templates.TemplateResponse(request, "chat.html", _ctx(
        request, user, tones=list(TONES.keys()), langs=list(LANG_INSTR.keys()),
    ))


class ChatRequest(BaseModel):
    messages: list[dict]
    engine: str = "claude"
    tone: str = "Aziendale formale"
    reply_lang: str = "Italiano"
    free_mode: bool = False
    session_id: str | None = None
    attachments: list[dict] = []


@app.post("/api/chat")
def api_chat(request: Request, body: ChatRequest):
    user = auth.current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Sessione scaduta. Effettua di nuovo l'accesso.")

    clean = []
    for m in body.messages[-20:]:
        role = m.get("role")
        content = (m.get("content") or "").strip()
        if role in ("user", "assistant") and content:
            clean.append({"role": role, "content": content})
    if not clean or clean[-1]["role"] != "user":
        raise HTTPException(status_code=400, detail="Nessun messaggio utente valido.")

    settings = admin_settings()
    settings["ai_engine"] = "lmstudio" if body.engine == "lmstudio" else "claude"
    settings["tone_key"] = body.tone if body.tone in TONES else "Aziendale formale"
    settings["reply_lang"] = body.reply_lang if body.reply_lang in LANG_INSTR else "Italiano"

    # Recupero conoscenza secondo i TOGGLE dell'utente (Connessioni): conoscenza
    # ChromaDB del dipartimento e/o cartelle del dipartimento. Scoping per area.
    # In MODALITÀ AI LIBERA non si consulta alcuna fonte: risposta da conoscenza generale.
    context = ""
    source_links: list[dict] = []
    uid = user["username"]
    query = clean[-1]["content"] if clean else ""

    # Allegati della conversazione: hanno priorità e valgono SEMPRE, anche in AI
    # libera (l'utente allega un file e chiede sintesi/elaborazione).
    attach_block = ""
    if body.attachments:
        ab = []
        for a in body.attachments[:20]:
            name = str(a.get("name", "allegato"))
            text = str(a.get("text", ""))[:12000]
            if text.strip():
                ab.append(f"[ALLEGATO: {name}]\n{text}")
        attach_block = "\n\n".join(ab)

    if not body.free_mode:
        try:
            dept = user.get("department") or ""
            use_kb = store.get_user_setting(uid, "use_kb", "1") == "1"
            use_folder = store.get_user_setting(uid, "use_folder", "1") == "1"
            # se ci sono allegati, arricchisci la query con le loro parole chiave
            search_q = query
            if attach_block:
                try:
                    from .engines.onedrive_search import _build_query as _kw
                    akw = _kw(attach_block[:2000])
                    if akw and akw.strip() and akw.strip() != query.strip():
                        search_q = (query + " " + akw).strip()
                except Exception:
                    pass
            parts = [knowledge.retrieve(dept, search_q, use_kb=use_kb, use_folder=use_folder)]
            if store.get_user_setting(uid, "use_onedrive", "0") == "1" and connectors.is_connected(uid, "onedrive"):
                od, od_links = connectors.search_with_links(uid, "onedrive", search_q, max_results=5)
                if od.strip():
                    parts.append("[OneDrive]\n" + od)
                source_links.extend(od_links)
            if store.get_user_setting(uid, "use_dynamics", "0") == "1" and connectors.is_connected(uid, "dynamics"):
                dy, dy_links = connectors.search_with_links(uid, "dynamics", search_q, max_results=5, current_user_name=uid, ai_settings=settings)
                if dy.strip():
                    parts.append("[Dynamics 365]\n" + dy)
                source_links.extend(dy_links)
            context = "\n\n".join(p for p in parts if p.strip())[:8000]
        except Exception:
            context = ""

    # Gli allegati precedono il resto del contesto (massima priorità).
    if attach_block:
        context = (attach_block + ("\n\n" + context if context else ""))[:14000]

    # ── Generazione documenti su richiesta (Word/Excel/PPT/PDF) ──
    # Funziona sia in Documentale sia in AI libera; il contenuto è prodotto da
    # Claude e il file è costruito sui template ISEO (Excel da zero).
    gen_fmt = docgen.detect_request(query)
    if gen_fmt:
        def gen_stream():
            try:
                yield "data: " + json.dumps({"type": "delta", "text": "Sto preparando il file…\n\n"}, ensure_ascii=False) + "\n\n"
                path, fname = docgen.generate(gen_fmt, query, context, settings)
                token = connectors.register_download(uid, path, fname)
                yield "data: " + json.dumps({"type": "delta", "text": f"Ho preparato **{fname}**. Puoi scaricarlo qui sotto."}, ensure_ascii=False) + "\n\n"
                yield "data: " + json.dumps({"type": "sources", "items": [{"name": fname, "url": "/download/" + token, "kind": "download"}]}, ensure_ascii=False) + "\n\n"
            except Exception as e:
                yield "data: " + json.dumps({"type": "error", "text": "Generazione non riuscita: " + str(e)[:200]}, ensure_ascii=False) + "\n\n"
        return StreamingResponse(gen_stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # Memoria conversazionale + esempi promossi (pollice su) dell'utente: danno
    # continuità tra sessioni e qualità. La sessione corrente è esclusa dalla
    # memoria per non duplicare la conversazione in corso.
    try:
        mem_ctx = memory.build_memory_context(uid, exclude_session=body.session_id)
        fb_ctx = memory.build_feedback_context(uid)
    except Exception:
        mem_ctx, fb_ctx = "", ""

    def _gen():
        # Risposta in streaming (i link NON passano da Claude/anonimizzazione).
        yield from stream_reply(clean, settings, anon_names(), context,
                                body.free_mode, mem_ctx, fb_ctx)
        # Fonti cliccabili (link strutturati), come i last_links del desktop.
        if source_links:
            # dedup mantenendo l'ordine di rilevanza
            seen, uniq = set(), []
            for s in source_links:
                key = s.get("url", "")
                if key and key not in seen:
                    seen.add(key)
                    uniq.append(s)
            if uniq:
                yield "data: " + json.dumps({"type": "sources", "items": uniq}, ensure_ascii=False) + "\n\n"

    return StreamingResponse(
        _gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/attach")
async def api_attach(request: Request, files: list[UploadFile] = File(...)):
    user = auth.current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Sessione scaduta.")
    out = []
    for f in files[:20]:
        ext = Path(f.filename or "").suffix.lower()
        if ext not in knowledge.ALLOWED_ATTACH_EXT:
            out.append({"name": f.filename, "ok": False,
                        "error": "Tipo non supportato"})
            continue
        try:
            raw = await f.read()
            if len(raw) > 25 * 1024 * 1024:  # 25 MB per file
                out.append({"name": f.filename, "ok": False, "error": "File troppo grande (max 25 MB)"})
                continue
            text = knowledge.extract_attachment_text(f.filename or "", raw)
            if not text.strip():
                out.append({"name": f.filename, "ok": False, "error": "Nessun testo estraibile"})
                continue
            out.append({"name": f.filename, "ok": True,
                        "chars": len(text), "text": text[:14000]})
        except Exception as e:
            out.append({"name": f.filename, "ok": False, "error": str(e)[:120]})
    return {"attachments": out}


# ── Cronologia conversazioni (per-utente) ──────────────────
def _derive_title(history: list[dict]) -> str:
    for m in history:
        if m.get("role") == "user":
            c = (m.get("content") or "").strip()
            if c and not c.startswith("[CONTESTO"):
                return c[:40] + ("…" if len(c) > 40 else "")
    return "Chat"


class SaveChatReq(BaseModel):
    session_id: str | None = None
    history: list[dict]
    title: str | None = None


class SessionIdReq(BaseModel):
    session_id: str


class RenameReq(BaseModel):
    session_id: str
    title: str


@app.post("/api/chat/save")
def api_chat_save(request: Request, body: SaveChatReq):
    user = auth.current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Sessione scaduta.")
    # salva solo se c'è almeno un turno utente reale
    has_user = any(m.get("role") == "user" and (m.get("content") or "").strip()
                   and not (m.get("content") or "").startswith("[CONTESTO") for m in body.history)
    if not has_user:
        return {"session_id": body.session_id or "", "skipped": True}
    title = (body.title or "").strip() or _derive_title(body.history)
    import json as _json
    sid = store.save_chat_session(user["username"], body.session_id or "", title,
                                  _json.dumps(body.history, ensure_ascii=False))
    return {"session_id": sid, "title": title}


@app.get("/api/chat/list")
def api_chat_list(request: Request):
    user = auth.current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Sessione scaduta.")
    return {"sessions": store.list_chat_sessions(user["username"])}


@app.get("/api/chat/get")
def api_chat_get(request: Request, sid: str):
    user = auth.current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Sessione scaduta.")
    s = store.get_chat_session(user["username"], sid)
    if not s:
        raise HTTPException(status_code=404, detail="Conversazione non trovata.")
    import json as _json
    try:
        hist = _json.loads(s["history"]) if s["history"] else []
    except Exception:
        hist = []
    return {"session_id": s["id"], "title": s["title"], "history": hist}


@app.post("/api/chat/delete")
def api_chat_delete(request: Request, body: SessionIdReq):
    user = auth.current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Sessione scaduta.")
    store.delete_chat_session(user["username"], body.session_id)
    return {"ok": True}


@app.post("/api/chat/rename")
def api_chat_rename(request: Request, body: RenameReq):
    user = auth.current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Sessione scaduta.")
    title = (body.title or "").strip()[:80] or "Chat"
    store.rename_chat_session(user["username"], body.session_id, title)
    return {"ok": True, "title": title}


# ── Feedback pollice su/giù ────────────────────────────────
class FeedbackReq(BaseModel):
    question: str = ""
    answer: str = ""


@app.post("/api/feedback/good")
def api_feedback_good(request: Request, body: FeedbackReq):
    user = auth.current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Sessione scaduta.")
    ans = (body.answer or "").strip()
    if not ans:
        return {"ok": False, "detail": "Nessuna risposta da salvare."}
    store.add_good_answer(user["username"], (body.question or "").strip(), ans)
    return {"ok": True, "promoted": store.count_good_answers(user["username"])}


@app.post("/api/feedback/bad")
def api_feedback_bad(request: Request, body: FeedbackReq):
    user = auth.current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Sessione scaduta.")
    # Segnalazione: non genera apprendimento negativo (come nel desktop). Utile
    # come riscontro all'utente; un'estensione potrebbe registrarla per analisi.
    return {"ok": True}


# ── Impostazioni motore (admin) ─────────────────────────────
@app.get("/admin", response_class=HTMLResponse)
def admin_page(request: Request):
    user = auth.current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    if not user["is_admin"]:
        raise HTTPException(status_code=403, detail="Accesso riservato all'amministratore.")
    return templates.TemplateResponse(request, "admin.html", _ctx(
        request, user,
        claude_model=store.get_setting("claude_model", "claude-opus-4-8"),
        claude_key_set=store.has_secret("claude_api_key"),
        claude_anonymize=store.get_setting("claude_anonymize", "1") == "1",
        lm_url=store.get_setting("lm_url", ""),
        lm_model=store.get_setting("lm_model", ""),
        anon_dictionary=store.get_setting("anon_dictionary", ""),
        kb_embedding_model=store.get_setting("kb_embedding_model", "all-minilm-l6-v2"),
        kb_mode=store.get_setting("kb_mode", "local"),
        kb_host=store.get_setting("kb_host", "localhost"),
        kb_port=store.get_setting("kb_port", "8000"),
        kb_key_set=store.has_secret("kb_api_key"),
        kb_available=knowledge.kb_available(),
        od_client_id=store.get_setting("od_client_id", DEF_OD_CLIENT_ID),
        od_tenant_id=store.get_setting("od_tenant_id", DEF_OD_TENANT_ID),
        dyn_client_id=store.get_setting("dyn_client_id", DEF_DYN_CLIENT_ID),
        dyn_tenant_id=store.get_setting("dyn_tenant_id", DEF_DYN_TENANT_ID),
        dyn_resource_url=store.get_setting("dyn_resource_url", DEF_DYN_RESOURCE_URL),
        def_od_client_id=DEF_OD_CLIENT_ID, def_od_tenant_id=DEF_OD_TENANT_ID,
        def_dyn_client_id=DEF_DYN_CLIENT_ID, def_dyn_tenant_id=DEF_DYN_TENANT_ID,
        def_dyn_resource_url=DEF_DYN_RESOURCE_URL,
        dyn_catalog=connectors.dyn_catalog_status(),
        dyn_connected=connectors.is_connected(user["username"], "dynamics"),
        dyn_msg=request.query_params.get("dyn_msg", ""),
        dyn_err=request.query_params.get("dyn_err", ""),
        saved=request.query_params.get("saved") == "1",
    ))


@app.post("/admin")
def admin_save(
    request: Request,
    claude_api_key: str = Form(""),
    claude_model: str = Form("claude-opus-4-8"),
    claude_anonymize: str = Form("0"),
    lm_url: str = Form(""),
    lm_model: str = Form(""),
    anon_dictionary: str = Form(""),
    kb_embedding_model: str = Form("all-minilm-l6-v2"),
    kb_mode: str = Form("local"),
    kb_host: str = Form("localhost"),
    kb_port: str = Form("8000"),
    kb_api_key: str = Form(""),
    od_client_id: str = Form(""),
    od_tenant_id: str = Form(""),
    dyn_client_id: str = Form(""),
    dyn_tenant_id: str = Form(""),
    dyn_resource_url: str = Form(""),
):
    user = auth.current_user(request)
    if not user or not user["is_admin"]:
        raise HTTPException(status_code=403, detail="Accesso riservato all'amministratore.")
    if claude_api_key.strip():
        store.set_setting("claude_api_key", claude_api_key.strip(), secret=True)
    store.set_setting("claude_model", claude_model.strip() or "claude-opus-4-8")
    store.set_setting("claude_anonymize", "1" if claude_anonymize == "1" else "0")
    store.set_setting("lm_url", lm_url.strip())
    store.set_setting("lm_model", lm_model.strip())
    store.set_setting("anon_dictionary", anon_dictionary)
    store.set_setting("kb_embedding_model", kb_embedding_model.strip() or "all-minilm-l6-v2")
    store.set_setting("kb_mode", "remote" if kb_mode == "remote" else "local")
    store.set_setting("kb_host", kb_host.strip() or "localhost")
    store.set_setting("kb_port", kb_port.strip() or "8000")
    if kb_api_key.strip():
        store.set_setting("kb_api_key", kb_api_key.strip(), secret=True)
    # Connettori Microsoft (non segreti): se vuoti, ripristina i default ISEO.
    store.set_setting("od_client_id", od_client_id.strip() or DEF_OD_CLIENT_ID)
    store.set_setting("od_tenant_id", od_tenant_id.strip() or DEF_OD_TENANT_ID)
    store.set_setting("dyn_client_id", dyn_client_id.strip() or DEF_DYN_CLIENT_ID)
    store.set_setting("dyn_tenant_id", dyn_tenant_id.strip() or DEF_DYN_TENANT_ID)
    store.set_setting("dyn_resource_url", dyn_resource_url.strip() or DEF_DYN_RESOURCE_URL)
    return RedirectResponse(url="/admin?saved=1", status_code=303)


# ── Gestione utenti (admin) ─────────────────────────────────
@app.get("/admin/users", response_class=HTMLResponse)
def users_page(request: Request):
    user = auth.current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    if not user["is_admin"]:
        raise HTTPException(status_code=403, detail="Accesso riservato all'amministratore.")
    return templates.TemplateResponse(request, "admin_users.html", _ctx(
        request, user,
        users=store.list_users(), departments=store.list_departments(),
        msg=request.query_params.get("msg", ""), err=request.query_params.get("err", ""),
    ))


@app.post("/admin/users")
def users_create(
    request: Request,
    new_username: str = Form(...),
    new_password: str = Form(...),
    new_department: str = Form(...),
    new_is_admin: str = Form("0"),
):
    user = auth.current_user(request)
    if not user or not user["is_admin"]:
        raise HTTPException(status_code=403, detail="Accesso riservato all'amministratore.")
    uname = new_username.strip()
    if len(new_password) < 8:
        return RedirectResponse(url="/admin/users?err=La+password+deve+avere+almeno+8+caratteri.", status_code=303)
    if not store.department_exists(new_department):
        return RedirectResponse(url="/admin/users?err=Dipartimento+non+valido.", status_code=303)
    ok = store.create_user(uname, auth.hash_password(new_password), new_department,
                           is_admin=(new_is_admin == "1"))
    if not ok:
        return RedirectResponse(url="/admin/users?err=Username+gi%C3%A0+esistente.", status_code=303)
    return RedirectResponse(url=f"/admin/users?msg=Utente+{uname}+creato.", status_code=303)


@app.post("/admin/users/update")
def users_update(
    request: Request,
    username: str = Form(...),
    department: str = Form(...),
    is_admin: str = Form("0"),
    active: str = Form("0"),
    reset_password: str = Form(""),
):
    admin = auth.current_user(request)
    if not admin or not admin["is_admin"]:
        raise HTTPException(status_code=403, detail="Accesso riservato all'amministratore.")
    target = store.get_user(username)
    if not target:
        return RedirectResponse(url="/admin/users?err=Utente+inesistente.", status_code=303)

    want_admin = is_admin == "1"
    want_active = active == "1"

    # Sicurezza: non lasciare l'app senza alcun amministratore attivo.
    losing_admin = target["is_admin"] and (not want_admin or not want_active)
    if losing_admin and store.admin_count() <= 1:
        return RedirectResponse(
            url="/admin/users?err=Deve+restare+almeno+un+amministratore+attivo.", status_code=303)

    if not store.department_exists(department):
        return RedirectResponse(url="/admin/users?err=Dipartimento+non+valido.", status_code=303)

    pwd_hash = None
    if reset_password.strip():
        if len(reset_password) < 8:
            return RedirectResponse(url="/admin/users?err=La+nuova+password+deve+avere+almeno+8+caratteri.", status_code=303)
        pwd_hash = auth.hash_password(reset_password)

    store.update_user(username, department=department, is_admin=want_admin,
                      active=want_active, password_hash=pwd_hash)
    return RedirectResponse(url=f"/admin/users?msg=Utente+{username}+aggiornato.", status_code=303)


# ── Gestione dipartimenti (admin) ───────────────────────────
@app.get("/admin/departments", response_class=HTMLResponse)
def departments_page(request: Request):
    user = auth.current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    if not user["is_admin"]:
        raise HTTPException(status_code=403, detail="Accesso riservato all'amministratore.")
    deps = store.list_departments()
    rows = [{"name": d, "collection": store.collection_for_department(d),
             "folders": store.department_folders(d)} for d in deps]
    return templates.TemplateResponse(request, "admin_departments.html", _ctx(
        request, user, departments=rows,
        msg=request.query_params.get("msg", ""), err=request.query_params.get("err", ""),
    ))


def _list_subdirs(path: str):
    """Elenco sottocartelle di 'path' (solo directory), robusto su share di rete.
    Strumento admin: naviga il filesystem del SERVER per scegliere le cartelle
    ricercabili. Ritorna (path_normalizzato, parent, [sottocartelle])."""
    try:
        base = Path(path or "/").expanduser()
        if not base.is_absolute():
            base = Path("/") / base
        base = base.resolve()
    except Exception:
        base = Path("/")
    subdirs, parent = [], str(base.parent)
    try:
        with os.scandir(str(base)) as it:
            for e in it:
                try:
                    if e.is_dir(follow_symlinks=False) and not e.name.startswith("."):
                        subdirs.append({"name": e.name, "path": str(base / e.name)})
                except OSError:
                    continue
    except (OSError, PermissionError):
        pass
    subdirs.sort(key=lambda d: d["name"].lower())
    return str(base), parent, subdirs


@app.get("/admin/browse", response_class=HTMLResponse)
def admin_browse(request: Request):
    user = auth.current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    if not user["is_admin"]:
        raise HTTPException(status_code=403, detail="Accesso riservato all'amministratore.")
    dept = request.query_params.get("dept", "")
    path = request.query_params.get("path", "/")
    here, parent, subdirs = _list_subdirs(path)
    return templates.TemplateResponse(request, "admin_browse.html", _ctx(
        request, user, dept=dept, here=here, parent=parent, subdirs=subdirs,
        deps=store.list_departments(),
        current_folders=store.department_folders(dept) if dept else [],
    ))


@app.post("/admin/departments/folder/add")
def departments_folder_add(request: Request, department: str = Form(...), path: str = Form(...)):
    user = auth.current_user(request)
    if not user or not user["is_admin"]:
        raise HTTPException(status_code=403, detail="Accesso riservato all'amministratore.")
    if not store.department_exists(department):
        return RedirectResponse(url="/admin/departments?err=Dipartimento+non+valido.", status_code=303)
    p = (path or "").strip()
    if not p or not Path(p).exists():
        return RedirectResponse(
            url=f"/admin/browse?dept={department}&path={p}&err=1", status_code=303)
    store.add_department_folder(department, p)
    return RedirectResponse(
        url=f"/admin/browse?dept={department}&path={p}", status_code=303,
        background=BackgroundTask(knowledge.folder_reindex, p))


@app.post("/admin/departments/folder/remove")
def departments_folder_remove(request: Request, department: str = Form(...), path: str = Form(...)):
    user = auth.current_user(request)
    if not user or not user["is_admin"]:
        raise HTTPException(status_code=403, detail="Accesso riservato all'amministratore.")
    store.remove_department_folder(department, path)
    return RedirectResponse(
        url=f"/admin/departments?msg=Cartella+rimossa+da+{department}.", status_code=303)


@app.post("/admin/departments")
def departments_create(request: Request, new_department: str = Form(...)):
    user = auth.current_user(request)
    if not user or not user["is_admin"]:
        raise HTTPException(status_code=403, detail="Accesso riservato all'amministratore.")
    name = new_department.strip()
    if not name:
        return RedirectResponse(url="/admin/departments?err=Nome+non+valido.", status_code=303)
    ok = store.add_department(name)
    if not ok:
        return RedirectResponse(url="/admin/departments?err=Dipartimento+gi%C3%A0+esistente.", status_code=303)
    return RedirectResponse(url=f"/admin/departments?msg=Dipartimento+{name}+creato.", status_code=303)


# ── Conoscenza: ChromaDB dipartimento + cartella ────────────
@app.get("/knowledge", response_class=HTMLResponse)
def knowledge_page(request: Request):
    user = auth.current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    dept = user.get("department") or "—"
    folders = store.department_folders(dept)
    return templates.TemplateResponse(request, "knowledge.html", _ctx(
        request, user,
        kb_available=knowledge.kb_available(),
        kb_collection=store.collection_for_department(dept),
        kb_docs=knowledge.kb_list(dept),
        kb_count=knowledge.kb_count(dept),
        folders=folders,
        folder_count=knowledge.dept_folders_count(dept),
        allowed_ext=", ".join(sorted(knowledge.ALLOWED_UPLOAD_EXT)),
        msg=request.query_params.get("msg", ""), err=request.query_params.get("err", ""),
    ))


@app.post("/knowledge/upload")
def knowledge_upload(request: Request, files: list[UploadFile] = File(...)):
    user = auth.current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    dept = user.get("department") or ""
    if not knowledge.kb_available():
        return RedirectResponse(url="/knowledge?err=ChromaDB+non+installato+sul+server.", status_code=303)
    ok_count, fail = 0, []
    for f in files:
        if not f or not f.filename:
            continue
        ext = ("." + f.filename.rsplit(".", 1)[-1].lower()) if "." in f.filename else ""
        if ext not in knowledge.ALLOWED_UPLOAD_EXT:
            fail.append(f.filename + " (tipo non supportato)")
            continue
        try:
            raw = f.file.read()
            text = knowledge.extract_text(f.filename, raw)
            done, _msg = knowledge.kb_ingest(dept, f.filename, text)
            if done:
                ok_count += 1
            else:
                fail.append(f.filename)
        except Exception:
            fail.append(f.filename)
    msg = f"{ok_count}+file+indicizzati+nella+collezione+del+dipartimento."
    if fail:
        return RedirectResponse(
            url=f"/knowledge?msg={msg}&err=" + "+".join(("Non+caricati:", *fail))[:300],
            status_code=303)
    return RedirectResponse(url=f"/knowledge?msg={msg}", status_code=303)


@app.post("/knowledge/delete")
def knowledge_delete(request: Request, filename: str = Form(...)):
    user = auth.current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    dept = user.get("department") or ""
    ok, _msg = knowledge.kb_delete(dept, filename)
    if ok:
        return RedirectResponse(url=f"/knowledge?msg=Rimosso+{filename}.", status_code=303)
    return RedirectResponse(url="/knowledge?err=Errore+rimozione.", status_code=303)


@app.post("/knowledge/reindex")
def knowledge_reindex(request: Request):
    user = auth.current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    dept = user.get("department") or ""
    if not store.department_folders(dept):
        return RedirectResponse(url="/knowledge?err=Nessuna+cartella+configurata+per+il+dipartimento.", status_code=303)
    ok, msg = knowledge.dept_folders_reindex(dept)
    key = "msg" if ok else "err"
    return RedirectResponse(url=f"/knowledge?{key}=" + msg.replace(" ", "+")[:200], status_code=303)


# ── Connessioni utente ──────────────────────────────────────
@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    user = auth.current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    uname = user["username"]
    dept = user.get("department") or "—"
    return templates.TemplateResponse(request, "user.html", _ctx(
        request, user,
        use_kb=store.get_user_setting(uname, "use_kb", "1") == "1",
        use_folder=store.get_user_setting(uname, "use_folder", "1") == "1",
        use_onedrive=store.get_user_setting(uname, "use_onedrive", "0") == "1",
        use_dynamics=store.get_user_setting(uname, "use_dynamics", "0") == "1",
        kb_count=knowledge.kb_count(dept),
        folders=store.department_folders(dept),
        # Connettori Microsoft: stato reale di connessione per-utente.
        od_connected=connectors.is_connected(uname, "onedrive"),
        dyn_connected=connectors.is_connected(uname, "dynamics"),
        od_configured=connectors.is_configured("onedrive"),
        dyn_configured=connectors.is_configured("dynamics"),
        saved=request.query_params.get("saved") == "1",
    ))


@app.post("/settings")
def settings_save(request: Request, use_kb: str = Form("0"), use_folder: str = Form("0"),
                  use_onedrive: str = Form("0"), use_dynamics: str = Form("0"),
                  ajax: str = Form("")):
    user = auth.current_user(request)
    if not user:
        if ajax == "1":
            return JSONResponse({"ok": False}, status_code=401)
        return RedirectResponse(url="/login", status_code=303)
    uname = user["username"]
    store.set_user_setting(uname, "use_kb", "1" if use_kb == "1" else "0")
    store.set_user_setting(uname, "use_folder", "1" if use_folder == "1" else "0")
    store.set_user_setting(uname, "use_onedrive", "1" if use_onedrive == "1" else "0")
    store.set_user_setting(uname, "use_dynamics", "1" if use_dynamics == "1" else "0")
    if ajax == "1":
        # Salvataggio automatico (cambio toggle): nessun reload di pagina.
        return JSONResponse({"ok": True})
    return RedirectResponse(url="/settings?saved=1", status_code=303)


@app.post("/connect/{conn}/start")
def connect_start(request: Request, conn: str):
    user = auth.current_user(request)
    if not user:
        return JSONResponse({"ok": False, "error": "Sessione scaduta."}, status_code=401)
    if conn not in connectors.CONNECTORS:
        return JSONResponse({"ok": False, "error": "Connettore non valido."}, status_code=404)
    return JSONResponse(connectors.start(user["username"], conn))


@app.get("/connect/{conn}/poll")
def connect_poll(request: Request, conn: str):
    user = auth.current_user(request)
    if not user:
        return JSONResponse({"status": "error", "message": "Sessione scaduta."}, status_code=401)
    if conn not in connectors.CONNECTORS:
        return JSONResponse({"status": "error", "message": "Connettore non valido."}, status_code=404)
    res = connectors.poll_once(user["username"], conn)
    # Appena connesso, attiva automaticamente la fonte: "connesso" implica "in uso".
    if res.get("status") == "connected":
        store.set_user_setting(user["username"], "use_" + conn, "1")
    return JSONResponse(res)


@app.post("/connect/{conn}/logout")
def connect_logout(request: Request, conn: str):
    user = auth.current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    if conn in connectors.CONNECTORS:
        connectors.disconnect(user["username"], conn)
    return RedirectResponse(url="/settings?saved=1", status_code=303)


@app.post("/knowledge/folder/add")
def knowledge_folder_add(request: Request, path: str = Form(...)):
    user = auth.current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    if not user["is_admin"]:
        raise HTTPException(status_code=403, detail="Solo l'amministratore può aggiungere cartelle.")
    dept = user.get("department") or ""
    p = (path or "").strip()
    if not p or not Path(p).exists():
        return RedirectResponse(url="/knowledge?err=Percorso+non+raggiungibile+dal+server:+" + p.replace(" ", "+")[:160], status_code=303)
    store.add_department_folder(dept, p)
    # Indicizzazione automatica in background (anche share di rete): la cartella
    # è subito agganciata; l'indice si costruisce senza bloccare la risposta.
    return RedirectResponse(url="/knowledge?msg=Cartella+aggiunta,+indicizzazione+avviata.",
                            status_code=303, background=BackgroundTask(knowledge.folder_reindex, p))


@app.post("/knowledge/folder/remove")
def knowledge_folder_remove(request: Request, path: str = Form(...)):
    user = auth.current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    if not user["is_admin"]:
        raise HTTPException(status_code=403, detail="Solo l'amministratore può rimuovere cartelle.")
    dept = user.get("department") or ""
    store.remove_department_folder(dept, path)
    return RedirectResponse(url="/knowledge?msg=Cartella+rimossa.", status_code=303)


@app.post("/admin/dynamics/build-catalog")
def admin_build_dyn_catalog(request: Request):
    user = auth.current_user(request)
    if not user or not user["is_admin"]:
        raise HTTPException(status_code=403, detail="Accesso riservato all'amministratore.")
    res = connectors.dyn_build_catalog(user["username"])
    if "errore" in res:
        return RedirectResponse(url="/admin?dyn_err=" + str(res["errore"]).replace(" ", "+")[:200], status_code=303)
    msg = f"Catalogo+generato:+{res.get('count',0)}+entità,+{res.get('relazioni',0)}+relazioni,+{res.get('schema_md',{}).get('file',0)}+schema."
    return RedirectResponse(url="/admin?saved=1&dyn_msg=" + msg, status_code=303)


@app.get("/admin/dynamics/diagnose")
def admin_dyn_diagnose(request: Request):
    user = auth.current_user(request)
    if not user or not user["is_admin"]:
        raise HTTPException(status_code=403, detail="Accesso riservato all'amministratore.")
    return JSONResponse({"report": connectors.dyn_diagnose(user["username"])})


@app.get("/admin/dynamics/log")
def admin_dyn_log(request: Request):
    user = auth.current_user(request)
    if not user or not user["is_admin"]:
        raise HTTPException(status_code=403, detail="Accesso riservato all'amministratore.")
    return JSONResponse({"log": connectors.dyn_log_tail(120)})


@app.get("/download/{token}")
def download_file(request: Request, token: str):
    user = auth.current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    rec = connectors.download_info(user["username"], token)
    if not rec or not Path(rec["path"]).is_file():
        raise HTTPException(status_code=404, detail="File non disponibile o scaduto.")
    from fastapi.responses import FileResponse
    return FileResponse(rec["path"], filename=rec["filename"])


@app.get("/dyn-report/{token}", response_class=HTMLResponse)
def dyn_report(request: Request, token: str):
    user = auth.current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    # Solo il proprietario del report; il path arriva dal registro (non da input
    # utente), quindi niente path traversal. Il report è dati Dynamics riservati.
    path = connectors.report_path(user["username"], token)
    if not path or not Path(path).is_file():
        raise HTTPException(status_code=404, detail="Report non disponibile o scaduto.")
    try:
        html = Path(path).read_text(encoding="utf-8")
    except Exception:
        raise HTTPException(status_code=404, detail="Report non leggibile.")
    return HTMLResponse(content=html)


@app.get("/healthz")
def healthz():
    return {"status": "ok"}

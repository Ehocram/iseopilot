"""
store.py — Persistenza con cifratura a riposo + utenti/dipartimenti.

Tabelle:
  * app_settings   -> impostazioni GLOBALI admin (chiave Claude, LM, dizionario).
  * user_settings  -> impostazioni PER-UTENTE (connessioni OneDrive/Dynamics).
  * departments    -> aree/dipartimenti aziendali (governano la conoscenza KB).
  * users          -> utenti gestiti dall'admin (login locale). Ogni utente
                      appartiene a un dipartimento: questo è ciò che, in
                      ChromaDB, determina la collezione ("container") in cui
                      finiscono e da cui si leggono i documenti dell'utente.

I valori `secret=1` (chiave API, token) sono cifrati con Fernet. Le PASSWORD
NON stanno qui in chiaro: si memorizza solo l'hash scrypt (vedi auth.py).
"""
from __future__ import annotations

import os
import re
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

DB_PATH = Path(os.environ.get("APP_DATA_DIR", "/data")) / "app.db"
USER_TOKENS_DIR = Path(os.environ.get("APP_DATA_DIR", "/data")) / "user_tokens"

# Dipartimenti creati al primo avvio (richiesti da Marco).
DEFAULT_DEPARTMENTS = [
    "IT", "Infosec", "ESG", "Privacy", "Sales",
    "Operations", "Finance", "HR", "Supply Chain", "R&D",
]

_lock = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _fernet() -> Fernet:
    key = os.environ.get("APP_SECRET_KEY", "").strip()
    if not key:
        raise RuntimeError(
            "APP_SECRET_KEY non impostata. Genera una chiave con:\n"
            "  python -c \"from cryptography.fernet import Fernet; "
            "print(Fernet.generate_key().decode())\"\n"
            "e impostala nell'ambiente (file .env / Docker secret)."
        )
    return Fernet(key.encode())


def _cx() -> sqlite3.Connection:
    cx = sqlite3.connect(DB_PATH)
    cx.row_factory = sqlite3.Row
    return cx


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    USER_TOKENS_DIR.mkdir(parents=True, exist_ok=True)
    with _lock, _cx() as cx:
        cx.execute(
            "CREATE TABLE IF NOT EXISTS app_settings ("
            "  key TEXT PRIMARY KEY, value TEXT, secret INTEGER DEFAULT 0)"
        )
        cx.execute(
            "CREATE TABLE IF NOT EXISTS user_settings ("
            "  user_id TEXT, key TEXT, value TEXT, secret INTEGER DEFAULT 0,"
            "  PRIMARY KEY (user_id, key))"
        )
        cx.execute(
            "CREATE TABLE IF NOT EXISTS departments ("
            "  name TEXT PRIMARY KEY, created_at TEXT, folder_path TEXT DEFAULT '')"
        )
        # Migrazione sicura: aggiunge folder_path se il DB è precedente.
        cols = [r[1] for r in cx.execute("PRAGMA table_info(departments)").fetchall()]
        if "folder_path" not in cols:
            cx.execute("ALTER TABLE departments ADD COLUMN folder_path TEXT DEFAULT ''")
        cx.execute(
            "CREATE TABLE IF NOT EXISTS users ("
            "  username TEXT PRIMARY KEY,"
            "  password_hash TEXT NOT NULL,"
            "  department TEXT,"
            "  is_admin INTEGER DEFAULT 0,"
            "  active INTEGER DEFAULT 1,"
            "  created_at TEXT)"
        )
        cx.execute(
            "CREATE TABLE IF NOT EXISTS dept_folders ("
            "  department TEXT, path TEXT,"
            "  PRIMARY KEY (department, path))"
        )
        # Cronologia conversazioni, per-utente. title/history cifrati (Fernet):
        # sono dati personali, coerente con la cifratura dei segreti.
        cx.execute(
            "CREATE TABLE IF NOT EXISTS chat_sessions ("
            "  id TEXT PRIMARY KEY,"
            "  user_id TEXT NOT NULL,"
            "  title TEXT,"
            "  history TEXT,"
            "  created_at TEXT,"
            "  updated_at TEXT)"
        )
        cx.execute("CREATE INDEX IF NOT EXISTS idx_chat_user ON chat_sessions(user_id, updated_at)")
        # Esempi promossi col pollice su (per-utente): alimentano la qualità delle
        # risposte future. question/answer cifrati.
        cx.execute(
            "CREATE TABLE IF NOT EXISTS good_answers ("
            "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "  user_id TEXT NOT NULL,"
            "  question TEXT,"
            "  answer TEXT,"
            "  created_at TEXT)"
        )
        cx.execute("CREATE INDEX IF NOT EXISTS idx_good_user ON good_answers(user_id, id)")
        # Audit trail: registro append-only delle attività. Contiene azioni e
        # METADATI (mai il contenuto delle chat né password), più utente, IP e
        # orario UTC. Visibile solo all'admin.
        cx.execute(
            "CREATE TABLE IF NOT EXISTS audit_log ("
            "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "  ts TEXT NOT NULL,"
            "  username TEXT,"
            "  action TEXT NOT NULL,"
            "  detail TEXT,"
            "  ip TEXT)"
        )
        cx.execute("CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts)")
        # Seed dipartimenti (solo se la tabella è vuota: non sovrascrive scelte admin)
        existing = cx.execute("SELECT COUNT(*) FROM departments").fetchone()[0]
        if existing == 0:
            for d in DEFAULT_DEPARTMENTS:
                cx.execute(
                    "INSERT OR IGNORE INTO departments (name, created_at) VALUES (?,?)",
                    (d, _now()),
                )
        # Migrazione: porta l'eventuale singolo folder_path nella tabella multi-cartella.
        for r in cx.execute("SELECT name, folder_path FROM departments WHERE folder_path != ''").fetchall():
            cx.execute("INSERT OR IGNORE INTO dept_folders (department, path) VALUES (?,?)",
                       (r["name"], r["folder_path"]))
        cx.commit()


# ── Audit trail (append-only) ───────────────────────────────
def audit_log(username: str, action: str, detail: str = "", ip: str = "") -> None:
    """Registra un'attività. Best-effort: non solleva mai, per non far fallire
    la richiesta utente se il log non riesce."""
    try:
        with _lock, _cx() as cx:
            cx.execute(
                "INSERT INTO audit_log (ts, username, action, detail, ip) VALUES (?,?,?,?,?)",
                (_now(), username or "", action or "", (detail or "")[:1000], ip or ""),
            )
            cx.commit()
    except Exception:
        import sys
        print(f"[audit] impossibile registrare: {action}", file=sys.stderr)


def audit_query(start_iso: str | None = None, end_iso: str | None = None,
                username: str | None = None, action: str | None = None,
                limit: int = 10000) -> list[dict]:
    """Righe di audit filtrate per intervallo (UTC ISO), utente e azione."""
    where, params = [], []
    if start_iso:
        where.append("ts >= ?"); params.append(start_iso)
    if end_iso:
        where.append("ts <= ?"); params.append(end_iso)
    if username:
        where.append("username = ?"); params.append(username)
    if action:
        where.append("action = ?"); params.append(action)
    sql = "SELECT ts, username, action, detail, ip FROM audit_log"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(int(limit))
    with _lock, _cx() as cx:
        rows = cx.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def audit_actions() -> list[str]:
    """Elenco delle azioni distinte presenti, per il filtro a tendina."""
    with _lock, _cx() as cx:
        rows = cx.execute("SELECT DISTINCT action FROM audit_log ORDER BY action").fetchall()
    return [r["action"] for r in rows if r["action"]]


def audit_usernames() -> list[str]:
    with _lock, _cx() as cx:
        rows = cx.execute("SELECT DISTINCT username FROM audit_log WHERE username != '' ORDER BY username").fetchall()
    return [r["username"] for r in rows]


# ── Collezione ChromaDB per dipartimento ────────────────────
def collection_for_department(dept: str) -> str:
    """Nome della collezione ChromaDB associata a un dipartimento.
    È il 'container' che isola la conoscenza: i documenti caricati da un utente
    finiscono qui e solo gli utenti dello stesso dipartimento li interrogano.
    (Usato dall'Incremento 3 per il wiring effettivo di ChromaDB.)"""
    slug = re.sub(r"[^a-z0-9]+", "_", (dept or "").strip().lower()).strip("_")
    return f"kb_{slug or 'generale'}"


# ── Impostazioni globali (admin) ────────────────────────────
def get_setting(key: str, default: str = "") -> str:
    with _lock, _cx() as cx:
        row = cx.execute("SELECT value, secret FROM app_settings WHERE key=?", (key,)).fetchone()
    if not row:
        return default
    value, secret = row["value"], row["secret"]
    if secret and value:
        try:
            return _fernet().decrypt(value.encode()).decode()
        except InvalidToken:
            return default
    return value if value is not None else default


def set_setting(key: str, value: str, secret: bool = False) -> None:
    stored = _fernet().encrypt(value.encode()).decode() if (secret and value) else value
    with _lock, _cx() as cx:
        cx.execute(
            "INSERT INTO app_settings (key, value, secret) VALUES (?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, secret=excluded.secret",
            (key, stored, 1 if secret else 0),
        )
        cx.commit()


def has_secret(key: str) -> bool:
    with _lock, _cx() as cx:
        row = cx.execute("SELECT value FROM app_settings WHERE key=?", (key,)).fetchone()
    return bool(row and row["value"])


# ── Cronologia conversazioni (per-utente, cifrata) ──────────
def _enc(s: str) -> str:
    return _fernet().encrypt((s or "").encode()).decode()


def _dec(s: str) -> str:
    if not s:
        return ""
    try:
        return _fernet().decrypt(s.encode()).decode()
    except InvalidToken:
        return ""


def save_chat_session(user_id: str, session_id: str, title: str, history_json: str) -> str:
    """Crea o aggiorna una sessione di chat dell'utente. Ritorna l'id."""
    sid = session_id or datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
    now = _now()
    t_enc, h_enc = _enc(title), _enc(history_json)
    with _lock, _cx() as cx:
        ex = cx.execute("SELECT id FROM chat_sessions WHERE id=? AND user_id=?", (sid, user_id)).fetchone()
        if ex:
            cx.execute("UPDATE chat_sessions SET title=?, history=?, updated_at=? WHERE id=? AND user_id=?",
                       (t_enc, h_enc, now, sid, user_id))
        else:
            cx.execute("INSERT INTO chat_sessions (id, user_id, title, history, created_at, updated_at) "
                       "VALUES (?,?,?,?,?,?)", (sid, user_id, t_enc, h_enc, now, now))
        cx.commit()
    return sid


def list_chat_sessions(user_id: str, limit: int = 200) -> list[dict]:
    """Elenco sessioni dell'utente (più recenti prima), senza la history."""
    with _lock, _cx() as cx:
        rows = cx.execute(
            "SELECT id, title, updated_at FROM chat_sessions WHERE user_id=? "
            "ORDER BY updated_at DESC LIMIT ?", (user_id, limit)).fetchall()
    return [{"id": r["id"], "title": _dec(r["title"]) or "Chat", "updated_at": r["updated_at"]} for r in rows]


def get_chat_session(user_id: str, session_id: str) -> dict | None:
    with _lock, _cx() as cx:
        r = cx.execute("SELECT id, title, history, updated_at FROM chat_sessions WHERE id=? AND user_id=?",
                       (session_id, user_id)).fetchone()
    if not r:
        return None
    return {"id": r["id"], "title": _dec(r["title"]) or "Chat",
            "history": _dec(r["history"]), "updated_at": r["updated_at"]}


def delete_chat_session(user_id: str, session_id: str) -> None:
    with _lock, _cx() as cx:
        cx.execute("DELETE FROM chat_sessions WHERE id=? AND user_id=?", (session_id, user_id))
        cx.commit()


def rename_chat_session(user_id: str, session_id: str, title: str) -> None:
    with _lock, _cx() as cx:
        cx.execute("UPDATE chat_sessions SET title=?, updated_at=? WHERE id=? AND user_id=?",
                   (_enc(title), _now(), session_id, user_id))
        cx.commit()


def recent_sessions_with_history(user_id: str, limit: int = 5) -> list[dict]:
    """Ultime sessioni CON history (per costruire la memoria conversazionale)."""
    with _lock, _cx() as cx:
        rows = cx.execute(
            "SELECT id, history, updated_at FROM chat_sessions WHERE user_id=? "
            "ORDER BY updated_at DESC LIMIT ?", (user_id, limit)).fetchall()
    return [{"id": r["id"], "history": _dec(r["history"]), "date": r["updated_at"]} for r in rows]


# ── Feedback: esempi promossi (pollice su), per-utente, cifrati ─
def add_good_answer(user_id: str, question: str, answer: str) -> None:
    with _lock, _cx() as cx:
        cx.execute("INSERT INTO good_answers (user_id, question, answer, created_at) VALUES (?,?,?,?)",
                   (user_id, _enc(question[:1000]), _enc(answer[:3000]), _now()))
        # tieni al massimo gli ultimi 100 per utente
        cx.execute("DELETE FROM good_answers WHERE user_id=? AND id NOT IN "
                   "(SELECT id FROM good_answers WHERE user_id=? ORDER BY id DESC LIMIT 100)",
                   (user_id, user_id))
        cx.commit()


def list_good_answers(user_id: str, limit: int = 5) -> list[dict]:
    with _lock, _cx() as cx:
        rows = cx.execute("SELECT question, answer FROM good_answers WHERE user_id=? "
                          "ORDER BY id DESC LIMIT ?", (user_id, limit)).fetchall()
    # ritorna in ordine cronologico (più vecchi prima) come few-shot
    out = [{"question": _dec(r["question"]), "answer": _dec(r["answer"])} for r in rows]
    return list(reversed(out))


def count_good_answers(user_id: str) -> int:
    with _lock, _cx() as cx:
        return cx.execute("SELECT COUNT(*) FROM good_answers WHERE user_id=?", (user_id,)).fetchone()[0]


def count_chat_sessions(user_id: str) -> int:
    with _lock, _cx() as cx:
        return cx.execute("SELECT COUNT(*) FROM chat_sessions WHERE user_id=?", (user_id,)).fetchone()[0]


# ── Impostazioni per-utente ─────────────────────────────────
def get_user_setting(user_id: str, key: str, default: str = "") -> str:
    with _lock, _cx() as cx:
        row = cx.execute(
            "SELECT value, secret FROM user_settings WHERE user_id=? AND key=?",
            (user_id, key),
        ).fetchone()
    if not row:
        return default
    value, secret = row["value"], row["secret"]
    if secret and value:
        try:
            return _fernet().decrypt(value.encode()).decode()
        except InvalidToken:
            return default
    return value if value is not None else default


def set_user_setting(user_id: str, key: str, value: str, secret: bool = False) -> None:
    stored = _fernet().encrypt(value.encode()).decode() if (secret and value) else value
    with _lock, _cx() as cx:
        cx.execute(
            "INSERT INTO user_settings (user_id, key, value, secret) VALUES (?,?,?,?) "
            "ON CONFLICT(user_id, key) DO UPDATE SET value=excluded.value, secret=excluded.secret",
            (user_id, key, stored, 1 if secret else 0),
        )
        cx.commit()


def user_token_path(user_id: str, connector: str) -> Path:
    import hashlib
    safe = hashlib.sha256(user_id.encode()).hexdigest()[:16]
    d = USER_TOKENS_DIR / safe
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{connector}_token.json"


# ── Dipartimenti ────────────────────────────────────────────
def list_departments() -> list[str]:
    with _lock, _cx() as cx:
        rows = cx.execute("SELECT name FROM departments ORDER BY name").fetchall()
    return [r["name"] for r in rows]


def add_department(name: str) -> bool:
    name = (name or "").strip()
    if not name:
        return False
    with _lock, _cx() as cx:
        try:
            cx.execute("INSERT INTO departments (name, created_at) VALUES (?,?)", (name, _now()))
            cx.commit()
            return True
        except sqlite3.IntegrityError:
            return False  # già esistente


def department_exists(name: str) -> bool:
    with _lock, _cx() as cx:
        return cx.execute("SELECT 1 FROM departments WHERE name=?", (name,)).fetchone() is not None


def department_folder(name: str) -> str:
    """Compatibilità: prima cartella del dipartimento (o stringa vuota)."""
    folders = department_folders(name)
    return folders[0] if folders else ""


def department_folders(name: str) -> list[str]:
    with _lock, _cx() as cx:
        rows = cx.execute(
            "SELECT path FROM dept_folders WHERE department=? ORDER BY path", (name,)
        ).fetchall()
    return [r["path"] for r in rows if r["path"]]


def add_department_folder(name: str, path: str) -> bool:
    path = (path or "").strip()
    if not path:
        return False
    with _lock, _cx() as cx:
        cx.execute("INSERT OR IGNORE INTO dept_folders (department, path) VALUES (?,?)", (name, path))
        cx.commit()
    return True


def remove_department_folder(name: str, path: str) -> None:
    with _lock, _cx() as cx:
        cx.execute("DELETE FROM dept_folders WHERE department=? AND path=?", (name, (path or "").strip()))
        cx.commit()


def set_department_folder(name: str, path: str) -> None:
    """Compatibilità: imposta un'unica cartella sostituendo le esistenti."""
    with _lock, _cx() as cx:
        cx.execute("DELETE FROM dept_folders WHERE department=?", (name,))
        if (path or "").strip():
            cx.execute("INSERT OR IGNORE INTO dept_folders (department, path) VALUES (?,?)",
                       (name, path.strip()))
        cx.commit()


# ── Utenti ──────────────────────────────────────────────────
def list_users() -> list[dict]:
    with _lock, _cx() as cx:
        rows = cx.execute(
            "SELECT username, department, is_admin, active, created_at "
            "FROM users ORDER BY username"
        ).fetchall()
    return [dict(r) for r in rows]


def get_user(username: str) -> dict | None:
    with _lock, _cx() as cx:
        row = cx.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    return dict(row) if row else None


def user_count() -> int:
    with _lock, _cx() as cx:
        return cx.execute("SELECT COUNT(*) FROM users").fetchone()[0]


def create_user(username: str, password_hash: str, department: str,
                is_admin: bool = False) -> bool:
    username = (username or "").strip()
    if not username:
        return False
    with _lock, _cx() as cx:
        try:
            cx.execute(
                "INSERT INTO users (username, password_hash, department, is_admin, active, created_at) "
                "VALUES (?,?,?,?,1,?)",
                (username, password_hash, department, 1 if is_admin else 0, _now()),
            )
            cx.commit()
            return True
        except sqlite3.IntegrityError:
            return False  # username già preso


def update_user(username: str, *, department: str | None = None,
                is_admin: bool | None = None, active: bool | None = None,
                password_hash: str | None = None) -> None:
    sets, params = [], []
    if department is not None:
        sets.append("department=?"); params.append(department)
    if is_admin is not None:
        sets.append("is_admin=?"); params.append(1 if is_admin else 0)
    if active is not None:
        sets.append("active=?"); params.append(1 if active else 0)
    if password_hash is not None:
        sets.append("password_hash=?"); params.append(password_hash)
    if not sets:
        return
    params.append(username)
    with _lock, _cx() as cx:
        cx.execute(f"UPDATE users SET {', '.join(sets)} WHERE username=?", params)
        cx.commit()


def admin_count() -> int:
    with _lock, _cx() as cx:
        return cx.execute("SELECT COUNT(*) FROM users WHERE is_admin=1 AND active=1").fetchone()[0]

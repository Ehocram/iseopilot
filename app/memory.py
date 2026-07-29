"""Memoria conversazionale e feedback (per-utente).

Replica i due meccanismi dell'app desktop che "accrescono la conoscenza":

1. MEMORIA SESSIONI — le ultime conversazioni salvate dell'utente vengono
   riassunte (coppie domanda/risposta) e iniettate come contesto, per dare
   continuità tra sessioni diverse.

2. ESEMPI PROMOSSI — le risposte marcate col pollice su diventano few-shot
   "esempi eccellenti" che guidano qualità e stile delle risposte future.

Entrambi sono scoping per-utente: ognuno ha la propria memoria e i propri
esempi. I dati arrivano da store.py (cifrati a riposo).
"""
from __future__ import annotations

import json

from . import store


def _pairs_from_history(history_json: str, max_pairs: int) -> list[tuple[str, str]]:
    """Estrae le coppie (utente, assistente) da una history salvata, saltando i
    messaggi di contesto documentale. Tiene le ultime `max_pairs`."""
    try:
        h = json.loads(history_json) if history_json else []
    except Exception:
        return []
    pairs, i = [], 0
    while i < len(h) - 1:
        u, a = h[i], h[i + 1]
        if (u.get("role") == "user" and not str(u.get("content", "")).startswith("[CONTESTO")
                and a.get("role") == "assistant"):
            pairs.append((str(u.get("content", ""))[:250], str(a.get("content", ""))[:400]))
            i += 2
        else:
            i += 1
    return pairs[-max_pairs:]


def build_memory_context(user_id: str, max_sessions: int = 5, max_pairs: int = 4,
                         exclude_session: str | None = None) -> str:
    """Contesto di continuità dalle ultime sessioni dell'utente. La sessione
    corrente (exclude_session) viene esclusa per non duplicare la conversazione
    in corso."""
    sessions = store.recent_sessions_with_history(user_id, limit=max_sessions + 1)
    sessions = [s for s in sessions if s["id"] != exclude_session][:max_sessions]
    if not sessions:
        return ""
    lines = ["[MEMORIA SESSIONI PRECEDENTI — mantieni continuità con queste conversazioni:]"]
    used = False
    for s in sessions:
        pairs = _pairs_from_history(s["history"], max_pairs)
        if not pairs:
            continue
        used = True
        lines.append(f"\n-- Sessione {str(s.get('date',''))[:10]} --")
        for q, r in pairs:
            lines.append(f"  U: {q}")
            lines.append(f"  A: {r}")
    return "\n".join(lines) if used else ""


def build_feedback_context(user_id: str, max_examples: int = 5) -> str:
    """Few-shot dagli esempi promossi col pollice su dell'utente."""
    items = store.list_good_answers(user_id, limit=max_examples)
    if not items:
        return ""
    lines = ["[ESEMPI DI RISPOSTE ECCELLENTI — usa come riferimento di qualità e stile:]"]
    for it in items:
        if it["question"] or it["answer"]:
            lines.append(f"\n  D: {it['question']}")
            lines.append(f"  R: {it['answer']}")
    return "\n".join(lines) if len(lines) > 1 else ""


# ════════════════════════════════════════════════════════════
#  NOTE PERSONALI (Incremento 8) — memoria di REGOLE, non di episodi.
#  Preferenze e correzioni che l'utente chiede ESPLICITAMENTE di ricordare
#  ("ricordati che…", "from now on…"): durevoli tra le chat, iniettate in
#  chat, generazione documenti e Attività. Progettate per la governance:
#   - kill-switch admin (memoria_note_enabled, SPENTO di default: si accende
#     solo dopo l'approvazione del Comitato AI);
#   - scritture SOLO su azione esplicita dell'utente (trigger deterministico
#     o pagina Connessioni): mai il modello che decide da solo cosa ricordare;
#   - visibili, modificabili e cancellabili dall'utente; cifrate a riposo;
#   - limiti rigidi (numero e lunghezza) e troncamenti dichiarati.
# ════════════════════════════════════════════════════════════
import re as _re
import uuid as _uuid
import datetime as _dt

NOTE_MAX = 20
NOTA_MAX_CHARS = 300
_NOTE_KEY = "memoria_note"

# Trigger ESPLICITI (inizio messaggio), IT/EN/ES. Deliberatamente esclusi i
# promemoria-di-fatti ("ricordami di chiamare X"): quelli non sono preferenze.
NOTE_TRIGGER_RE = _re.compile(
    r"^\s*(ricordati\s+che|ricorda\s+che|memorizza\s+che|d'ora\s+in\s+poi[,:]?|"
    r"remember\s+that|from\s+now\s+on[,:]?|"
    r"recuerda\s+que|a\s+partir\s+de\s+ahora[,:]?)\s+",
    _re.IGNORECASE)


def note_enabled() -> bool:
    return store.get_setting("memoria_note_enabled", "0") == "1"


def _note_load(user_id: str) -> list[dict]:
    try:
        raw = store.get_user_setting(user_id, _NOTE_KEY, "")
        return json.loads(raw) if raw else []
    except Exception:
        return []


def _note_save(user_id: str, note: list[dict]) -> None:
    store.set_user_setting(user_id, _NOTE_KEY,
                           json.dumps(note, ensure_ascii=False), secret=True)


def note_list(user_id: str) -> list[dict]:
    return _note_load(user_id)


def note_add(user_id: str, testo: str, origine: str = "utente") -> dict:
    """Aggiunge una nota. Ritorna {ok} o {ok: False, errore} — limiti parlanti."""
    if not note_enabled():
        return {"ok": False, "errore": "Le note di memoria personale non sono abilitate."}
    testo = " ".join(str(testo or "").split()).strip()
    if not testo:
        return {"ok": False, "errore": "Nota vuota."}
    if len(testo) > NOTA_MAX_CHARS:
        return {"ok": False, "errore": f"Nota troppo lunga ({len(testo)} caratteri): "
                                       f"il limite è {NOTA_MAX_CHARS}."}
    note = _note_load(user_id)
    if any(n.get("testo", "").lower() == testo.lower() for n in note):
        return {"ok": True, "duplicata": True}
    if len(note) >= NOTE_MAX:
        return {"ok": False, "errore": f"Limite di {NOTE_MAX} note raggiunto: "
                                       "elimina una nota dalla pagina Connessioni."}
    note.append({"id": _uuid.uuid4().hex[:12], "testo": testo,
                 "origine": origine,
                 "creata": _dt.datetime.now().strftime("%Y-%m-%d %H:%M")})
    _note_save(user_id, note)
    return {"ok": True}


def note_delete(user_id: str, nota_id: str) -> bool:
    note = _note_load(user_id)
    dopo = [n for n in note if n.get("id") != nota_id]
    if len(dopo) == len(note):
        return False
    _note_save(user_id, dopo)
    return True


def note_clear(user_id: str) -> int:
    note = _note_load(user_id)
    _note_save(user_id, [])
    return len(note)


def estrai_nota(messaggio: str) -> str:
    """Se il messaggio inizia con un trigger esplicito, ritorna il testo della
    nota (troncato al limite); altrimenti stringa vuota."""
    m = NOTE_TRIGGER_RE.match(messaggio or "")
    if not m:
        return ""
    resto = (messaggio or "")[m.end():].strip().rstrip(".")
    return resto[:NOTA_MAX_CHARS]


def build_note_context(user_id: str) -> str:
    """Blocco di sistema con le note dell'utente. Vuoto se spento o senza note.
    La richiesta esplicita nel messaggio corrente PREVALE comunque (coerente
    con la regola lingua)."""
    if not note_enabled():
        return ""
    note = _note_load(user_id)
    if not note:
        return ""
    righe = ["[NOTE PERSONALI DELL'UTENTE — preferenze e correzioni che ha chiesto "
             "di ricordare: rispettale in ogni risposta e documento; una richiesta "
             "esplicita nel messaggio corrente prevale comunque:]"]
    for n in note[:NOTE_MAX]:
        righe.append(f"- {n.get('testo', '')}")
    return "\n".join(righe)

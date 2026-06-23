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

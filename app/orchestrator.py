"""
orchestrator.py — Cuore conversazionale (porting del ChatWorker desktop).

Espone un generatore SINCRONO `stream_reply(...)` che produce chunk di testo
in streaming. FastAPI lo serve con StreamingResponse, eseguendolo in threadpool,
così possiamo usare `requests` senza complicazioni async.

Motori supportati: Claude (Anthropic) e LM Studio locale. OpenAI rimosso.

Differenze rispetto al desktop:
  * Claude qui è in STREAMING (il desktop lo faceva solo per LM Studio).
  * Anonimizzazione con ripristino "a flush sicuro": il testo viene emesso man
    mano, ma un token tipo [EMAIL_001] non viene mai spezzato tra due chunk —
    si trattiene la coda finché il token non è completo, poi si ripristina il
    valore originale. Garantisce streaming E confidenzialità corretta.
"""
from __future__ import annotations

import json
from typing import Iterator

import requests

from .anonymizer import Anonymizer

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"

# Toni — invariati dalla versione desktop.
TONES = {
    "Aziendale formale": "Sei un assistente aziendale professionale. Tono formale, preciso e autorevole. Sii conciso ed esaustivo.",
    "Informale":         "Sei un assistente simpatico e disponibile. Tono amichevole e diretto.",
    "Tecnico":           "Sei un esperto tecnico altamente specializzato. Linguaggio tecnico preciso. Struttura le risposte in modo logico.",
    "Sintetico":         "Sei un assistente che valorizza la brevità. Rispondi nel modo più conciso possibile.",
    "Analitico":         "Sei un consulente analitico e strategico. Analizza ogni problema da più angolazioni.",
}

# Istruzioni di lingua — invariate dalla versione desktop.
LANG_INSTR = {
    "Italiano": "CRITICAL LANGUAGE RULE: You MUST respond ONLY in Italian. Tutte le tue risposte devono essere SOLO in italiano.",
    "Inglese":  "CRITICAL LANGUAGE RULE: You MUST respond ONLY in English, even if the user writes in another language.",
    "Tedesco":  "CRITICAL LANGUAGE RULE: You MUST respond ONLY in German (Deutsch).",
    "Francese": "CRITICAL LANGUAGE RULE: You MUST respond ONLY in French (Français).",
    "Spagnolo": "CRITICAL LANGUAGE RULE: You MUST respond ONLY in Spanish (Español).",
    "Rumeno":   "CRITICAL LANGUAGE RULE: You MUST respond ONLY in Romanian (Română).",
    "Auto":     "",
}


def build_system(tone_key: str, reply_lang: str, context: str = "",
                 free_mode: bool = False, memory_context: str = "",
                 feedback_context: str = "") -> str:
    parts = [TONES.get(tone_key, TONES["Aziendale formale"])]
    li = LANG_INSTR.get(reply_lang, "")
    if li:
        parts.append(li)
    # Evita che Claude aggiunga note di anonimizzazione: le gestiamo noi a valle.
    parts.append("Non aggiungere note o avvisi sull'anonimizzazione dei dati.")
    if free_mode:
        # MODALITÀ AI LIBERA (AI ON): conoscenza generale, nessuna ricerca nelle
        # fonti aziendali. Porting del comportamento del pulsante "🤖 AI" desktop.
        parts.append(
            "MODALITÀ AI LIBERA. Sei un assistente generalista: rispondi usando la "
            "TUA CONOSCENZA GENERALE su qualsiasi argomento (persone pubbliche, fatti "
            "storici, scienza, tecnica, geografia, attualità). NON stai consultando "
            "documenti aziendali. NON dire mai 'non trovato nei documenti', 'nella base "
            "dati' o 'non ho informazioni': stai rispondendo dalla tua conoscenza "
            "generale. Fornisci sempre una risposta utile e completa."
        )
    elif context and context.strip():
        parts.append(
            "Usa il CONTESTO seguente, recuperato dalla base di conoscenza aziendale, "
            "SOLO se pertinente alla domanda. Cita le fonti tra parentesi quadre, es. "
            "[Fonte: nome_file]. Se il contesto non è pertinente, ignoralo e rispondi "
            "con le tue conoscenze.\n\n=== CONTESTO ===\n" + context.strip() +
            "\n=== FINE CONTESTO ==="
        )
    # Memoria conversazionale e esempi promossi: continuità e qualità. Non sono
    # una restrizione — se non pertinenti, il modello procede normalmente.
    if memory_context and memory_context.strip():
        parts.append(memory_context.strip())
    if feedback_context and feedback_context.strip():
        parts.append(feedback_context.strip())
    return "\n\n".join(parts)


def _sse(payload: dict) -> str:
    """Serializza un evento per il client (formato SSE su POST)."""
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


# ── Claude ──────────────────────────────────────────────────
def _stream_claude(messages, settings, anon_names, context: str = "", free_mode: bool = False,
                   memory_context: str = "", feedback_context: str = "") -> Iterator[str]:
    api_key = settings.get("claude_api_key", "").strip()
    model = settings.get("claude_model", "claude-opus-4-8").strip() or "claude-opus-4-8"
    if not api_key:
        yield _sse({"type": "error", "text": "Chiave API Claude non configurata dall'amministratore."})
        return

    do_anon = settings.get("claude_anonymize", True)
    system = build_system(settings.get("tone_key", "Aziendale formale"),
                          settings.get("reply_lang", "Italiano"), context, free_mode,
                          memory_context, feedback_context)
    anon = Anonymizer()

    if do_anon:
        # Regex su tutti i messaggi utente + dizionario nomi (NLP off, come la
        # modalità KB del desktop: non maschera nomi propri utili al contesto).
        out_msgs = []
        for m in messages:
            if m["role"] == "user":
                body = anon.anonymize(m["content"])
                body = anon.anonymize_names(body, anon_names, use_nlp=False)
                out_msgs.append({"role": "user", "content": body})
            else:
                out_msgs.append(m)
        system = anon.anonymize(system)
    else:
        out_msgs = list(messages)

    needs_restore = bool(anon.get_map())
    payload = {"model": model, "max_tokens": 1500, "system": system,
               "messages": out_msgs, "stream": True}

    try:
        resp = requests.post(
            ANTHROPIC_URL,
            headers={"x-api-key": api_key, "anthropic-version": "2023-06-01",
                     "Content-Type": "application/json"},
            json=payload, stream=True, timeout=120,
        )
        resp.raise_for_status()
    except Exception as e:
        yield _sse({"type": "error", "text": f"Errore chiamata Claude: {e}"})
        return

    buffer = ""
    for line in resp.iter_lines(decode_unicode=True):
        if not line or not line.startswith("data: "):
            continue
        data_str = line[6:].strip()
        try:
            evt = json.loads(data_str)
        except Exception:
            continue
        etype = evt.get("type")
        if etype == "content_block_delta":
            delta = evt.get("delta", {}).get("text", "")
            if not delta:
                continue
            if not needs_restore:
                yield _sse({"type": "delta", "text": delta})
                continue
            # Flush sicuro: non spezzare un token [TIPO_NNN] tra due chunk.
            buffer += delta
            last_open = buffer.rfind("[")
            last_close = buffer.rfind("]")
            if last_open > last_close:
                flush, buffer = buffer[:last_open], buffer[last_open:]
            else:
                flush, buffer = buffer, ""
            if flush:
                yield _sse({"type": "delta", "text": anon.restore(flush)})
        elif etype == "error":
            msg = evt.get("error", {}).get("message", "errore Claude")
            yield _sse({"type": "error", "text": msg})

    if needs_restore and buffer:
        yield _sse({"type": "delta", "text": anon.restore(buffer)})

    if needs_restore:
        n = len(anon.get_map())
        yield _sse({"type": "delta",
                    "text": f"\n\n🔒 *{n} elementi anonimizzati prima dell'invio ad Anthropic.*"})
    yield _sse({"type": "done"})


# ── LM Studio (locale) ──────────────────────────────────────
def _stream_lmstudio(messages, settings, context: str = "", free_mode: bool = False,
                     memory_context: str = "", feedback_context: str = "") -> Iterator[str]:
    url = settings.get("lm_url", "").strip()
    model = settings.get("lm_model", "").strip()
    if not url:
        yield _sse({"type": "error", "text": "Endpoint LM Studio non configurato dall'amministratore."})
        return
    system = build_system(settings.get("tone_key", "Aziendale formale"),
                          settings.get("reply_lang", "Italiano"), context, free_mode,
                          memory_context, feedback_context)
    lm_msgs = [{"role": "system", "content": system}] + list(messages)
    payload = {"model": model or "local-model", "messages": lm_msgs,
               "max_tokens": settings.get("lm_max_tokens", 1024),
               "temperature": settings.get("lm_temperature", 0.3), "stream": True}
    try:
        resp = requests.post(url, headers={"Content-Type": "application/json"},
                             json=payload, stream=True, timeout=300)
        resp.raise_for_status()
    except Exception as e:
        yield _sse({"type": "error", "text": f"Errore LM Studio: {e}"})
        return

    for line in resp.iter_lines(decode_unicode=True):
        if not line or not line.startswith("data: "):
            continue
        data_str = line[6:].strip()
        if data_str == "[DONE]":
            break
        try:
            chunk = json.loads(data_str)
            delta = chunk.get("choices", [{}])[0].get("delta", {}).get("content", "")
            if delta:
                yield _sse({"type": "delta", "text": delta})
        except Exception:
            continue
    yield _sse({"type": "done"})


# ── Dispatcher ──────────────────────────────────────────────
def complete(system: str, user_text: str, settings: dict, max_tokens: int = 4000) -> str:
    """Chiamata Claude NON in streaming: ritorna il testo completo. Usata per
    generare contenuti strutturati (documenti)."""
    api_key = settings.get("claude_api_key", "").strip()
    model = settings.get("claude_model", "claude-opus-4-8").strip() or "claude-opus-4-8"
    if not api_key:
        raise RuntimeError("Chiave API Claude non configurata dall'amministratore.")
    payload = {"model": model, "max_tokens": max_tokens, "system": system,
               "messages": [{"role": "user", "content": user_text}]}
    resp = requests.post(
        ANTHROPIC_URL, json=payload,
        headers={"x-api-key": api_key, "anthropic-version": "2023-06-01",
                 "content-type": "application/json"},
        timeout=120)
    if resp.status_code != 200:
        raise RuntimeError(f"Errore API Claude ({resp.status_code}): {resp.text[:200]}")
    data = resp.json()
    return "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")


def stream_reply(messages, settings, anon_names, context: str = "", free_mode: bool = False,
                 memory_context: str = "", feedback_context: str = "") -> Iterator[str]:
    """Genera la risposta in streaming secondo il motore selezionato."""
    engine = settings.get("ai_engine", "claude")
    if engine == "lmstudio":
        yield from _stream_lmstudio(messages, settings, context, free_mode, memory_context, feedback_context)
    else:
        yield from _stream_claude(messages, settings, anon_names, context, free_mode, memory_context, feedback_context)

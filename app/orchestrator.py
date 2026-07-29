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
    parts.append(
        "SU ISEOPILOT STESSO: se l'utente segnala un problema o malfunzionamento "
        "dell'applicazione (link di download mancante, errore, funzione che non "
        "risponde) o chiede di aggiungere integrazioni/funzioni, NON inventare "
        "procedure, impostazioni, tempi di attesa o canali di 'supporto tecnico': "
        "non esistono. Riconosci la segnalazione e invita a inoltrarla "
        "all'amministratore interno di ISEOPilot, che riceve anche i log. "
        "Sulle capacità dell'applicazione dichiara solo ciò che risulta dal "
        "contesto o ammetti di non saperlo.")

    li = LANG_INSTR.get(reply_lang, "")
    if li:
        parts.append(li)
        # La regola lingua è il DEFAULT, non una gabbia: se l'utente chiede
        # esplicitamente un'altra lingua ("redigi in spagnolo", "draft it in
        # Spanish"), quella prevale — caso Carlos: la regola CRITICAL faceva
        # ignorare la richiesta e, nel conflitto, anche il compito.
        parts.append(
            "ECCEZIONE alla regola lingua: se l'utente chiede ESPLICITAMENTE che "
            "la risposta o un contenuto sia in un'altra lingua, la sua richiesta "
            "PREVALE su questa impostazione per quel contenuto, e il compito va "
            "comunque eseguito per intero nella lingua richiesta.")
    # Evita che Claude aggiunga note di anonimizzazione: le gestiamo noi a valle.
    parts.append("Non aggiungere note o avvisi sull'anonimizzazione dei dati.")
    # Il modello vive dentro ISEOPilot, che SA generare file scaricabili: non
    # deve mai negare questa capacità (creava risposte fuorvianti agli utenti).
    parts.append(
        "IMPORTANTE: l'applicazione ISEOPilot in cui operi PUÒ generare file "
        "scaricabili (Word, Excel, PowerPoint, PDF) sui modelli aziendali, ma il "
        "file viene creato DALL'APPLICAZIONE, non da te, e solo quando il "
        "messaggio dell'utente è una richiesta esplicita (es. \"creami un "
        "PowerPoint su...\") o una conferma (\"sì, procedi\"). Non dire mai che "
        "non è possibile generare file; ma non dire nemmeno \"procedo a generare "
        "il file\": tu non puoi. Se l'utente vuole il file, digli di confermare "
        "con un messaggio come \"procedi\" o di chiederlo esplicitamente, e il "
        "link di download comparirà sotto la risposta.")
    if free_mode:
        # MODALITÀ AI LIBERA (AI ON): conoscenza generale, nessuna ricerca nelle
        # fonti aziendali. Gli ALLEGATI dell'utente però passano SEMPRE (in
        # libera il context contiene solo quelli): scartarli faceva negare al
        # modello file che l'utente vedeva regolarmente agganciati alla chat.
        parts.append(
            "MODALITÀ AI LIBERA. Sei un assistente generalista: rispondi usando la "
            "TUA CONOSCENZA GENERALE su qualsiasi argomento (persone pubbliche, fatti "
            "storici, scienza, tecnica, geografia, attualità). NON stai consultando le "
            "fonti aziendali (Conoscenza, Cartelle, OneDrive, Dynamics). NON dire mai "
            "'non trovato nei documenti' o 'nella base dati': stai rispondendo dalla "
            "tua conoscenza generale. Fornisci sempre una risposta utile e completa. "
            "ECCEZIONE DI CORTESIA: se la domanda riguarda chiaramente DOCUMENTI, "
            "CERTIFICAZIONI o POLICY AZIENDALI di ISEO, ricorda all'utente che può "
            "passare alla modalità Documentale con fonte 'Conoscenza' (selettori "
            "sotto la chat): lì l'assistente consulta i documenti del suo "
            "dipartimento. Non suggerire di caricare a mano file che probabilmente "
            "sono già nella Conoscenza."
        )
        if context and context.strip():
            parts.append(
                "ECCEZIONE IMPORTANTE: l'utente ha ALLEGATO dei documenti a questa "
                "conversazione. Li trovi qui sotto e DEVI leggerli e usarli come fonte "
                "primaria per rispondere: non negare mai di vederli.\n\n"
                "=== ALLEGATI DELL'UTENTE ===\n" + context.strip() +
                "\n=== FINE ALLEGATI ==="
            )
    elif context and context.strip():
        parts.append(
            "Usa il CONTESTO seguente, recuperato dalla base di conoscenza aziendale, "
            "SOLO se pertinente alla domanda. Cita SEMPRE le fonti col NOME ESATTO del "
            "file tra parentesi quadre, es. [Fonte: anagrafica.docx] — MAI nomi generici "
            "come [Fonte: OneDrive] o [Fonte: contesto aziendale]: il nome del file serve "
            "all'applicazione per mostrare all'utente solo i documenti davvero usati. "
            "Se il contesto non è pertinente, ignoralo e rispondi "
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
                   memory_context: str = "", feedback_context: str = "",
                   images: list | None = None) -> Iterator[str]:
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
    out_msgs = _apply_images(out_msgs, images or [])
    payload = {"model": model, "max_tokens": 8000, "system": system,
               "messages": out_msgs, "stream": True}
    # RICERCA WEB (tool server-side Anthropic): solo in AI LIBERA e solo se
    # l'amministratore l'ha attivata. Tetto per domanda: contiene costi e latenza.
    web_enabled = bool(free_mode and settings.get("claude_web_search"))
    if web_enabled:
        payload["tools"] = [{"type": "web_search_20250305", "name": "web_search",
                             "max_uses": 5}]
        payload["system"] = system + (
            "\n\nHai a disposizione la RICERCA WEB: usala quando la domanda riguarda "
            "informazioni recenti, prezzi, trend o fatti verificabili online. Nel testo "
            "della risposta cita SEMPRE i link delle fonti web che hai usato.")
    web_used = 0

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
        if etype == "content_block_start":
            blk = evt.get("content_block", {}) or {}
            if blk.get("type") == "server_tool_use" and blk.get("name") == "web_search":
                web_used += 1
                yield _sse({"type": "status",
                            "text": f"Ricerca web in corso ({web_used})…"})
            continue
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

    if web_used:
        import sys
        print(f"[web] ricerche web usate nella risposta: {web_used}", file=sys.stderr)
        yield _sse({"type": "delta",
                    "text": f"\n\n🌐 *Risposta costruita con {web_used} ricerc"
                            f"{'a' if web_used == 1 else 'he'} web.*"})
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
def complete(system: str, user_text: str, settings: dict, max_tokens: int = 4000,
             timeout: int = 120) -> str:
    """Chiamata Claude NON in streaming: ritorna il testo completo. Usata per
    generare contenuti strutturati (documenti) e riscritture brevi."""
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
        timeout=timeout)
    if resp.status_code != 200:
        body = (resp.text or "").strip()
        if body.startswith("<") or "<!DOCTYPE" in body[:200] or "<html" in body[:200].lower():
            # pagina HTML di un apparato di rete (proxy/firewall), non dell'API
            body = "risposta HTML da un apparato di rete (proxy/firewall) lungo il percorso verso l'API"
        raise RuntimeError(f"Errore API Claude ({resp.status_code}): {body[:200]}")
    data = resp.json()
    return "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")




def _apply_images(msgs: list, images: list) -> list:
    """Trasforma l'ultimo messaggio utente in contenuto MULTIMODALE per
    l'API Claude: blocchi immagine (base64) seguiti dal testo. Applicato
    DOPO l'anonimizzazione del testo: le immagini viaggiano così come sono
    (limite dichiarato all'utente nell'interfaccia)."""
    if not images:
        return msgs
    out = list(msgs)
    for i in range(len(out) - 1, -1, -1):
        if out[i].get("role") == "user":
            text = out[i].get("content") or ""
            blocks = [{"type": "image",
                       "source": {"type": "base64", "media_type": mt, "data": b64}}
                      for mt, b64 in images]
            blocks.append({"type": "text",
                           "text": text if isinstance(text, str) else str(text)})
            out[i] = {"role": "user", "content": blocks}
            break
    return out


def stream_reply(messages, settings, anon_names, context: str = "", free_mode: bool = False,
                 memory_context: str = "", feedback_context: str = "",
                 images: list | None = None) -> Iterator[str]:
    """Genera la risposta in streaming secondo il motore selezionato."""
    engine = settings.get("ai_engine", "claude")
    if engine == "lmstudio":
        yield from _stream_lmstudio(messages, settings, context, free_mode, memory_context, feedback_context)
    else:
        yield from _stream_claude(messages, settings, anon_names, context, free_mode, memory_context, feedback_context, images=images)

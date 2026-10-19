"""
orchestrator.py — Cuore conversazionale (porting del ChatWorker desktop).

Espone un generatore SINCRONO `stream_reply(...)` che produce chunk di testo
in streaming. FastAPI lo serve con StreamingResponse, eseguendolo in threadpool,
così possiamo usare `requests` senza complicazioni async.

Motori supportati: Claude (Anthropic) e LM Studio locale. OpenAI rimosso.

Differenze rispetto al desktop:
  * Claude qui è in STREAMING (il desktop lo faceva solo per LM Studio).
  * Protezione dei dati con ripristino "a flush sicuro": il testo viene emesso
    man mano, ma un token tipo [EMAIL_001] non viene mai spezzato tra due chunk —
    si trattiene la coda finché il token non è completo, poi si ripristina il
    valore originale. Garantisce streaming E confidenzialità corretta.
  * Verso Claude la protezione è SEMPRE attiva (l'interruttore claude_anonymize
    è ignorato): tutti i messaggi, anche dell'assistente, e le parti dinamiche
    del system passano da UN solo mask_batch con UNA mappa.
"""
from __future__ import annotations

import json
from typing import Iterator

import requests  # noqa: F401 — i test intercettano orchestrator.requests.post

from . import uscita
from .anonymizer import Anonymizer

# Istruzione FISSA sui segnaposto: senza, il modello traduce o «corregge» i
# codici e il ripristino non li ritrova.
ISTRUZIONE_SEGNAPOSTO = (
    "Nel testo alcuni dati sono sostituiti da codici come [PERSONA_001]: trattali "
    "come nomi propri e riportali identici, con le parentesi quadre; non "
    "inventarne di nuovi.")

# Messaggi all'utente sulla protezione dei dati.
MSG_IMMAGINI_CLAUDE = (
    "Le immagini non vengono inviate a Claude: rimuovi le immagini allegate e, "
    "se serve, descrivi a parole cosa contengono.")
AVVISO_PRESIDIO = (
    "⚠️ *Riconoscimento automatico dei dati personali (Presidio) non disponibile: "
    "sostituiti solo i nomi del dizionario e i dati tecnici.*")


def nota_sostituzioni(n: int) -> str:
    """Nota finale 🔒 con il numero di dati DAVVERO sostituiti (mai il dizionario)."""
    return f"🔒 *{n} dat{'o sostituito' if n == 1 else 'i sostituiti'} con codici prima dell'invio a Claude.*"

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
    parts.append(ISTRUZIONE_SEGNAPOSTO)
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

    # Immagini: MAI a Claude (non passano da alcuna protezione). Rifiuto prima
    # di qualunque chiamata, anche per chi usa l'API senza l'interfaccia.
    if images:
        yield _sse({"type": "error", "text": MSG_IMMAGINI_CLAUDE})
        return

    # Protezione SEMPRE attiva (claude_anonymize ignorato). In UN solo lotto e
    # con UNA mappa: tutti i messaggi (anche dell'assistente: dal secondo turno
    # la cronologia tornava in chiaro) e le parti dinamiche del system, prima di
    # build_system. Le istruzioni fisse non passano dal mascheramento.
    yield _sse({"type": "status", "text": "Protezione dei dati in corso…"})
    anon = Anonymizer()
    testi = [m.get("content") if isinstance(m.get("content"), str) else str(m.get("content") or "")
             for m in messages]
    mascherati, esito = anon.mask_batch(
        testi + [context or "", memory_context or "", feedback_context or ""], anon_names)
    out_msgs = [{"role": m["role"], "content": t} for m, t in zip(messages, mascherati)]
    context_m, memory_m, feedback_m = mascherati[len(messages):]
    system = build_system(settings.get("tone_key", "Aziendale formale"),
                          settings.get("reply_lang", "Italiano"), context_m, free_mode,
                          memory_m, feedback_m)
    if not esito["presidio_ok"]:
        import sys
        print(f"[chat] Presidio non disponibile ({esito['presidio_motivo']}): "
              f"solo dizionario e regex", file=sys.stderr)

    needs_restore = bool(anon.get_map())
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
    # Istruzioni fisse del system (senza i dati): un loro esempio che coincide
    # con un valore della mappa non è una fuga per il controllo in uscita.
    fisso = build_system(settings.get("tone_key", "Aziendale formale"),
                         settings.get("reply_lang", "Italiano"),
                         "·" if (context_m or "").strip() else "", free_mode)
    fisso += payload["system"][len(system):]

    try:
        resp = uscita.invia("chat", payload, api_key, stream=True, timeout=120,
                            controllo=anon, fisso=(fisso,))
        resp.raise_for_status()
    except uscita.UscitaBloccata as e:
        # Nulla è partito: errore chiaro, nessun avviso su Presidio.
        yield _sse({"type": "error", "text": str(e)})
        return
    except Exception as e:
        yield _sse({"type": "error", "text": f"Errore chiamata Claude: {e}"})
        # Il payload può essere già partito: l'utente deve sapere comunque che
        # è partito con il solo dizionario e le regex.
        if not esito["presidio_ok"]:
            yield _sse({"type": "delta", "text": "\n\n" + AVVISO_PRESIDIO})
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
            # Si ripristinano SOLO i segnaposto emessi in questa richiesta.
            buffer += delta
            flush, buffer = anon.restore_stream(buffer)
            if flush:
                yield _sse({"type": "delta", "text": flush})
        elif etype == "error":
            msg = evt.get("error", {}).get("message", "errore Claude")
            yield _sse({"type": "error", "text": msg})

    if needs_restore and buffer:
        yield _sse({"type": "delta", "text": anon.restore_stream(buffer, finale=True)[0]})

    if web_used:
        import sys
        print(f"[web] ricerche web usate nella risposta: {web_used}", file=sys.stderr)
        yield _sse({"type": "delta",
                    "text": f"\n\n🌐 *Risposta costruita con {web_used} ricerc"
                            f"{'a' if web_used == 1 else 'he'} web.*"})
    # Note finali: il totale dei dati DAVVERO sostituiti (non il dizionario) e,
    # se Presidio non ha lavorato, l'avviso esplicito all'utente.
    if esito["totale"] > 0:
        yield _sse({"type": "delta", "text": "\n\n" + nota_sostituzioni(esito["totale"])})
    if not esito["presidio_ok"]:
        yield _sse({"type": "delta", "text": "\n\n" + AVVISO_PRESIDIO})
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
        # uscita.invia_locale rifiuta un endpoint che non sia privato o locale
        resp = uscita.invia_locale(url, payload, stream=True, timeout=300)
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
def _complete_lmstudio(system: str, user_text: str, settings: dict, max_tokens: int,
                       timeout: int) -> str:
    """complete() con LM Studio: stesso endpoint della chat, senza streaming.
    Nessun ripiego su Claude: se LM Studio non risponde è un errore."""
    url = settings.get("lm_url", "").strip()
    model = settings.get("lm_model", "").strip()
    if not url:
        raise RuntimeError("Endpoint LM Studio non configurato dall'amministratore.")
    payload = {"model": model or "local-model",
               "messages": [{"role": "system", "content": system},
                            {"role": "user", "content": user_text}],
               "max_tokens": max_tokens,
               "temperature": settings.get("lm_temperature", 0.3), "stream": False}
    try:
        resp = uscita.invia_locale(url, payload, timeout=timeout)
    except uscita.EndpointNonPrivato:
        raise
    except Exception as e:
        raise RuntimeError(f"Errore LM Studio: {e}") from e
    if resp.status_code != 200:
        raise RuntimeError(f"Errore LM Studio ({resp.status_code}): {(resp.text or '').strip()[:200]}")
    try:
        return resp.json()["choices"][0]["message"]["content"] or ""
    except Exception as e:
        raise RuntimeError(f"Risposta di LM Studio non leggibile: {type(e).__name__}") from e


def complete(system: str, user_text: str, settings: dict, max_tokens: int = 4000,
             timeout: int | None = None, *, scopo: str = "", controllo=None) -> str:
    """Chiamata NON in streaming: ritorna il testo completo. Usata per generare
    contenuti strutturati (documenti), piani e riscritture brevi.

    Il motore è quello scelto per la richiesta (settings["ai_engine"], come in
    stream_reply): con LM Studio non si chiama MAI Claude, nemmeno come ripiego.
    `scopo` è quello di uscita.SCOPI; `controllo` l'Anonymizer del chiamante
    per il controllo in uscita. Il system di tutti i chiamanti contiene solo
    istruzioni fisse (i dati vanno in user_text): vale come testo fisso."""
    uscita.politica(scopo)                     # scopo sconosciuto: ValueError subito
    if settings.get("ai_engine", "claude") == "lmstudio":
        # i modelli locali sono lenti: senza un tempo esplicito si attende di più
        return _complete_lmstudio(system, user_text, settings, max_tokens,
                                  timeout if timeout is not None else 300)
    api_key = settings.get("claude_api_key", "").strip()
    model = settings.get("claude_model", "claude-opus-4-8").strip() or "claude-opus-4-8"
    if not api_key:
        raise RuntimeError("Chiave API Claude non configurata dall'amministratore.")
    payload = {"model": model, "max_tokens": max_tokens, "system": system,
               "messages": [{"role": "user", "content": user_text}]}
    resp = uscita.invia(scopo, payload, api_key,
                        timeout=timeout if timeout is not None else 120,
                        controllo=controllo, fisso=(system,))
    if resp.status_code != 200:
        body = (resp.text or "").strip()
        if body.startswith("<") or "<!DOCTYPE" in body[:200] or "<html" in body[:200].lower():
            # pagina HTML di un apparato di rete (proxy/firewall), non dell'API
            body = "risposta HTML da un apparato di rete (proxy/firewall) lungo il percorso verso l'API"
        raise RuntimeError(f"Errore API Claude ({resp.status_code}): {body[:200]}")
    data = resp.json()
    return "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")




def _apply_images(msgs: list, images: list) -> list:
    """Trasforma l'ultimo messaggio utente in contenuto MULTIMODALE (blocchi
    immagine base64 seguiti dal testo). NON più usata verso Claude: le
    immagini non passano da alcuna protezione e _stream_claude le rifiuta."""
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

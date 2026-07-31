"""
translate.py — traduzione dei segmenti per il doppiaggio.
Backend: Claude API (api.anthropic.com) oppure endpoint OpenAI-compatibile
(es. LM Studio locale). Nessun SDK: solo `requests`.
"""
import json

import requests

LANG_NAMES = {"it": "italiano", "en": "inglese", "es": "spagnolo (Spagna)",
              "fr": "francese", "de": "tedesco"}
LANG_REGISTER = {"it": "dai del tu", "es": "dai del tu ('tú')",
                 "fr": "usa il 'vous'", "de": "usa il 'Sie'"}

SYSTEM = ("Sei un traduttore professionista specializzato nel doppiaggio di "
          "video aziendali (formazione interna, tutorial software, security "
          "awareness).")

CLAUDE_MODELS = ["claude-sonnet-4-6", "claude-haiku-4-5-20251001",
                 "claude-opus-4-8", "claude-fable-5"]

BATCH = 25  # segmenti per richiesta: JSON corti = niente troncamenti


def _prompt(texts, lang, context="", glossary="", src="it", limits=None):
    reg = LANG_REGISTER.get(lang)
    rules = [
        "registro PARLATO, naturale e professionale: verranno letti da una voce, non stampati",
        "la trascrizione può contenere piccoli errori di riconoscimento vocale: "
        "correggili in base al contesto, senza segnalarlo",
        "nomi di prodotti, funzioni e voci di interfaccia restano riconoscibili; "
        "non inventare terminologia",
        ("il parlato tradotto deve risultare PIÙ CORTO dell'originale del 10-15%: "
         "questa lingua si espande e il doppiaggio deve stare nei tempi"
         if lang in ("es", "fr", "de") or (lang == "it" and src == "en") else
         "lunghezza uguale o leggermente più corta dell'originale (mai oltre +10%): "
         "il doppiaggio deve stare nei tempi"),
        "una traduzione per segmento, stesso ordine, nessuna aggiunta od omissione",
    ]
    if reg:
        rules.insert(0, reg)
    head = (f"Traduci i segmenti seguenti dalla lingua di origine "
            f"({LANG_NAMES.get(src, src)}) alla lingua di destinazione "
            f"({LANG_NAMES.get(lang, lang)}): sono parti consecutive dello "
            "stesso discorso parlato.")
    ctx = f"\nContesto del video: {context.strip()}" if context.strip() else ""
    glo = (f"\nGlossario da rispettare (termine = resa; '(invariato)' = non tradurre): "
           f"{glossary.strip()}") if glossary.strip() else ""
    lim = ("\nBudget MASSIMO di caratteri per ciascun segmento, nello stesso "
           "ordine (tassativo: se serve riformula più asciutto): "
           + json.dumps(limits)) if limits else ""
    return (head + ctx + glo + lim + "\nRegole:\n- " + "\n- ".join(rules) +
            "\nRispondi SOLO con un array JSON di stringhe, senza testo extra.\n\n"
            + json.dumps(texts, ensure_ascii=False))


def _check(r):
    """Solleva un errore con il messaggio VERO del server, non solo il codice."""
    if r.status_code >= 400:
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:300]}")


def _parse(text, n):
    t = text.strip()
    if t.startswith("```"):
        t = t.split("```")[1]
        if t.startswith("json"):
            t = t[4:]
    out = json.loads(t.strip())
    if isinstance(out, list) and len(out) == n + 1 and not str(out[-1]).strip():
        out = out[:-1]                      # elemento vuoto in coda: tollerato
    if not isinstance(out, list) or len(out) != n:
        raise ValueError(f"attese {n} traduzioni, ricevute "
                         f"{len(out) if isinstance(out, list) else 'N/A'}")
    return [str(x) for x in out]


def _batched(call, texts, limits=None, batch=BATCH, retries=1):
    """Traduzione a lotti con retry; se un lotto sballa il conteggio anche al
    retry (es. segmenti quasi identici da allucinazioni Whisper), degrada a
    frase-per-frase: deterministico, mai un job perso per un lotto storto."""
    out = []
    for i in range(0, len(texts), batch):
        chunk = texts[i:i + batch]
        lims = limits[i:i + batch] if limits else None
        done = False
        for attempt in range(retries + 1):
            try:
                out.extend(call(chunk, lims))
                done = True
                break
            except ValueError:                 # conteggio sbagliato: ritenta
                if attempt == retries:
                    break
            except Exception:
                if attempt == retries:
                    raise
        if not done:
            for k, t_ in enumerate(chunk):     # frase per frase
                lk = [lims[k]] if lims else None
                try:
                    out.extend(call([t_], lk))
                except Exception:
                    out.append(t_)             # ultima difesa: testo invariato
    return out


def translate_claude(texts, lang, api_key, model="claude-sonnet-4-6",
                     context="", glossary="", src="it", limits=None, timeout=180):
    def call(chunk, lims):
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": api_key, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": model, "max_tokens": 8000, "system": SYSTEM,
                  "messages": [{"role": "user",
                                "content": _prompt(chunk, lang, context, glossary, src, lims)}]},
            timeout=timeout)
        _check(r)
        return _parse(r.json()["content"][0]["text"], len(chunk))
    return _batched(call, texts, limits)


def translate_openai_compat(texts, lang, base_url, api_key="", model="local-model",
                            context="", glossary="", src="it", limits=None, timeout=300):
    """Per LM Studio: base_url tipicamente http://localhost:1234/v1"""
    headers = {"content-type": "application/json"}
    if api_key:
        headers["authorization"] = f"Bearer {api_key}"

    def call(chunk, lims):
        r = requests.post(
            base_url.rstrip("/") + "/chat/completions", headers=headers,
            json={"model": model, "temperature": 0.2, "max_tokens": 4000,
                  "messages": [{"role": "system", "content": SYSTEM},
                               {"role": "user",
                                "content": _prompt(chunk, lang, context, glossary, src, lims)}]},
            timeout=timeout)
        _check(r)
        return _parse(r.json()["choices"][0]["message"]["content"], len(chunk))
    return _batched(call, texts, limits)


def list_claude_models(api_key, timeout=15):
    """Modelli realmente disponibili per questa API key (GET /v1/models)."""
    r = requests.get("https://api.anthropic.com/v1/models",
                     headers={"x-api-key": api_key,
                              "anthropic-version": "2023-06-01"},
                     timeout=timeout)
    _check(r)
    return [m["id"] for m in r.json().get("data", []) if m.get("id")]


def list_openai_models(base_url, api_key="", timeout=8):
    """Modelli esposti da un endpoint OpenAI-compatibile (es. LM Studio)."""
    headers = {}
    if api_key:
        headers["authorization"] = f"Bearer {api_key}"
    r = requests.get(base_url.rstrip("/") + "/models", headers=headers,
                     timeout=timeout)
    _check(r)
    return [m["id"] for m in r.json().get("data", []) if m.get("id")]


def make_translator(cfg):
    """cfg: {backend, api_key, base_url, model, context, glossary}"""
    b = cfg.get("backend", "manual")
    ctx = cfg.get("context", "")
    glo = cfg.get("glossary", "")
    src = cfg.get("src", "it")
    if b == "claude":
        return lambda texts, lang, limits=None: translate_claude(
            texts, lang, cfg["api_key"], cfg.get("model") or "claude-sonnet-4-6",
            context=ctx, glossary=glo, src=src, limits=limits)
    if b == "openai":
        return lambda texts, lang, limits=None: translate_openai_compat(
            texts, lang, cfg.get("base_url") or "http://localhost:1234/v1",
            cfg.get("api_key", ""), cfg.get("model") or "local-model",
            context=ctx, glossary=glo, src=src, limits=limits)
    # manuale: copia l'italiano, l'utente corregge nella tabella di revisione
    return lambda texts, lang, limits=None: list(texts)


SHORTEN_PROMPT = ("Sei un adattatore dialoghi per doppiaggio. Riscrivi il testo "
                  "in {lang} in MASSIMO {n} caratteri (spazi inclusi), mantenendo "
                  "le informazioni chiave e il registro parlato. Rispondi SOLO "
                  "con il testo riscritto, senza virgolette.")


def make_shortener(cfg):
    """f(text, lang, max_chars) -> testo accorciato; None se backend manuale."""
    b = cfg.get("backend", "manual")
    if b == "manual":
        return None

    def short(text, lang, max_chars):
        sysmsg = SHORTEN_PROMPT.format(lang=LANG_NAMES.get(lang, lang),
                                       n=int(max_chars))
        if b == "claude":
            r = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": cfg["api_key"],
                         "anthropic-version": "2023-06-01",
                         "content-type": "application/json"},
                json={"model": cfg.get("model") or "claude-sonnet-4-6",
                      "max_tokens": 500, "system": sysmsg,
                      "messages": [{"role": "user", "content": text}]},
                timeout=90)
            _check(r)
            out = r.json()["content"][0]["text"]
        else:
            headers = {"content-type": "application/json"}
            if cfg.get("api_key"):
                headers["authorization"] = f"Bearer {cfg['api_key']}"
            r = requests.post(
                (cfg.get("base_url") or "http://localhost:1234/v1").rstrip("/")
                + "/chat/completions", headers=headers,
                json={"model": cfg.get("model") or "local-model",
                      "temperature": 0.2, "max_tokens": 500,
                      "messages": [{"role": "system", "content": sysmsg},
                                   {"role": "user", "content": text}]},
                timeout=120)
            _check(r)
            out = r.json()["choices"][0]["message"]["content"]
        return out.strip().strip('"').strip()
    return short

"""Modalità ATTIVITÀ ("cowork"): agente DELIMITATO sulla Conoscenza del
reparto. Per i COMPITI, non per le domande: pianifica in passi (max 8), può
CERCARE nella Conoscenza, LEGGERE documenti per intero (dagli originali
archiviati) e COMPORRE un documento Word — poi risponde citando le fonti.

Garanzie di progetto ("senza compromettere nulla"):
- modulo separato: Documentale e AI libera non vengono toccate;
- perimetro identico alla chat: SOLO la Conoscenza del reparto dell'utente,
  sola lettura, anonimizzazione su tutto ciò che esce, fonti citate;
- passi limitati (MAX_PASSI) e memoria limitata: costi e latenza delimitati;
- ogni passo è nominato nei log ([cowork]) e visibile in /admin/logs;
- esito ONESTO: se i passi non bastano, lo dice e consegna quanto raccolto.
"""
from __future__ import annotations

import json
import re
import sys
from typing import Iterator

from . import knowledge
from . import docgen
from . import connectors
from .orchestrator import complete, _sse
from .anonymizer import Anonymizer

MAX_PASSI = 8
LETTURA_MAX_CHARS = 40_000
MEMORIA_MAX_CHARS = 60_000

PROTOCOLLO = """Sei l'agente ATTIVITÀ di ISEOPilot. Ricevi un COMPITO da svolgere
usando ESCLUSIVAMENTE la Conoscenza del dipartimento (documenti elencati).
Lavori a passi: a ogni turno rispondi con UNA SOLA azione, in JSON puro,
senza testo prima o dopo, senza markdown.

Azioni disponibili:
{"azione": "cerca", "query": "parole chiave"}
  → ricerca semantica nella Conoscenza (con copertura per nome).
{"azione": "leggi", "documento": "nome esatto del file"}
  → testo INTEGRALE di un documento dell'elenco (usa il nome esatto).
{"azione": "componi", "spec": {"title": "...", "subtitle": "...", "sections": [{"heading": "...", "paragraphs": ["..."], "bullets": ["..."]}]}}
  → genera un documento Word aziendale con quel contenuto; poi potrai rispondere.
{"azione": "rispondi", "testo": "risposta finale per l'utente"}
  → chiude l'attività. Cita SEMPRE i documenti usati come [Fonte: nome file].

Regole: basati SOLO su ciò che leggi qui (mai conoscenza generale per fatti
aziendali); se un'informazione non è nei documenti, dichiaralo; preferisci
LEGGERE i documenti pertinenti piuttosto che accontentarti dei frammenti;
quando il compito chiede un documento/profilo/relazione, usa "componi" con
contenuti COMPLETI tratti dalle letture, poi "rispondi" riassumendo."""


def _estrai_json(raw: str) -> dict:
    """Il modello deve rispondere JSON puro, ma tolleriamo recinzioni e testo
    attorno: si estrae il primo oggetto { ... } bilanciato."""
    t = (raw or "").strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t, flags=re.S)
    i = t.find("{")
    if i < 0:
        return {}
    prof = 0
    for j in range(i, len(t)):
        if t[j] == "{":
            prof += 1
        elif t[j] == "}":
            prof -= 1
            if prof == 0:
                try:
                    return json.loads(t[i:j + 1])
                except Exception:
                    return {}
    return {}


def _log(msg: str) -> None:
    print(f"[cowork] {msg}", file=sys.stderr)


def _stream_testo(testo: str) -> Iterator[str]:
    """Il testo finale esce a blocchi come deltas: l'interfaccia esistente
    lo rende senza modifiche."""
    for i in range(0, len(testo), 400):
        yield _sse({"type": "delta", "text": testo[i:i + 400]})


def run(dept: str, uid: str, domanda: str, settings: dict,
        anon_names: list) -> Iterator[str]:
    """Esegue l'attività e produce eventi SSE (status/delta/sources/done)."""
    anon = Anonymizer()  # UNA mappa per l'intera attività: segnaposto coerenti tra i passi
    nomi = [d.get("name", "") for d in knowledge.kb_list(dept)]
    if not nomi:
        yield _sse({"type": "error", "text":
                    "La Conoscenza del tuo dipartimento è vuota: la modalità "
                    "Attività lavora sui documenti caricati in Conoscenza."})
        return
    memoria = [f"COMPITO: {domanda}",
               "DOCUMENTI DISPONIBILI (usa questi nomi esatti):\n- " + "\n- ".join(nomi)]
    letture, ricerche, file_generato = [], 0, ""
    # Il compito chiede un file Word/PDF? Se sì, il documento è GARANTITO:
    # nudge all'ultimo passo e, se non basta, composizione in finalizzazione
    # (fuori dal budget passi). Caso Carlos: 8 passi spesi in ricerca e
    # "componi" mai raggiunto — contenuto pronto, file mai prodotto.
    doc_richiesto = docgen.detect_request(domanda) in ("docx", "pdf")
    _tpl = docgen.get_user_template(uid, "docx")
    tpl_docx, tpl_nome = (_tpl[0], _tpl[1]) if _tpl else (None, "")

    for passo in range(1, MAX_PASSI + 1):
        yield _sse({"type": "status",
                    "text": f"Attività · passo {passo}/{MAX_PASSI}: pianifico…"})
        if passo == MAX_PASSI and doc_richiesto and not file_generato:
            memoria.append("[ULTIMO PASSO DISPONIBILE: il compito richiede un "
                           "documento Word — usa ORA l'azione 'componi' con il "
                           "contenuto già raccolto.]")
        contesto = "\n\n".join(memoria)[-MEMORIA_MAX_CHARS:]
        try:
            contesto_anon = anon.anonymize_names(anon.anonymize(contesto),
                                                 anon_names, use_nlp=False)
            raw = complete(PROTOCOLLO, contesto_anon, settings, max_tokens=2500)
            raw = anon.restore(raw)
        except Exception as e:
            _log(f"errore modello al passo {passo}: {type(e).__name__}: {e}")
            yield _sse({"type": "error", "text": f"Attività interrotta: {e}"})
            return
        az = _estrai_json(raw)
        azione = (az.get("azione") or "").strip().lower()
        _log(f"uid={uid} passo={passo} azione={azione or 'NON RICONOSCIUTA'}")

        if azione == "cerca":
            q = str(az.get("query") or domanda)[:300]
            ricerche += 1
            yield _sse({"type": "status",
                        "text": f"Attività · passo {passo}/{MAX_PASSI}: cerco «{q[:60]}»…"})
            testo, _fonti = knowledge.kb_search(dept, q)
            memoria.append(f"[RISULTATO RICERCA «{q}»]\n{(testo or 'nessun risultato')[:6000]}")
            continue

        if azione == "leggi":
            nome = str(az.get("documento") or "").strip()
            yield _sse({"type": "status",
                        "text": f"Attività · passo {passo}/{MAX_PASSI}: leggo «{nome[:60]}»…"})
            p = knowledge.kb_file_path(dept, nome)
            if p.is_file():
                try:
                    testo = knowledge.extract_text(p.name, p.read_bytes())[:LETTURA_MAX_CHARS]
                except Exception as e:
                    testo = f"(errore di lettura: {e})"
            else:
                testo = ("(originale non archiviato: documento caricato prima "
                         "dell'archiviazione — usa 'cerca' per i suoi contenuti)")
            letture.append(nome)
            memoria.append(f"[DOCUMENTO INTEGRALE: {nome}]\n{testo}")
            continue

        if azione == "componi":
            spec = az.get("spec") or {}
            titolo = str(spec.get("title") or "Documento")[:120]
            yield _sse({"type": "status",
                        "text": f"Attività · passo {passo}/{MAX_PASSI}: compongo «{titolo[:60]}»…"})
            try:
                path, fname = docgen.gen_docx(spec, template_path=tpl_docx)
                token = connectors.register_download(uid, path, fname)
                file_generato = fname
                yield _sse({"type": "sources", "items": [
                    {"name": fname, "url": "/download/" + token, "kind": "download"}]})
                memoria.append(f"[DOCUMENTO GENERATO: {fname} — ora RISPONDI riassumendo cosa contiene]")
            except Exception as e:
                _log(f"componi fallito: {type(e).__name__}: {e}")
                memoria.append(f"[COMPOSIZIONE FALLITA: {e} — rispondi comunque col contenuto in testo]")
            continue

        if azione == "rispondi":
            testo = str(az.get("testo") or "").strip()
            if not testo:
                memoria.append("[RISPOSTA VUOTA: riprova con 'rispondi' e un testo completo]")
                continue
            _log(f"uid={uid} conclusa in {passo} passi (letture={len(letture)}, "
                 f"ricerche={ricerche}, file={file_generato or '-'})")
            yield from _stream_testo(testo)
            _tpl_nota = f" · template: {tpl_nome}" if (file_generato and tpl_nome) else ""
            yield _sse({"type": "delta", "text":
                        f"\n\n🧭 *Attività: {passo} pass{'o' if passo == 1 else 'i'}, "
                        f"{len(letture)} document{'o letto' if len(letture) == 1 else 'i letti'}, "
                        f"{ricerche} ricerch{'a' if ricerche == 1 else 'e'}{_tpl_nota}.*"})
            yield _sse({"type": "done"})
            return

        memoria.append("[AZIONE NON RICONOSCIUTA: rispondi con UNA delle azioni "
                       "del protocollo, in JSON puro]")

    # passi esauriti: chiusura ONESTA con ciò che è stato raccolto
    _log(f"uid={uid} limite passi raggiunto (letture={len(letture)}, ricerche={ricerche})")
    if doc_richiesto and not file_generato:
        # FINALIZZAZIONE GARANTITA: il compito chiedeva un documento — una
        # chiamata dedicata (fuori dal budget passi) trasforma il materiale
        # raccolto nella spec e il file viene COMPOSTO comunque. Solo se anche
        # questa fallisce si torna alla dichiarazione onesta di parte mancante.
        yield _sse({"type": "status",
                    "text": "Attività · compongo il documento richiesto…"})
        try:
            contesto = "\n\n".join(memoria)[-MEMORIA_MAX_CHARS:]
            sys_spec = ("Componi ORA il documento Word richiesto dal COMPITO, "
                        "usando ESCLUSIVAMENTE il materiale raccolto qui sotto. " +
                        docgen._SCHEMAS["docx"])
            contesto_anon = anon.anonymize_names(anon.anonymize(contesto),
                                                 anon_names, use_nlp=False)
            raw = complete(sys_spec, contesto_anon, settings, max_tokens=3500)
            spec = docgen._parse_json(anon.restore(raw))
            path, fname = docgen.gen_docx(spec, template_path=tpl_docx)
            token = connectors.register_download(uid, path, fname)
            file_generato = fname
            yield _sse({"type": "sources", "items": [
                {"name": fname, "url": "/download/" + token, "kind": "download"}]})
            memoria.append(f"[DOCUMENTO GENERATO IN FINALIZZAZIONE: {fname} — "
                           "nella sintesi dichiara che il file è pronto al download]")
            _log(f"uid={uid} finalizzazione documento riuscita: {fname}")
        except Exception as e:
            _log(f"finalizzazione documento FALLITA: {type(e).__name__}: {e}")
            memoria.append(f"[FINALIZZAZIONE DOCUMENTO FALLITA: {e} — dichiara "
                           "la parte mancante nella sintesi]")
    yield _sse({"type": "status", "text": "Attività · sintesi finale…"})
    try:
        contesto = "\n\n".join(memoria)[-MEMORIA_MAX_CHARS:]
        contesto += ("\n\n[LIMITE PASSI RAGGIUNTO: rispondi ORA all'utente con la "
                     "migliore sintesi possibile di quanto raccolto, dichiarando "
                     "eventuali parti mancanti. Testo semplice, non JSON.]")
        contesto_anon = anon.anonymize_names(anon.anonymize(contesto),
                                             anon_names, use_nlp=False)
        finale = anon.restore(complete(PROTOCOLLO, contesto_anon, settings,
                                       max_tokens=2500))
    except Exception as e:
        finale = (f"Ho raggiunto il limite di {MAX_PASSI} passi e non sono "
                  f"riuscito a completare la sintesi finale ({e}).")
    yield from _stream_testo(finale)
    if file_generato:
        _tpl_nota = f" · template: {tpl_nome}" if tpl_nome else ""
        coda = (f"\n\n🧭 *Attività: limite di {MAX_PASSI} passi raggiunto — "
                f"documento composto in finalizzazione ({file_generato}){_tpl_nota}.*")
    else:
        coda = (f"\n\n🧭 *Attività: limite di {MAX_PASSI} passi raggiunto — "
                "sintesi parziale dichiarata.*")
    yield _sse({"type": "delta", "text": coda})
    yield _sse({"type": "done"})

"""
motore.py — Costruzione del motore Presidio per l'italiano e analisi dei segmenti.

Scelte (contratto app/sidecar, decisioni del CISO):
- modello spaCy it_core_news_md 3.8.0 (CC BY-NC-SA, accettata per uso interno);
- registro ESPLICITO: niente load_predefined_recognizers e niente
  AnalyzerEngine() di default, che proverebbe a scaricare en_core_web_lg;
- dal NER si prende solo PER -> PERSON; LOC, ORG e MISC restano leggibili;
- telefoni con phonenumbers sulle regioni IT, CH, DE, FR, ES, GB, US, AT, RO;
- carta d'identità, passaporto e patente solo con il contesto (contesto.py);
- una regex di Presidio che va in timeout NON passa in silenzio: errore 500.

Il motore si costruisce una volta (con gunicorn --preload, nel processo master)
e si condivide in sola lettura fra le analisi.
"""
from __future__ import annotations

import logging
import threading
import time
from importlib import metadata

from . import contesto

log = logging.getLogger("presidio_svc")

MODELLO = "it_core_news_md"
LINGUA = "it"
REGIONI = ("IT", "CH", "DE", "FR", "ES", "GB", "US", "AT", "RO")
ENTITA = ["PERSON", "PHONE_NUMBER", *contesto.TIPI_DOCUMENTO]
CONTESTO_TELEFONO = ["telefono", "tel", "cellulare", "cell", "numero", "chiamare",
                     "phone", "mobile"]
NLP = {
    "nlp_engine_name": "spacy",
    "models": [{"lang_code": LINGUA, "model_name": MODELLO}],
    "ner_model_configuration": {
        "model_to_presidio_entity_mapping": {"PER": "PERSON", "LOC": "LOCATION",
                                             "ORG": "ORGANIZATION", "MISC": "MISC"},
        "labels_to_ignore": ["LOCATION", "ORGANIZATION", "MISC"],
    },
}

# Testi lunghi: spaCy chiede memoria temporanea proporzionale al testo (circa
# 1 GB ogni 100.000 caratteri col parser, secondo la sua documentazione) e il
# container ha un tetto di 1 GB. Si analizza a finestre di BLOCCO caratteri con
# SOVRAPPOSIZIONE a sinistra e a destra: di ogni finestra si tengono solo i
# risultati che INIZIANO nella sua parte centrale, così un'entità a cavallo del
# taglio la trova intera la finestra vicina e i pezzi troncati si scartano.
BLOCCO = 10_000
SOVRAPPOSIZIONE = 200
# Caratteri al massimo per lotto di nlp.pipe. Misurato sul Mac con 200 KB:
# 12.000 dà già tutto il guadagno di velocità (+20% sui paragrafi brevi) con
# un picco di RSS di 430-440 MB; 50.000 non è più veloce e arriva a 840 MB.
LOTTO_CARATTERI = 12_000


class RegexTimeout(Exception):
    """Una regex di Presidio è andata oltre REGEX_TIMEOUT_SECONDS."""


class RichiestaScaduta(Exception):
    """Oltre la scadenza del client: iseopilot ha già proceduto senza Presidio,
    finire l'analisi occuperebbe la CPU per niente."""


# ── Timeout delle regex ─────────────────────────────────────
# Presidio, se una regex va in timeout, salta il pattern con un warning
# (pattern_recognizer.py:272-278): cioè lascia passare il dato. Il filtro
# intercetta il messaggio sul logger «presidio-analyzer» e segna la richiesta
# del thread corrente, che poi risponde 500. Il record non viene scritto: può
# contenere nomi di pattern e traceback, non serve nel log del servizio.
_stato = threading.local()


class _FiltroTimeout(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            messaggio = record.getMessage()
        except Exception:
            messaggio = str(record.msg)
        if "timed out" in messaggio:
            _stato.timeout = True
            return False
        return True


def _installa_filtro() -> None:
    lg = logging.getLogger("presidio-analyzer")
    # Con un livello sopra WARNING il filtro non verrebbe mai chiamato.
    lg.setLevel(logging.WARNING)
    if not any(isinstance(f, _FiltroTimeout) for f in lg.filters):
        lg.addFilter(_FiltroTimeout())


# ── Costruzione ─────────────────────────────────────────────
class Motore:
    def __init__(self):
        import spacy
        from presidio_analyzer import AnalyzerEngine, RecognizerRegistry
        from presidio_analyzer.nlp_engine import NlpEngineProvider
        from presidio_analyzer.predefined_recognizers import (
            ItDriverLicenseRecognizer, ItIdentityCardRecognizer, ItPassportRecognizer,
            PhoneRecognizer, SpacyRecognizer)

        _installa_filtro()
        # SpacyNlpEngine.load() scarica il modello se manca (spacy.cli.download):
        # qui la rete non c'è e non deve servire. Modello assente = avvio fallito.
        if not spacy.util.is_package(MODELLO):
            raise RuntimeError(f"modello {MODELLO} non installato")
        t0 = time.monotonic()
        nlp = NlpEngineProvider(nlp_configuration=NLP).create_engine()
        # Il parser sintattico non serve a Presidio (usa token, lemmi ed entità)
        # e da solo costa circa il 13% del tempo: si toglie. NER e lemmi non
        # dipendono da lui, i risultati restano identici (misurato su 200 KB).
        pipeline = nlp.get_nlp(LINGUA)
        if "parser" in pipeline.pipe_names:
            pipeline.remove_pipe("parser")
        registro = RecognizerRegistry(
            recognizers=[
                SpacyRecognizer(supported_language=LINGUA, supported_entities=["PERSON"]),
                PhoneRecognizer(supported_language=LINGUA, supported_regions=REGIONI,
                                context=CONTESTO_TELEFONO),
                ItIdentityCardRecognizer(supported_language=LINGUA),
                ItPassportRecognizer(supported_language=LINGUA),
                ItDriverLicenseRecognizer(supported_language=LINGUA),
            ],
            supported_languages=[LINGUA])
        self.engine = AnalyzerEngine(nlp_engine=nlp, registry=registro,
                                     supported_languages=[LINGUA],
                                     default_score_threshold=0,
                                     log_decision_process=False)
        # Controllo di coerenza: ogni entità del contratto ha un riconoscitore.
        # Presidio ignora in silenzio le entità che nessuno serve (issue #2256).
        servite = set(self.engine.get_supported_entities(language=LINGUA))
        mancanti = set(ENTITA) - servite
        if mancanti:
            raise RuntimeError(f"entità senza riconoscitore: {sorted(mancanti)}")
        meta = nlp.get_nlp(LINGUA).meta
        self.versione = f"presidio-analyzer {metadata.version('presidio-analyzer')}"
        self.modello = f"{MODELLO}-{meta.get('version', '?')}"
        self.ms_caricamento = int((time.monotonic() - t0) * 1000)
        # Una analisi alla volta: le memory_zone di spaCy (sotto) non si possono
        # sovrapporre fra thread, e con 1 CPU due analisi insieme non vanno più
        # veloci di due in fila.
        self._lock = threading.Lock()

    # ── Analisi ──────────────────────────────────────────────
    @staticmethod
    def _finestre(testo: str):
        """(da, a, centro_da, centro_a): la finestra testo[da:a] e la parte
        centrale in cui un risultato deve INIZIARE per essere tenuto."""
        if len(testo) <= BLOCCO + 2 * SOVRAPPOSIZIONE:
            yield 0, len(testo), 0, len(testo)
            return
        for centro in range(0, len(testo), BLOCCO):
            yield (max(0, centro - SOVRAPPOSIZIONE),
                   min(len(testo), centro + BLOCCO + SOVRAPPOSIZIONE),
                   centro, centro + BLOCCO)

    def analizza(self, segmenti: list[tuple[str, str]],
                 scadenza: float | None = None) -> dict[str, list[list]]:
        """{id: [[inizio, fine, tipo, punteggio], ...]} sul testo di ogni
        segmento (indici di stringa Python, fine esclusa). Solleva RegexTimeout,
        e RichiestaScaduta se `scadenza` (time.monotonic) passa fra un lotto e
        l'altro.

        spaCy lavora a lotti (nlp.pipe): con tanti paragrafi brevi, come li
        manda iseopilot, è circa il 20% più veloce che uno per uno. Un lotto
        non supera LOTTO_CARATTERI, così la memoria resta limitata.

        Tutto dentro nlp.memory_zone(): senza, spaCy aggiunge al vocabolario
        ogni parola mai vista (codici, matricole, cognomi, refusi) e non la
        libera più; il worker cresceva oltre il tetto di 1 GB del container.
        Dentro la zona i risultati diventano subito numeri e stringhe Python."""
        with self._lock, self.engine.nlp_engine.get_nlp(LINGUA).memory_zone():
            grezzi = self._analizza_in_zona(segmenti, scadenza)
        # Filtro sul testo INTERO del segmento: il contesto dei documenti guarda
        # i 40 caratteri prima, che possono stare nella finestra precedente.
        return {sid: [[s, e, tipo, round(p, 4)] for s, e, tipo, p in contesto.filtra(testo, grezzi[sid])]
                for sid, testo in segmenti}

    def _analizza_in_zona(self, segmenti, scadenza):
        _stato.timeout = False
        if scadenza is not None and time.monotonic() > scadenza:
            raise RichiestaScaduta()
        lavori = []                       # (id, da, centro_da, centro_a, pezzo)
        for sid, testo in segmenti:
            if testo.strip():
                for da, a, c_da, c_a in self._finestre(testo):
                    lavori.append((sid, da, c_da, c_a, testo[da:a]))
        grezzi: dict[str, list[tuple[int, int, str, float]]] = {sid: [] for sid, _ in segmenti}
        nlp = self.engine.nlp_engine
        lotto: list = []
        caratteri = 0
        for i, lavoro in enumerate(lavori):
            lotto.append(lavoro)
            caratteri += len(lavoro[4])
            if i + 1 < len(lavori) and caratteri + len(lavori[i + 1][4]) <= LOTTO_CARATTERI:
                continue
            artefatti = nlp.process_batch([l[4] for l in lotto], LINGUA, batch_size=len(lotto))
            for (sid, da, c_da, c_a, pezzo), (_, art) in zip(lotto, artefatti):
                for r in self.engine.analyze(text=pezzo, language=LINGUA, entities=ENTITA,
                                             nlp_artifacts=art):
                    inizio, fine = int(r.start) + da, int(r.end) + da
                    if c_da <= inizio < c_a:
                        grezzi[sid].append((inizio, fine, str(r.entity_type), float(r.score)))
            lotto, caratteri = [], 0
            if scadenza is not None and time.monotonic() > scadenza:
                raise RichiestaScaduta()
        if getattr(_stato, "timeout", False):
            raise RegexTimeout()
        return grezzi

    def analizza_segmento(self, testo: str) -> list[list]:
        return self.analizza([("s", testo)])["s"]

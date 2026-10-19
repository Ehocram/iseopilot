"""
anonymizer.py — Protezione dei dati prima dell'invio a Claude.

Portato dalla versione desktop con UNA differenza fondamentale per il web:
NON è un singleton globale. Sul desktop un'unica istanza `_anonymizer` veniva
resettata a ogni invio: corretto con un solo utente. Su un server con richieste
concorrenti, un'istanza condivisa farebbe trapelare la mappa di un utente nella
risposta di un altro. Qui ogni richiesta crea la propria istanza isolata.

I dati non diventano anonimi: vengono sostituiti con codici ([NOME_001],
[PERSONA_001], [EMAIL_001]…) e la mappa codice->valore vive solo in memoria,
per la durata della richiesta, per ripristinare i valori nella risposta.

Ordine dei livelli in `mask_batch` (un lotto di testi, UNA mappa):
  1. segnaposto letterali scritti dall'utente neutralizzati: [NOME_001] -> (NOME_001);
  2. regex per i dati tecnici (IP, email, CF anche minuscolo/omocodico, P.IVA…);
  3. Presidio (sidecar, una sola chiamata HTTP per tutto il lotto) sul testo
     dopo le SOLE regex, cioè PRIMA del dizionario: su «[NOME_001] Ferraro» il
     NER non vede più una persona e il secondo cognome passerebbe in chiaro.
     Il NER riceve una copia della stessa lunghezza con TAB, «;», «|» e «_»
     come spazi e le parole TUTTE MAIUSCOLE con la sola iniziale maiuscola
     (tabelle Excel/CSV/markdown ed elenchi in maiuscolo);
  4. dizionario dei nomi riservati (admin, uguale per tutti): UNA regex
     compilata; i suoi intervalli si UNISCONO a quelli di Presidio (gli
     intervalli sovrapposti diventano un solo dato);
  5. propagazione: ogni valore trovato da Presidio viene sostituito anche nelle
     sue altre occorrenze, senza distinguere maiuscole e separatori, in tutti
     i testi del lotto.

Fuga del dizionario chiusa (sondaggio «ripeti [NOME_001] [NOME_002]»): i codici
si creano SOLO per le voci presenti nel testo e il ripristino tocca SOLO i codici
emessi da questa istanza.
"""
from __future__ import annotations

import hashlib
import re as _re
import sys
import threading

from . import presidio_client

# Segnaposto emessi da noi: [TIPO_NNN], tipo maiuscolo che può contenere cifre
# (IPV6) e almeno 3 cifre (oltre 999 valori dello stesso tipo il contatore
# continua: [TEL_1000], [TEL_10005]).
_SEGNAPOSTO = _re.compile(r"\[([A-Z][A-Z0-9]{1,13})_(\d{3,})\]")
# Segnaposto scritti dall'utente (o rimasti nella cronologia): si rende inerte
# SOLO la forma esatta che il ripristino tradurrebbe. riga[col_1] o
# Sales[Anno_2024] restano come sono: il ripristino non li tocca comunque.
_LETTERALE = _SEGNAPOSTO
# Coda di uno streaming che PUÒ essere l'inizio di un segnaposto: si trattiene.
_CODA_APERTA = _re.compile(r"\[(?:[A-Z][A-Z0-9]{0,13}(?:_\d{0,8})?)?$")
_CODA_MAX = 24

_APOSTROFI = "'’ʼ"
# Una lettera (qualunque alfabeto): i confini delle voci sono fra lettere e non
# lettere, così «_», cifre e punteggiatura valgono come separatori
# (CV_Laura_Bianchi.pdf, laura_bianchi, rossi2024).
_LETTERA = r"[^\W\d_]"
# Fra le parole di una voce: spazi, «_», «.», «-» o niente (LauraBianchi).
_SEP_VOCE = r"[\s_.\-]*"
# Fra le parole di un valore propagato: i separatori che il NER vede come spazi.
_SEP_PROPAGA = r"[\s;|_]+"
# Cifre del CF nelle posizioni numeriche, con le lettere dell'omocodia.
_CF_CIFRA = r"[\dLMNPQRSTUVlmnpqrstuv]"

# Tipi di Presidio -> tipo del segnaposto.
TIPI_PRESIDIO = {
    "PERSON": "PERSONA",
    "PHONE_NUMBER": "TEL",
    "IT_IDENTITY_CARD": "DOCUMENTO",
    "IT_PASSPORT": "DOCUMENTO",
    "IT_DRIVER_LICENSE": "DOCUMENTO",
}
# Valori di Presidio più corti di così non si propagano: troppe coincidenze.
PROPAGAZIONE_MIN = 4
# Non sono persone (stessa allow-list del sidecar, confronto esatto sul testo
# originale): il NER riceve il testo normalizzato e «ISEO» resta com'è.
ALLOW_PERSONE = frozenset({
    "ISEO", "ISEOPilot", "Claude", "Anthropic", "Presidio", "Microsoft",
    "OneDrive", "SharePoint", "Teams", "Dynamics", "Power BI", "Excel", "Word",
    "PowerPoint", "Outlook", "Copilot", "Sentinella",
})

# Regex del dizionario in cache per hash: si ricompila solo quando l'admin lo cambia.
_DIZ_CACHE: dict = {}
_DIZ_LOCK = threading.Lock()


def _chiave(val: str) -> str:
    """Forma normalizzata di un valore: stesso valore => stesso segnaposto
    («Mario Rossi», «mario  rossi», «D’Angelo» e «D'Angelo» sono uno solo).
    «_» e «.» restano: «Laura_Bianchi» in un nome di file ha il suo codice e
    torna identico, così il file si ritrova."""
    v = _re.sub(r"\s+", " ", (val or "").strip())
    v = _re.sub(r"[’ʼ]", "'", v)
    return v.casefold()


def neutralizza_letterali(testo: str) -> str:
    """[NOME_001] scritto dall'utente diventa (NOME_001): non viene più
    ripristinato, quindi non può far leggere il dizionario."""
    return _LETTERALE.sub(lambda m: f"({m.group(1)}_{m.group(2)})", testo or "")


def _fuori_segnaposto(testo: str, fn) -> str:
    """Applica fn ai soli tratti di testo FUORI dai segnaposto [TIPO_NNN]:
    una voce come «Tel» o «Nome» non deve mai toccare un codice già emesso."""
    pezzi, pos = [], 0
    for m in _SEGNAPOSTO.finditer(testo):
        pezzi.append(fn(testo[pos:m.start()]))
        pezzi.append(m.group(0))
        pos = m.end()
    pezzi.append(fn(testo[pos:]))
    return "".join(pezzi)


# ── Testo per il NER ────────────────────────────────────────
# Il NER di it_core_news_md non vede le persone nelle righe di una tabella
# («Giovanni\tVerdi\tAcquisti», «Giovanni;Verdi», «| Giovanni Verdi |») né negli
# elenchi in maiuscolo («VERDI GIOVANNI, NERI PAOLO»). A Presidio va una copia
# della STESSA lunghezza, così gli offset valgono sul testo originale.
_SEP_NER = str.maketrans({"\t": " ", ";": " ", "|": " ", "_": " "})
_PAROLA = _re.compile(r"[^\W\d_]{3,}")


def _iniziale(m) -> str:
    w = m.group(0)
    if not w.isupper() or w in ALLOW_PERSONE:
        return w
    # carattere per carattere: un minuscolo di lunghezza diversa resta com'è
    return w[0] + "".join(c.lower() if len(c.lower()) == 1 else c for c in w[1:])


def testo_per_ner(testo: str) -> str:
    """Copia del testo per Presidio, della stessa lunghezza; i segnaposto
    restano intatti."""
    out = _fuori_segnaposto(testo, lambda t: _PAROLA.sub(_iniziale, t.translate(_SEP_NER)))
    return out if len(out) == len(testo) else testo


def _voce_regex(voce: str) -> str:
    """Una voce del dizionario come regex: fra le parole spazi, «_», «.», «-»
    o nessun separatore; apostrofi come qualunque apostrofo (dritto,
    tipografico, modificatore)."""
    out = []
    for ch in _re.sub(r"\s+", " ", voce.strip()):
        if ch == " ":
            out.append(_SEP_VOCE)
        elif ch in _APOSTROFI:
            out.append("['’ʼ]")
        else:
            out.append(_re.escape(ch))
    return "".join(out)


def regex_dizionario(nomi):
    """UNA regex per tutto il dizionario. Voci deduplicate senza distinguere le
    maiuscole e ordinate per lunghezza decrescente: con «Mario» e «Mario Rossi»
    vince la voce più lunga e «Rossi» non resta in chiaro. I confini sono fra
    lettere e non lettere: «_» e le cifre separano (CV_Laura_Bianchi.pdf)."""
    voci: dict = {}
    for n in nomi or []:
        n = (n or "").strip()
        if n and _chiave(n) not in voci:
            voci[_chiave(n)] = n
    if not voci:
        return None
    ordinate = sorted(voci, key=lambda k: (-len(k), k))
    h = hashlib.sha256("\n".join(ordinate).encode("utf-8")).hexdigest()
    with _DIZ_LOCK:
        rx = _DIZ_CACHE.get(h)
    if rx is not None:
        return rx
    rx = _re.compile(f"(?<!{_LETTERA})(?:" + "|".join(_voce_regex(voci[k]) for k in ordinate)
                     + f")(?!{_LETTERA})", _re.IGNORECASE)
    with _DIZ_LOCK:
        if len(_DIZ_CACHE) >= 8:  # pochi dizionari diversi per processo
            _DIZ_CACHE.clear()
        _DIZ_CACHE[h] = rx
    return rx


def _chiave_propagazione(val: str) -> str:
    """Un valore di Presidio senza maiuscole e con un solo spazio fra le parole
    («Giovanni\\tVerdi», «GIOVANNI;VERDI» e «Giovanni Verdi» coincidono)."""
    parole = [p for p in _re.split(_SEP_PROPAGA, val.strip()) if p]
    return _re.sub(r"[’ʼ]", "'", " ".join(parole)).casefold()


class Anonymizer:
    PATTERNS = [
        ("IP",    _re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}(?:/\d{1,2})?\b")),
        ("IPV6",  _re.compile(r"\b(?:[0-9a-fA-F]{1,4}:){2,7}[0-9a-fA-F]{1,4}\b")),
        ("EMAIL", _re.compile(r"\b[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}\b")),
        ("CVE",   _re.compile(r"\bCVE-\d{4}-\d{4,7}\b", _re.IGNORECASE)),
        ("MAC",   _re.compile(r"\b(?:[0-9a-fA-F]{2}[:\-]){5}[0-9a-fA-F]{2}\b")),
        # CF anche minuscolo e omocodico; almeno una cifra vera, così una parola
        # di 16 lettere non viene presa per un codice fiscale.
        ("CF",    _re.compile(r"\b(?=[A-Za-z0-9]{16}\b)(?=[A-Za-z]{0,15}\d)[A-Za-z]{6}"
                              + _CF_CIFRA + "{2}[A-Za-z]" + _CF_CIFRA + "{2}[A-Za-z]"
                              + _CF_CIFRA + r"{3}[A-Za-z]\b")),
        ("PIVA",  _re.compile(r"\b(?:IT)?\d{11}\b")),
        # Telefoni italiani nei formati veri: cellulare 3xx (9-10 cifre) o fisso
        # 0x, con o senza +39/0039 e i separatori tipici. Un importo, una
        # matricola o un codice articolo di 6-8 cifre NON è un telefono: Claude
        # deve poterci fare i conti. Gli altri formati li riconosce Presidio.
        ("TEL",   _re.compile(r"(?<![\w+])(?:(?:\+|00)39[\s\-.]?)?"
                              r"(?:3\d{2}[\s\-.]?\d{3}[\s\-.]?\d{3,4}"
                              r"|0\d{1,3}[\s\-./]?\d{3,4}[\s\-.]?\d{2,4})(?!\w)")),
        ("HASH",  _re.compile(r"\b[0-9a-fA-F]{32}\b|\b[0-9a-fA-F]{40}\b|\b[0-9a-fA-F]{64}\b")),
        ("TOKEN", _re.compile(r"\b[A-Za-z0-9_\-]{32,}\b")),
        ("HOST",  _re.compile(r"\b[a-zA-Z][a-zA-Z0-9\-]{2,}\.[a-zA-Z][a-zA-Z0-9\-\.]{2,}\b")),
    ]

    LABELS = {
        "IP": "Indirizzi IP", "IPV6": "Indirizzi IPv6", "EMAIL": "Email",
        "CVE": "Codici CVE", "MAC": "MAC address", "CF": "Codici fiscali",
        "PIVA": "Partite IVA", "TEL": "Telefoni", "HASH": "Hash/digest",
        "TOKEN": "Token/chiavi", "HOST": "Hostname/FQDN",
        "NOME": "Nomi (dizionario)", "PERSONA": "Persone (Presidio)",
        "DOCUMENTO": "Documenti d'identità (Presidio)",
    }

    def __init__(self):
        self._map = {}       # token -> valore originale (prima forma vista)
        self._rev = {}       # valore normalizzato -> token
        self._counters = {}  # tipo -> contatore
        self._usati = {}     # token -> occorrenze sostituite nel lotto corrente

    def _next_token(self, tipo: str) -> str:
        self._counters[tipo] = self._counters.get(tipo, 0) + 1
        return f"[{tipo}_{self._counters[tipo]:03d}]"

    def _emetti(self, tipo: str, val: str) -> str:
        """Il segnaposto nasce QUI, solo per un valore davvero presente nel testo."""
        k = _chiave(val)
        tok = self._rev.get(k)
        if tok is None:
            tok = self._next_token(tipo)
            self._map[tok] = val
            self._rev[k] = tok
        self._usati[tok] = self._usati.get(tok, 0) + 1
        return tok

    # ── livelli deterministici ──────────────────────────────
    def _maschera_regex(self, text: str) -> str:
        result = text
        for tipo, pattern in self.PATTERNS:
            result = pattern.sub(lambda m, tipo=tipo: self._emetti(tipo, m.group(0)), result)
        return result

    def _maschera_dizionario(self, text: str, rx) -> str:
        if rx is None or not text:
            return text
        return _fuori_segnaposto(text, lambda t: rx.sub(lambda m: self._emetti("NOME", m.group(0)), t))

    def anonymize(self, text: str) -> str:
        """Segnaposto letterali neutralizzati + regex dei dati tecnici."""
        return self._maschera_regex(neutralizza_letterali(text))

    def anonymize_names(self, text: str, custom_names: list, use_nlp: bool = False) -> str:
        """Dizionario dei nomi riservati (gestito dall'admin, uguale per tutti).
        `use_nlp` resta per compatibilità: i nomi fuori dizionario li riconosce
        Presidio, dentro `mask_batch`."""
        return self._maschera_dizionario(text, regex_dizionario(custom_names))

    # ── dizionario + Presidio ───────────────────────────────
    def _applica_intervalli(self, text: str, rx, spans, valori: dict) -> str:
        """Dizionario e Presidio sul testo dopo le regex. Gli intervalli che
        toccano un segnaposto si scartano; quelli sovrapposti si UNISCONO in un
        solo dato («Laura Bianchi» dal dizionario e «Laura Bianchi Ferraro» da
        Presidio: esce un codice solo, Ferraro non resta in chiaro). Il tipo è
        quello dell'intervallo più lungo; a parità vince il dizionario. I codici
        si numerano nell'ordine di lettura, la sostituzione va da destra a
        sinistra per non spostare gli indici."""
        occupati = [(m.start(), m.end()) for m in _SEGNAPOSTO.finditer(text)]

        def _libero(a, b):
            return not any(a < fine and b > inizio for inizio, fine in occupati)
        candidati = []                    # (inizio, fine, tipo, da_presidio)
        if rx is not None:
            for m in rx.finditer(text):
                if _libero(m.start(), m.end()):
                    candidati.append((m.start(), m.end(), "NOME", False))
        for a, b, ent, _score in spans or ():
            tipo = TIPI_PRESIDIO.get(ent)
            if not tipo:
                continue
            # bordi ripuliti: sul testo del NER «;», «|» e TAB erano spazi
            while a < b and (text[a].isspace() or text[a] in ";|_"):
                a += 1
            while b > a and (text[b - 1].isspace() or text[b - 1] in ";|_"):
                b -= 1
            if a >= b or not _libero(a, b):
                continue
            if ent == "PERSON" and text[a:b] in ALLOW_PERSONE:
                continue
            candidati.append((a, b, tipo, True))
        gruppi = []                       # [inizio, fine, membri]
        for c in sorted(candidati, key=lambda c: (c[0], -(c[1] - c[0]))):
            if gruppi and c[0] < gruppi[-1][1]:
                gruppi[-1][1] = max(gruppi[-1][1], c[1])
                gruppi[-1][2].append(c)
            else:
                gruppi.append([c[0], c[1], [c]])
        sostituzioni = []
        for a, b, membri in gruppi:
            tipo = max(membri, key=lambda c: (c[1] - c[0], not c[3]))[2]
            val = text[a:b]
            sostituzioni.append((a, b, self._emetti(tipo, val)))
            if any(c[3] for c in membri) and len(val.strip()) >= PROPAGAZIONE_MIN:
                valori.setdefault(_chiave_propagazione(val), tipo)
        for a, b, tok in reversed(sostituzioni):
            text = text[:a] + tok + text[b:]
        return text

    def _propaga(self, testi: list, valori: dict) -> list:
        """Ogni valore trovato da Presidio va sostituito anche nelle sue altre
        occorrenze: il NER può vedere «Giovanni Verdi» in un paragrafo e non in
        un altro, ma il nome è lo stesso. Senza distinguere maiuscole e
        separatori («GIOVANNI VERDI», «Giovanni;Verdi»); ogni forma diversa ha
        il suo codice e torna identica."""
        if not valori:
            return testi
        ordinati = sorted(valori, key=lambda v: (-len(v), v))

        def _modello(chiave):
            return _SEP_PROPAGA.join(
                "".join("['’ʼ]" if ch in _APOSTROFI else _re.escape(ch) for ch in parola)
                for parola in chiave.split(" "))
        rx = _re.compile(f"(?<!{_LETTERA})(?:" + "|".join(_modello(v) for v in ordinati)
                         + f")(?!{_LETTERA})", _re.IGNORECASE)

        def _sost(m):
            tipo = valori.get(_chiave_propagazione(m.group(0)), "PERSONA")
            return self._emetti(tipo, m.group(0))
        return [_fuori_segnaposto(t, lambda p: rx.sub(_sost, p)) if t else t for t in testi]

    def mask_batch(self, segmenti: list, dizionario=None, presidio: bool = True) -> tuple[list, dict]:
        """Maschera un LOTTO di testi con UNA mappa: stesso valore, stesso
        segnaposto in tutta la richiesta. Ritorna (testi mascherati, esito):
          esito = {"presidio_ok": bool, "presidio_motivo": str,
                   "per_tipo": {tipo: valori distinti}, "totale": int,
                   "occorrenze": int}
        Presidio non disponibile NON blocca (decisione del CISO): restano
        dizionario e regex, e il chiamante avvisa l'utente. Con presidio=False
        (Attività dopo un primo guasto) non lo si richiama nemmeno."""
        self._usati = {}
        rx = regex_dizionario(dizionario)
        base = []
        for s in segmenti:
            t = s if isinstance(s, str) else ("" if s is None else str(s))
            base.append(self.anonymize(t) if t else t)

        presidio_ok, motivo, trovati = False, "", None
        if not presidio:
            motivo = "sospeso_nell_attivita"
        else:
            try:
                trovati = presidio_client.analizza([testo_per_ner(t) for t in base])
                presidio_ok = True
            except presidio_client.PresidioNonDisponibile as e:
                motivo = e.motivo
            except Exception as e:  # qualunque imprevisto vale come guasto, mai come blocco
                motivo = f"errore:{type(e).__name__}"
                print(f"[presidio] errore imprevisto nel client: {type(e).__name__}", file=sys.stderr)
        valori: dict = {}
        out = [self._applica_intervalli(t, rx, sp, valori) if t else t
               for t, sp in zip(base, trovati or [()] * len(base))]
        out = self._propaga(out, valori)

        per_tipo: dict = {}
        for tok in self._usati:
            tipo = tok.strip("[]").rsplit("_", 1)[0]
            per_tipo[tipo] = per_tipo.get(tipo, 0) + 1
        esito = {"presidio_ok": presidio_ok, "presidio_motivo": motivo,
                 "per_tipo": per_tipo, "totale": len(self._usati),
                 "occorrenze": sum(self._usati.values())}
        return out, esito

    # ── ripristino ──────────────────────────────────────────
    def restore(self, text: str) -> str:
        """Ripristina SOLO i segnaposto emessi da questa istanza: un codice
        inventato dal modello o scritto dall'utente resta com'è."""
        if not self._map or not text:
            return text
        return _SEGNAPOSTO.sub(lambda m: self._map.get(m.group(0), m.group(0)), text)

    def restore_stream(self, buffer: str, finale: bool = False) -> tuple[str, str]:
        """Streaming: ritorna (testo da emettere, già ripristinato; coda da
        trattenere). Si trattiene solo una coda che può ancora diventare un
        segnaposto, al massimo 24 caratteri: una «[» mai chiusa non blocca più
        lo streaming fino alla fine."""
        if not finale:
            m = _CODA_APERTA.search(buffer)
            if m and len(buffer) - m.start() <= _CODA_MAX:
                return self.restore(buffer[:m.start()]), buffer[m.start():]
        return self.restore(buffer), ""

    def restore_struttura(self, obj):
        """Ripristino foglia per foglia di una struttura JSON GIÀ interpretata:
        un valore con virgolette non può più rompere il parsing."""
        if isinstance(obj, str):
            return self.restore(obj)
        if isinstance(obj, list):
            return [self.restore_struttura(x) for x in obj]
        if isinstance(obj, dict):
            return {k: self.restore_struttura(v) for k, v in obj.items()}
        return obj

    def get_map(self) -> dict:
        return dict(self._map)

    def summary(self) -> str:
        if not self._map:
            return "Nessun dato sensibile rilevato."
        by_type = {}
        for tok in self._map:
            tipo = tok.strip("[]").rsplit("_", 1)[0]
            by_type[tipo] = by_type.get(tipo, 0) + 1
        return "\n".join(
            f"  {self.LABELS.get(t, t)}: {n}" for t, n in sorted(by_type.items())
        )

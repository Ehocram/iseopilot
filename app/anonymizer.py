"""
anonymizer.py — Anonimizzazione dati sensibili prima dell'invio a Claude.

Portato dalla versione desktop con UNA differenza fondamentale per il web:
NON è un singleton globale. Sul desktop un'unica istanza `_anonymizer` veniva
resettata a ogni invio: corretto con un solo utente. Su un server con richieste
concorrenti, un'istanza condivisa farebbe trapelare la mappa di un utente nella
risposta di un altro. Qui ogni richiesta crea la propria istanza isolata.

Logica invariata: regex per dati tecnici (IP, email, CF, P.IVA, hash, token...)
+ dizionario nomi propri gestito a livello admin (uguale per tutti) + spaCy NLP
opzionale. La mappa token->valore vive solo in memoria, per la durata della
richiesta, e serve a ripristinare i valori originali nella risposta.
"""
from __future__ import annotations

import re as _re


class Anonymizer:
    PATTERNS = [
        ("IP",    _re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}(?:/\d{1,2})?\b")),
        ("IPV6",  _re.compile(r"\b(?:[0-9a-fA-F]{1,4}:){2,7}[0-9a-fA-F]{1,4}\b")),
        ("EMAIL", _re.compile(r"\b[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}\b")),
        ("CVE",   _re.compile(r"\bCVE-\d{4}-\d{4,7}\b", _re.IGNORECASE)),
        ("MAC",   _re.compile(r"\b(?:[0-9a-fA-F]{2}[:\-]){5}[0-9a-fA-F]{2}\b")),
        ("CF",    _re.compile(r"\b[A-Z]{6}\d{2}[A-Z]\d{2}[A-Z]\d{3}[A-Z]\b")),
        ("PIVA",  _re.compile(r"\b(?:IT)?\d{11}\b")),
        ("TEL",   _re.compile(r"\b(?:\+?39[\s\-]?)?(?:0\d{1,4}[\s\-]?)?\d{6,8}\b")),
        ("HASH",  _re.compile(r"\b[0-9a-fA-F]{32}\b|\b[0-9a-fA-F]{40}\b|\b[0-9a-fA-F]{64}\b")),
        ("TOKEN", _re.compile(r"\b[A-Za-z0-9_\-]{32,}\b")),
        ("HOST",  _re.compile(r"\b[a-zA-Z][a-zA-Z0-9\-]{2,}\.[a-zA-Z][a-zA-Z0-9\-\.]{2,}\b")),
    ]

    LABELS = {
        "IP": "Indirizzi IP", "IPV6": "Indirizzi IPv6", "EMAIL": "Email",
        "CVE": "Codici CVE", "MAC": "MAC address", "CF": "Codici fiscali",
        "PIVA": "Partite IVA", "TEL": "Telefoni", "HASH": "Hash/digest",
        "TOKEN": "Token/chiavi", "HOST": "Hostname/FQDN",
        "NOME": "Nomi (dizionario)", "PERSONA": "Persone (NLP)", "AZIENDA": "Aziende (NLP)",
    }

    def __init__(self):
        self._map = {}       # token -> valore originale
        self._rev = {}       # valore originale -> token
        self._counters = {}  # tipo -> contatore

    def _next_token(self, tipo: str) -> str:
        self._counters[tipo] = self._counters.get(tipo, 0) + 1
        return f"[{tipo}_{self._counters[tipo]:03d}]"

    def anonymize(self, text: str) -> str:
        result = text
        for tipo, pattern in self.PATTERNS:
            def replace_match(m, tipo=tipo):
                val = m.group(0)
                if val in self._rev:
                    return self._rev[val]
                tok = self._next_token(tipo)
                self._map[tok] = val
                self._rev[val] = tok
                return tok
            result = pattern.sub(replace_match, result)
        return result

    def anonymize_names(self, text: str, custom_names: list, use_nlp: bool = False) -> str:
        result = text
        # Dizionario personalizzato (gestito dall'admin, uguale per tutti)
        for name in custom_names:
            name = (name or "").strip()
            if not name:
                continue
            if name in self._rev:
                tok = self._rev[name]
            else:
                tok = self._next_token("NOME")
                self._map[tok] = name
                self._rev[name] = tok
            result = _re.sub(
                r"(?<![\w])" + _re.escape(name) + r"(?![\w])",
                tok, result, flags=_re.IGNORECASE,
            )
        # spaCy NLP (opzionale)
        if use_nlp:
            try:
                import spacy
                try:
                    nlp = spacy.load("it_core_news_sm")
                except Exception:
                    try:
                        nlp = spacy.load("en_core_web_sm")
                    except Exception:
                        nlp = None
                if nlp:
                    doc = nlp(result)
                    ents = [
                        (e.start_char, e.end_char, e.text, e.label_)
                        for e in doc.ents
                        if e.label_ in ("PER", "PERSON", "ORG")
                    ]
                    for start, end, ent_text, label in sorted(ents, reverse=True):
                        if ent_text in self._rev:
                            tok = self._rev[ent_text]
                        else:
                            tipo = "PERSONA" if label in ("PER", "PERSON") else "AZIENDA"
                            tok = self._next_token(tipo)
                            self._map[tok] = ent_text
                            self._rev[ent_text] = tok
                        result = result[:start] + tok + result[end:]
            except ImportError:
                pass
            except Exception:
                pass
        return result

    def restore(self, text: str) -> str:
        result = text
        for tok, val in sorted(self._map.items(), key=lambda x: len(x[0]), reverse=True):
            result = result.replace(tok, val)
        return result

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

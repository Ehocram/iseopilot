"""
contesto.py — Regole applicate DOPO Presidio: soglie per tipo, allow-list sulle
persone e filtro di contesto sui documenti d'identità.

Il motore gira con soglia 0 e restituisce tutto; qui si decide cosa tenere.
Funzioni pure, senza Presidio né spaCy: si provano senza caricare il modello.
"""
from __future__ import annotations

import re

# Soglie minime per tipo (contratto app/sidecar). spaCy non dà un punteggio per
# entità: Presidio assegna 0,85 fisso a ogni PERSON. Il telefono parte da 0,4.
SOGLIE = {"PERSON": 0.5, "PHONE_NUMBER": 0.4}

# Non sono persone: prodotti e nomi che compaiono di continuo nelle chat ISEO.
# Confronto ESATTO sul testo trovato (come allow_list_match="exact" di
# Presidio): «Claude» si scarta, «Claude Martin» resta una persona.
ALLOW_PERSONE = frozenset({
    "ISEO", "ISEOPilot", "Claude", "Anthropic", "Presidio", "Microsoft",
    "OneDrive", "SharePoint", "Teams", "Dynamics", "Power BI", "Excel", "Word",
    "PowerPoint", "Outlook", "Copilot", "Sentinella",
})

# Documenti d'identità italiani: il punteggio base va da 0,01 a 0,2 e la regex
# prende qualunque «AB1234567» (codici articolo, matricole...). Si tengono SOLO
# se nei 40 caratteri precedenti c'è la parola che li annuncia.
TIPI_DOCUMENTO = ("IT_IDENTITY_CARD", "IT_PASSPORT", "IT_DRIVER_LICENSE")
FINESTRA_CONTESTO = 40
_APOSTROFI = "['’ʼ`]"
# Parola chiave -> tipo che annuncia (serve solo a scegliere fra due tipi sullo
# stesso intervallo: carta d'identità e passaporto hanno regex sovrapposte).
_PAROLE = (
    (re.compile(r"carta\s+d" + _APOSTROFI + r"\s*identit[àa]", re.I), "IT_IDENTITY_CARD"),
    (re.compile(r"carta\s+di\s+identit[àa]", re.I), "IT_IDENTITY_CARD"),
    (re.compile(r"\bCIE\b", re.I), "IT_IDENTITY_CARD"),
    (re.compile(r"\bpassaport[oi]\b", re.I), "IT_PASSPORT"),
    (re.compile(r"\bpatent[ei]\b", re.I), "IT_DRIVER_LICENSE"),
)


def tipi_annunciati(testo: str, inizio: int) -> dict[str, int]:
    """Tipi di documento annunciati nei 40 caratteri prima di `inizio`, con la
    distanza dell'ultima parola chiave (più è piccola, più è vicina)."""
    da = max(0, inizio - FINESTRA_CONTESTO)
    prima = testo[da:inizio]
    trovati: dict[str, int] = {}
    for rx, tipo in _PAROLE:
        for m in rx.finditer(prima):
            distanza = len(prima) - m.end()
            trovati[tipo] = min(distanza, trovati.get(tipo, distanza))
    return trovati


def filtra(testo: str, risultati: list[tuple[int, int, str, float]]
           ) -> list[tuple[int, int, str, float]]:
    """Applica soglie, allow-list e contesto. Input e output: (inizio, fine,
    tipo, punteggio) sul testo del segmento. Ordinato per posizione."""
    tenuti: list[tuple[int, int, str, float]] = []
    documenti: dict[tuple[int, int], list[tuple[str, float, dict[str, int]]]] = {}
    for inizio, fine, tipo, punteggio in risultati:
        if tipo in TIPI_DOCUMENTO:
            annunciati = tipi_annunciati(testo, inizio)
            if annunciati and punteggio > 0:
                documenti.setdefault((inizio, fine), []).append((tipo, punteggio, annunciati))
            continue
        soglia = SOGLIE.get(tipo)
        if soglia is None or punteggio < soglia:
            continue                      # tipo non previsto dal contratto: mai restituito
        if tipo == "PERSON" and testo[inizio:fine] in ALLOW_PERSONE:
            continue
        tenuti.append((inizio, fine, tipo, punteggio))
    # Stesso intervallo con più tipi di documento (CIE cartacea e passaporto
    # hanno la stessa regex): vince il tipo annunciato dalla parola chiave più
    # vicina («carta d'identità X, passaporto Y»: Y è un passaporto).
    for (inizio, fine), candidati in documenti.items():
        annunciati = candidati[0][2]
        scelto = min(candidati, key=lambda c: annunciati.get(c[0], FINESTRA_CONTESTO + 1))
        tenuti.append((inizio, fine, scelto[0], scelto[1]))
    tenuti.sort(key=lambda r: (r[0], r[1], r[2]))
    return tenuti

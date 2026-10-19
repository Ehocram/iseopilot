"""
selftest.py — Controllo di prontezza del motore (usato da /ready).

Un testo fisso deve far scattare OGNI entità configurata: una persona italiana,
un telefono nazionale e uno con +39, carta d'identità, passaporto e patente con
il loro contesto. Se un riconoscitore manca o non lavora, /ready resta 503 e
iseopilot procede con dizionario e regole, mostrando l'avviso in chat.
Nessun testo dell'utente: le frasi sono inventate e restano nel codice.
"""
from __future__ import annotations

TESTO = ("Il referente è Mario Rossi, telefono 030 1234567 oppure +39 333 1234567. "
         "Carta d'identità CA12345AB, passaporto YA1234567, patente MI1234567A.")

ATTESI = (
    ("Mario Rossi", "PERSON"),
    ("030 1234567", "PHONE_NUMBER"),
    ("+39 333 1234567", "PHONE_NUMBER"),
    ("CA12345AB", "IT_IDENTITY_CARD"),
    ("YA1234567", "IT_PASSPORT"),
    ("MI1234567A", "IT_DRIVER_LICENSE"),
)


def esegui(motore) -> tuple[bool, str]:
    """(esito, motivo). Il motivo nomina solo le entità mancanti."""
    try:
        trovati = {(TESTO[s:e], tipo) for s, e, tipo, _ in motore.analizza_segmento(TESTO)}
    except Exception as e:                       # noqa: BLE001 - qualunque guasto = non pronto
        return False, f"errore {type(e).__name__}"
    mancanti = [f"{tipo}:{valore}" for valore, tipo in ATTESI if (valore, tipo) not in trovati]
    if mancanti:
        return False, "non riconosciuti " + ", ".join(mancanti)
    return True, "ok"

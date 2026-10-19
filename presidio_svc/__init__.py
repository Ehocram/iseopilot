"""
presidio_svc — sidecar Presidio di ISEOPilot (container «iseopilot-presidio").

Servizio HTTP senza stato: riceve testo, restituisce solo intervalli
(inizio, fine, tipo, punteggio). Dizionario e mappa dei segnaposto restano
in iseopilot. Nessuna rete verso l'esterno: modello installato come wheel con
hash durante il build, nessun download a runtime.

Le variabili qui sotto vanno impostate PRIMA di importare presidio/spaCy:
tolgono ogni via di rete «nascosta» (lista dei suffissi di tldextract, hub di
Hugging Face) e fissano la CPU senza sondare torch.
"""
import os

os.environ.setdefault("TLDEXTRACT_PUBLIC_SUFFIX_LIST_URLS", "")
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
os.environ.setdefault("PRESIDIO_DEVICE", "cpu")
# Una regex di Presidio che va oltre questo tempo viene saltata con un warning:
# il filtro sul logger (motore.py) la trasforma in errore 500. 60 s (default)
# sono più del timeout del client in iseopilot (massimo 20 s).
os.environ.setdefault("REGEX_TIMEOUT_SECONDS", "10")

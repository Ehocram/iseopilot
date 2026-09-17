"""Chat sviluppatore: legge il codice e PROPONE una modifica.

Separazione dei poteri, deliberata: questo modulo non scrive mai sul
repository. Legge da un mount in SOLA LETTURA, produce una patch in formato
unified diff e la valida applicandola a una copia temporanea. Ad applicarla,
committarla e portarla in produzione e' un agente separato che gira
sull'host — cosi' l'applicazione non puo' modificare se stessa, e se una
modifica rompe l'avvio lo strumento per rimediare non e' dentro cio' che e'
rotto.

Il testo contenuto negli screenshot e' DATO, mai istruzione: e' scritto nel
prompt di sistema ed e' la ragione per cui questo modulo non esegue nulla.
"""
from __future__ import annotations

import difflib
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

# Repository montato in sola lettura (docker-compose: /opt/iseopilot:/repo:ro)
REPO = Path(os.environ.get("DEV_REPO_DIR", "/repo"))

# Estensioni leggibili: il resto e' rumore o binario
ESTENSIONI = {".py", ".html", ".css", ".js", ".json", ".md", ".txt", ".yml",
              ".yaml", ".toml", ".cfg", ".ini", ".sh", ".sql", ".env-example"}
ESCLUSE = {".git", "__pycache__", ".venv", "node_modules", "data", "data_test"}
MAX_FILE = 200_000          # oltre, si legge a finestre
MAX_RISULTATI_GREP = 60


class FuoriRepo(Exception):
    """Percorso che tenta di uscire dal repository."""


def _risolvi(rel: str) -> Path:
    """Percorso assoluto dentro il repo. Solleva se tenta di uscirne.

    Non e' paranoia astratta: il percorso arriva da un modello che a sua volta
    legge screenshot e messaggi, quindi va trattato come input non fidato.
    """
    p = (REPO / str(rel or "").lstrip("/")).resolve()
    if p != REPO.resolve() and REPO.resolve() not in p.parents:
        raise FuoriRepo(rel)
    return p


def repo_presente() -> bool:
    return REPO.is_dir() and (REPO / "app").is_dir()


# ─────────────────────────────────────────────────────────── lettura
def elenca(rel: str = "") -> list[dict]:
    """Contenuto di una cartella: nome, tipo e dimensione."""
    base = _risolvi(rel)
    if not base.is_dir():
        return []
    out = []
    for e in sorted(base.iterdir(), key=lambda x: (x.is_file(), x.name.lower())):
        if e.name in ESCLUSE or e.name.startswith("."):
            continue
        out.append({"nome": e.name, "tipo": "dir" if e.is_dir() else "file",
                    "byte": e.stat().st_size if e.is_file() else None})
    return out


def leggi(rel: str, da_riga: int = 1, righe: int = 400) -> dict:
    """Contenuto di un file, a finestre: i file lunghi non devono saturare
    il contesto, e il modello puo' chiedere la finestra successiva."""
    p = _risolvi(rel)
    if not p.is_file():
        return {"errore": f"{rel}: file inesistente"}
    if p.suffix and p.suffix not in ESTENSIONI:
        return {"errore": f"{rel}: estensione non leggibile ({p.suffix})"}
    testo = p.read_text(encoding="utf-8", errors="replace")
    tutte = testo.splitlines()
    da = max(1, int(da_riga))
    fetta = tutte[da - 1: da - 1 + max(1, int(righe))]
    return {"file": rel, "righe_totali": len(tutte), "da_riga": da,
            "contenuto": "\n".join(f"{da + i}\t{r}" for i, r in enumerate(fetta)),
            "altro_dopo": da - 1 + len(fetta) < len(tutte)}


def cerca(testo: str, sottocartella: str = "") -> list[dict]:
    """Ricerca testuale nel repo: il modo piu' rapido per orientarsi senza
    leggere tutto."""
    base = _risolvi(sottocartella)
    ago = str(testo or "")
    if not ago:
        return []
    out = []
    for p in base.rglob("*"):
        if len(out) >= MAX_RISULTATI_GREP:
            break
        if not p.is_file() or p.suffix not in ESTENSIONI:
            continue
        if any(x in p.parts for x in ESCLUSE):
            continue
        try:
            for n, riga in enumerate(p.read_text(encoding="utf-8",
                                                 errors="replace").splitlines(), 1):
                if ago in riga:
                    out.append({"file": str(p.relative_to(REPO)), "riga": n,
                                "testo": riga.strip()[:180]})
                    if len(out) >= MAX_RISULTATI_GREP:
                        break
        except OSError:
            continue
    return out


# ─────────────────────────────────────────────────────── proposta di patch
def costruisci_patch(modifiche: list[dict]) -> dict:
    """Da [{file, cerca, sostituisci}] produce un unified diff VALIDATO.

    La sostituzione e' letterale e deve corrispondere a un solo punto del
    file: se il testo cercato non c'e', o compare piu' volte, la modifica
    viene rifiutata invece di indovinare dove applicarla.
    """
    pezzi, toccati, errori = [], [], []
    for m in modifiche or []:
        rel = str(m.get("file") or "")
        cerca_t = m.get("cerca")
        sost = m.get("sostituisci")
        if not rel or cerca_t is None or sost is None:
            errori.append(f"{rel or '(senza file)'}: modifica incompleta")
            continue
        try:
            p = _risolvi(rel)
        except FuoriRepo:
            errori.append(f"{rel}: fuori dal repository")
            continue
        if not p.is_file():
            errori.append(f"{rel}: file inesistente")
            continue
        originale = p.read_text(encoding="utf-8", errors="replace")
        n = originale.count(cerca_t)
        if n == 0:
            errori.append(f"{rel}: il testo da sostituire non e' presente")
            continue
        if n > 1:
            errori.append(f"{rel}: il testo da sostituire compare {n} volte, "
                          f"serve piu' contesto per individuarlo")
            continue
        nuovo = originale.replace(cerca_t, sost, 1)
        diff = difflib.unified_diff(
            originale.splitlines(keepends=True), nuovo.splitlines(keepends=True),
            fromfile=f"a/{rel}", tofile=f"b/{rel}", n=3)
        testo = "".join(diff)
        if testo:
            pezzi.append(testo)
            toccati.append(rel)
    return {"patch": "".join(pezzi), "file": toccati, "errori": errori}


def verifica_patch(patch: str) -> dict:
    """Applica la patch a una COPIA e ne controlla la sintassi Python.

    Una patch che non si applica, o che produce un file che non compila, non
    deve nemmeno arrivare al pulsante di pubblicazione.
    """
    if not (patch or "").strip():
        return {"ok": False, "errore": "patch vuota"}
    tmp = tempfile.mkdtemp(prefix="devchat-")
    try:
        for nome in ("app", "templates", "static"):
            src = REPO / nome
            if src.is_dir():
                shutil.copytree(src, Path(tmp) / nome, dirs_exist_ok=True,
                                ignore=shutil.ignore_patterns(*ESCLUSE))
        r = subprocess.run(["patch", "-p1", "--forward", "--silent"],
                           input=patch, text=True, cwd=tmp,
                           capture_output=True, timeout=60)
        if r.returncode != 0:
            return {"ok": False, "errore": "la patch non si applica: "
                                           + (r.stderr or r.stdout)[:400]}
        rotti = []
        for rel in re.findall(r"^\+\+\+ b/(\S+)", patch, re.M):
            if not rel.endswith(".py"):
                continue
            f = Path(tmp) / rel
            if not f.is_file():
                continue
            c = subprocess.run(["python3", "-m", "py_compile", str(f)],
                               capture_output=True, text=True, timeout=60)
            if c.returncode != 0:
                rotti.append(f"{rel}: {(c.stderr or '').strip()[:300]}")
        if rotti:
            return {"ok": False, "errore": "sintassi Python non valida dopo la "
                                           "modifica:\n" + "\n".join(rotti)}
        return {"ok": True}
    except subprocess.TimeoutExpired:
        return {"ok": False, "errore": "verifica scaduta"}
    except Exception as e:
        return {"ok": False, "errore": f"verifica non riuscita: {type(e).__name__}: {e}"}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

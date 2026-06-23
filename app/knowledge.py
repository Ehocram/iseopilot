"""
knowledge.py — Livello conoscenza per la web app.

Riusa i moduli desktop senza modificarli:
  * engines/vector_db.py     -> ChromaDB. Collezione = dipartimento (kb_<area>).
  * engines/folder_index.py  -> ricerca FTS5 su cartelle locali o di rete.

Isolamento:
  * Ogni dipartimento ha la PROPRIA collezione ChromaDB. Un utente carica e
    interroga solo la collezione del suo dipartimento.
  * I dati ChromaDB e gli indici cartelle vivono sotto APP_DATA_DIR (volume),
    non nella home dell'utente.

Note dipendenze:
  * La ricerca CARTELLE su .txt/.md/.csv/.json funziona con la sola libreria
    standard (FTS5). PDF/DOCX/XLSX richiedono i pacchetti in
    requirements-connectors.txt (PyMuPDF/python-docx/openpyxl).
  * ChromaDB richiede `pip install -r requirements-connectors.txt`. Se assente,
    le funzioni KB ritornano un errore esplicito (nessun fallimento silenzioso).
"""
from __future__ import annotations

import os
import tempfile
import threading
from pathlib import Path

from . import store
from .engines import vector_db, folder_index

DATA_DIR = Path(os.environ.get("APP_DATA_DIR", "/data"))
CHROMA_DIR = DATA_DIR / "vectordb"
FOLDER_INDEX_DIR = DATA_DIR / "folder_index"
CHROMA_DIR.mkdir(parents=True, exist_ok=True)
FOLDER_INDEX_DIR.mkdir(parents=True, exist_ok=True)

# Reindirizza gli indici cartella sotto il volume dati (non nella home).
folder_index.INDEX_DIR = FOLDER_INDEX_DIR

# Estensioni accettate per l'upload in ChromaDB.
ALLOWED_UPLOAD_EXT = {".txt", ".md", ".csv", ".json", ".xml", ".py",
                      ".pdf", ".docx", ".xlsx", ".xls"}
# Gli allegati di chat accettano anche le presentazioni.
ALLOWED_ATTACH_EXT = ALLOWED_UPLOAD_EXT | {".pptx", ".ppt"}


# ── Configurazione ChromaDB per dipartimento ────────────────
def vector_cfg(dept: str) -> dict:
    """cfg per VectorDBManager: collezione = dipartimento; infra da admin."""
    return {
        "db_mode": store.get_setting("kb_mode", "local"),
        "db_embedding_model": store.get_setting("kb_embedding_model", "all-minilm-l6-v2"),
        "db_collection": store.collection_for_department(dept),
        "db_path": str(CHROMA_DIR),
        "db_host": store.get_setting("kb_host", "localhost"),
        "db_port": store.get_setting("kb_port", "8000"),
        "db_api_key": store.get_setting("kb_api_key", ""),
    }


def kb_available() -> bool:
    try:
        import chromadb  # noqa: F401
        return True
    except Exception:
        return False


# ── Estrazione testo dai file caricati ──────────────────────
def extract_text(filename: str, raw: bytes) -> str:
    """Estrae il testo da un file caricato (riusa l'estrattore desktop)."""
    ext = Path(filename).suffix.lower()
    if ext not in ALLOWED_UPLOAD_EXT:
        return ""
    suffix = ext or ".bin"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(raw)
        tmp_path = Path(tmp.name)
    try:
        return folder_index._extract_text(tmp_path, ext) or ""
    finally:
        try:
            tmp_path.unlink()
        except Exception:
            pass


def _extract_pptx_text(raw: bytes) -> str:
    """Testo di una presentazione: titoli, testo delle forme, note relatore."""
    try:
        from pptx import Presentation
        import io
        prs = Presentation(io.BytesIO(raw))
        out = []
        for i, slide in enumerate(prs.slides, 1):
            out.append(f"-- Slide {i} --")
            for shape in slide.shapes:
                if shape.has_text_frame:
                    for p in shape.text_frame.paragraphs:
                        t = "".join(r.text for r in p.runs).strip()
                        if t:
                            out.append(t)
                if shape.has_table:
                    for row in shape.table.rows:
                        cells = [c.text.strip() for c in row.cells]
                        out.append(" | ".join(cells))
            if slide.has_notes_slide and slide.notes_slide.notes_text_frame:
                note = slide.notes_slide.notes_text_frame.text.strip()
                if note:
                    out.append("[Note: " + note + "]")
        return "\n".join(out)
    except Exception:
        return ""


def extract_attachment_text(filename: str, raw: bytes) -> str:
    """Estrae il testo da un allegato di chat. Come extract_text ma include
    anche le presentazioni PowerPoint."""
    ext = Path(filename).suffix.lower()
    if ext in {".pptx", ".ppt"}:
        return _extract_pptx_text(raw)
    if ext in ALLOWED_UPLOAD_EXT:
        return extract_text(filename, raw)
    return ""


# ── Operazioni KB (ChromaDB) per dipartimento ───────────────
def kb_ingest(dept: str, filename: str, text: str) -> tuple[bool, str]:
    if not kb_available():
        return False, "ChromaDB non installato sul server (pip install -r requirements-connectors.txt)."
    if not text.strip():
        return False, f"Nessun testo estraibile da '{filename}'."
    vdb = vector_db.get_vdb(vector_cfg(dept))
    n, msg = vdb.add_document(text, filename)
    return (n > 0), msg


def kb_list(dept: str) -> list:
    if not kb_available():
        return []
    try:
        return vector_db.get_vdb(vector_cfg(dept)).list_documents()
    except Exception:
        return []


def kb_count(dept: str) -> int:
    if not kb_available():
        return 0
    try:
        return vector_db.get_vdb(vector_cfg(dept)).get_count()
    except Exception:
        return 0


def kb_delete(dept: str, filename: str) -> tuple[bool, str]:
    if not kb_available():
        return False, "ChromaDB non installato."
    try:
        ok, msg = vector_db.get_vdb(vector_cfg(dept)).delete_document(filename)
        return bool(ok), msg
    except Exception as e:
        return False, f"Errore eliminazione: {e}"


def kb_search(dept: str, query: str, n: int = 4) -> tuple[str, list]:
    if not kb_available():
        return "", []
    try:
        return vector_db.get_vdb(vector_cfg(dept)).search(query, n_results=n)
    except Exception:
        return "", []


# ── Ricerca cartelle (locali / di rete) ─────────────────────
def folder_reindex(path: str) -> tuple[bool, str]:
    p = (path or "").strip()
    if not p:
        return False, "Nessuna cartella configurata."
    if not Path(p).exists():
        return False, f"Percorso non raggiungibile dal server: {p}"
    try:
        idx = folder_index.get_index(p)
        res = idx.update()
        n = idx.count()
        return True, f"Indice aggiornato: {n} passaggi indicizzati."
    except Exception as e:
        return False, f"Errore indicizzazione: {e}"


def folder_count(path: str) -> int:
    p = (path or "").strip()
    if not p or not Path(p).exists():
        return 0
    try:
        res = folder_index.get_index(p).count()
        # FolderIndex.count() -> (num_file, num_passaggi); ci interessano i passaggi.
        return res[1] if isinstance(res, (tuple, list)) else int(res)
    except Exception:
        return 0


def folder_search(path: str, query: str, n: int = 4) -> tuple[str, list]:
    p = (path or "").strip()
    if not p or not Path(p).exists():
        return "", []
    try:
        return folder_index.search_folder(p, query, top_k=n, rerank=False)
    except Exception:
        return "", []


# ── Cartelle del dipartimento (più percorsi) ────────────────
def dept_folders_reindex(dept: str) -> tuple[bool, str]:
    paths = store.department_folders(dept)
    if not paths:
        return False, "Nessuna cartella configurata per il dipartimento."
    done, msgs = 0, []
    for p in paths:
        ok, msg = folder_reindex(p)
        if ok:
            done += 1
        else:
            msgs.append(msg)
    if done == 0:
        return False, " · ".join(msgs) or "Indicizzazione non riuscita."
    tail = (" (" + " · ".join(msgs) + ")") if msgs else ""
    return True, f"{done}/{len(paths)} cartelle indicizzate.{tail}"


def dept_folders_count(dept: str) -> int:
    return sum(folder_count(p) for p in store.department_folders(dept))


# ── Indicizzazione automatica (come l'app desktop: all'avvio + on demand) ──
def reindex_all() -> None:
    """Indicizza (incrementale) tutte le cartelle di tutti i dipartimenti.
    Le share di rete grandi possono richiedere tempo: va eseguita in background.
    L'update di FolderIndex è incrementale (mtime+size), quindi ai riavvii
    successivi rilegge solo i file nuovi o modificati."""
    seen = set()
    for dept in store.list_departments():
        for p in store.department_folders(dept):
            if p in seen:
                continue
            seen.add(p)
            try:
                folder_reindex(p)
            except Exception:
                pass


def reindex_folder_async(path: str) -> None:
    """Indicizza una singola cartella in un thread separato (non blocca la risposta)."""
    threading.Thread(target=folder_reindex, args=(path,), daemon=True).start()


def reindex_all_async() -> None:
    """Avvia in background l'indicizzazione di tutte le cartelle configurate."""
    threading.Thread(target=reindex_all, daemon=True).start()


# ── Recupero combinato per la chat (RAG) ────────────────────
def retrieve(dept: str, query: str, use_kb: bool = True, use_folder: bool = True,
             max_chars: int = 6000) -> str:
    """Contesto combinato secondo i toggle dell'utente: conoscenza ChromaDB del
    dipartimento e/o cartelle del dipartimento. Resiliente: in caso di errore in
    una sorgente, restituisce ciò che ha (la chat non deve fallire per il retrieval)."""
    parts = []
    if use_kb:
        kb_text, _ = kb_search(dept, query, n=4)
        if kb_text.strip():
            parts.append("[Conoscenza dipartimento — " + dept + "]\n" + kb_text)
    if use_folder:
        for p in store.department_folders(dept):
            f_text, _ = folder_search(p, query, n=3)
            if f_text.strip():
                parts.append("[Cartella: " + p + "]\n" + f_text)
    ctx = "\n\n".join(parts)
    return ctx[:max_chars]

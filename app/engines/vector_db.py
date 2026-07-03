#!/usr/bin/env python3
"""
VectorDB — modulo ChromaDB per Chat Assistant
Sviluppato da Marco Bonometti
Cache singleton per client e modello embedding — ricaricato una sola volta.
"""
import json
import datetime
import re
import os
import sys
from pathlib import Path

CHROMA_DIR = Path.home() / "Documents" / "ChatAssistant" / "vectordb"
CHROMA_DIR.mkdir(parents=True, exist_ok=True)

# ── Cache globale — evita reload del modello ad ogni query ──
_CLIENT_CACHE    = {}   # key: (mode, path/host) → chromadb client
_EMBEDDING_CACHE = {}   # key: model_name → embedding function
_COLLECTION_CACHE = {}  # key: (client_key, collection_name) → collection


def _bundled_models_dir() -> Path:
    """Cartella dei modelli embedding inclusi nell'app (bundle).
    In un'app PyInstaller le risorse stanno in sys._MEIPASS; in sviluppo
    si usa ./models accanto al sorgente. Ritorna il percorso (può non esistere)."""
    base = getattr(sys, "_MEIPASS", None)
    if base:
        return Path(base) / "models"
    return Path(__file__).resolve().parent / "models"


def resolve_embedding_model(name_or_alias: str) -> str:
    """Risolve il modello di embedding a un PERCORSO LOCALE se è incluso nel
    bundle, altrimenti al nome HuggingFace (che verrà scaricato al primo uso).

    Quando il modello è bundlato, attiva anche la modalità OFFLINE di
    HuggingFace/Transformers, così non viene tentata alcuna connessione di rete:
    l'app funziona senza download e senza internet. È pensato per il .dmg
    distribuito, dove vogliamo che il modello multilingue sia già presente."""
    aliases = {
        "all-minilm-l6-v2": "all-MiniLM-L6-v2",
        "multilingual": "paraphrase-multilingual-MiniLM-L12-v2",
        "paraphrase-multilingual": "paraphrase-multilingual-MiniLM-L12-v2",
        "multilingual-e5-large": "intfloat/multilingual-e5-large",
        "multilingual-e5": "intfloat/multilingual-e5-base",
    }
    real = aliases.get((name_or_alias or "").lower(), name_or_alias)
    # nome "sicuro" della cartella (l'eventuale prefisso org/ diventa _)
    folder = real.replace("/", "__")
    cand = _bundled_models_dir() / folder
    if cand.exists() and (cand / "config.json").exists():
        # modello presente nel bundle: usa il percorso locale e vai offline
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
        os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
        return str(cand)
    return real

def chunk_text(text: str, chunk_size: int = 800, overlap: int = 100) -> list:
    if not text or not text.strip():
        return []
    words = text.split()
    chunks = []
    i = 0
    while i < len(words):
        chunk = " ".join(words[i:i + chunk_size])
        if chunk.strip():
            chunks.append(chunk)
        i += chunk_size - overlap
        if i >= len(words):
            break
    return chunks


class VectorDBManager:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self._client     = None
        self._collection = None
        self._embedding_fn = None
        self._client_key   = None

    def is_available(self) -> bool:
        try:
            import chromadb
            return True
        except ImportError:
            return False

    def connect(self) -> tuple:
        try:
            import chromadb
            from chromadb.utils import embedding_functions

            mode     = self.cfg.get("db_mode", "local")
            emb_model = self.cfg.get("db_embedding_model", "all-minilm-l6-v2")
            coll_name = self.cfg.get("db_collection", "chat_assistant_kb")

            # [v2.0] Risoluzione alias -> modello reale. Se il modello è incluso nel
            # bundle (.dmg), resolve_embedding_model ritorna il PERCORSO LOCALE e
            # attiva la modalità offline: nessun download al primo avvio.
            real_model = resolve_embedding_model(emb_model)

            # [v2.0] NAMESPACE della collection per modello: gli embedding di modelli
            # diversi NON sono confrontabili (spazi/dimensioni diversi). Legare il nome
            # della collection al modello evita di interrogare con un modello vettori
            # creati con un altro (risultati falsati). Cambiare modello = nuova
            # collection pulita, da reindicizzare, invece di una corruzione silenziosa.
            # Uso il nome "logico" del modello (basename) per un tag stabile anche
            # quando real_model è un percorso assoluto del bundle.
            import os as _os
            model_ident = _os.path.basename(real_model.rstrip("/")) if ("/" in real_model or "\\" in real_model) else real_model
            # NB: il taglio a 24 caratteri può lasciare un "_" finale (successo
            # col modello multilingue: 'paraphrase_multilingual_') e ChromaDB
            # esige nomi che iniziano e finiscono con [a-zA-Z0-9]. Strip DOPO
            # il taglio + cintura finale sull'intero nome.
            model_tag = re.sub(r"[^a-z0-9]+", "_", model_ident.lower()).strip("_")[:24].strip("_")
            coll_name_eff = f"{coll_name}__{model_tag}".strip("._-") or "kb_default"

            # ── Cache client ────────────────────────────────
            if mode == "local":
                db_path = self.cfg.get("db_path", str(CHROMA_DIR))
                client_key = ("local", db_path)
            else:
                host = self.cfg.get("db_host", "localhost")
                port = self.cfg.get("db_port", "8000")
                client_key = ("remote", f"{host}:{port}")

            self._client_key = client_key

            if client_key not in _CLIENT_CACHE:
                if mode == "local":
                    _CLIENT_CACHE[client_key] = chromadb.PersistentClient(path=db_path)
                else:
                    api_key = self.cfg.get("db_api_key", "")
                    try:
                        port_num = int(str(port).strip())
                    except (ValueError, TypeError):
                        port_num = 8000  # default ChromaDB
                    kwargs = {"host": host, "port": port_num}
                    if api_key:
                        kwargs["headers"] = {"Authorization": f"Bearer {api_key}"}
                    _CLIENT_CACHE[client_key] = chromadb.HttpClient(**kwargs)

            self._client = _CLIENT_CACHE[client_key]

            # ── Cache embedding function ─────────────────────
            if real_model not in _EMBEDDING_CACHE:
                try:
                    _EMBEDDING_CACHE[real_model] = (
                        embedding_functions.SentenceTransformerEmbeddingFunction(
                            model_name=real_model
                        )
                    )
                except Exception:
                    _EMBEDDING_CACHE[real_model] = (
                        embedding_functions.DefaultEmbeddingFunction()
                    )

            self._embedding_fn = _EMBEDDING_CACHE[real_model]

            # ── Cache collection (namespaced per modello) ────
            coll_key = (client_key, coll_name_eff)
            if coll_key not in _COLLECTION_CACHE:
                _COLLECTION_CACHE[coll_key] = self._client.get_or_create_collection(
                    name=coll_name_eff,
                    embedding_function=self._embedding_fn,
                    metadata={"hnsw:space": "cosine"}
                )

            self._collection = _COLLECTION_CACHE[coll_key]
            return True, f"Connesso — {self._collection.count()} documenti"

        except ImportError:
            return False, "ChromaDB non installato. Esegui: pip install chromadb sentence-transformers"
        except Exception as e:
            return False, f"Errore connessione: {e}"

    def _ensure_connected(self):
        if not self._collection:
            ok, msg = self.connect()
            if not ok:
                raise RuntimeError(msg)

    def add_document(self, text: str, filename: str,
                     chunk_size: int = 800, overlap: int = 100) -> tuple:
        self._ensure_connected()
        try:
            chunks = chunk_text(text, chunk_size, overlap)
            if not chunks:
                return 0, "Nessun contenuto da indicizzare"
            # Rimuovi chunks esistenti
            try:
                existing = self._collection.get(where={"source": filename})
                if existing["ids"]:
                    self._collection.delete(ids=existing["ids"])
            except Exception:
                pass
            # ID Chroma-safe: dal nome file grezzo (spazi, parentesi, unicode)
            # a uno slug [a-zA-Z0-9._-] + hash breve per unicità. Il nome
            # ORIGINALE resta nel metadata 'source' (visualizzazione e delete).
            import hashlib as _hl
            _base = re.sub(r"[^a-zA-Z0-9._-]", "_", filename)[:180].strip("._-") or "doc"
            _sid = f"{_base}-{_hl.md5(filename.encode('utf-8')).hexdigest()[:8]}"
            ids   = [f"{_sid}__chunk_{i}" for i in range(len(chunks))]
            metas = [{"source": filename, "chunk": i,
                      "date": datetime.datetime.now().isoformat()} for i in range(len(chunks))]
            self._collection.add(documents=chunks, ids=ids, metadatas=metas)
            # Invalida cache collection per aggiornare conteggio
            coll_key = (self._client_key, self.cfg.get("db_collection", "chat_assistant_kb"))
            if coll_key in _COLLECTION_CACHE:
                del _COLLECTION_CACHE[coll_key]
            return len(chunks), f"Indicizzati {len(chunks)} chunk da '{filename}'"
        except Exception as e:
            return 0, f"Errore indicizzazione: {e}"

    def search(self, query: str, n_results: int = 5):
        """Ricerca semantica. Ritorna (testo_formattato, [(nome_file, path_assoluto), ...])."""
        self._ensure_connected()
        try:
            count = self._collection.count()
            if count == 0:
                return "", []
            results = self._collection.query(
                query_texts=[query],
                n_results=min(n_results, count)
            )
            if not results["documents"] or not results["documents"][0]:
                return "", []
            parts = []
            seen_sources = []  # mantiene ordine dei primi match
            for doc, meta in zip(results["documents"][0], results["metadatas"][0]):
                src = meta.get("source", "?")
                parts.append(f"[Fonte: {src}]\n{doc}")
                if src and src not in [s[0] for s in seen_sources]:
                    # Path assoluto del file: cerca nella cartella sorgente di indicizzazione
                    src_path = ""
                    if hasattr(self, "_last_source_dir") and self._last_source_dir:
                        candidate = Path(self._last_source_dir) / src
                        if candidate.exists():
                            src_path = str(candidate)
                    seen_sources.append((src, src_path))
            return "\n\n".join(parts), seen_sources
        except Exception as e:
            return "", []

    def list_documents(self) -> list:
        self._ensure_connected()
        try:
            all_items = self._collection.get()
            sources = {}
            for meta in all_items.get("metadatas", []):
                src = meta.get("source", "?")
                sources[src] = sources.get(src, 0) + 1
            return [{"name": k, "chunks": v} for k, v in sorted(sources.items())]
        except Exception:
            return []

    def delete_document(self, filename: str) -> tuple:
        self._ensure_connected()
        try:
            existing = self._collection.get(where={"source": filename})
            if existing["ids"]:
                self._collection.delete(ids=existing["ids"])
                return True, f"'{filename}' eliminato ({len(existing['ids'])} chunk)"
            return False, "Documento non trovato"
        except Exception as e:
            return False, str(e)

    def clear_all(self) -> tuple:
        self._ensure_connected()
        try:
            name = self._collection.name
            emb  = self._embedding_fn
            self._client.delete_collection(name)
            # Invalida cache
            coll_key = (self._client_key, name)
            if coll_key in _COLLECTION_CACHE:
                del _COLLECTION_CACHE[coll_key]
            self._collection = self._client.get_or_create_collection(
                name=name, embedding_function=emb,
                metadata={"hnsw:space": "cosine"}
            )
            _COLLECTION_CACHE[(self._client_key, name)] = self._collection
            return True, "DB svuotato"
        except Exception as e:
            return False, str(e)

    def get_count(self) -> int:
        try:
            self._ensure_connected()
            return self._collection.count()
        except Exception:
            return 0


# ── Istanza globale con cache ────────────────────────────
_vdb_instance = None
_vdb_cfg_key  = None

def get_vdb(cfg: dict) -> VectorDBManager:
    global _vdb_instance, _vdb_cfg_key
    # Ricrea solo se la config rilevante è cambiata
    cfg_key = (cfg.get("db_mode"), cfg.get("db_path"), cfg.get("db_host"),
               cfg.get("db_port"), cfg.get("db_collection"), cfg.get("db_embedding_model"))
    if _vdb_instance is None or cfg_key != _vdb_cfg_key:
        _vdb_instance = VectorDBManager(cfg)
        _vdb_cfg_key  = cfg_key
    else:
        _vdb_instance.cfg = cfg  # aggiorna cfg senza ricreare
    return _vdb_instance

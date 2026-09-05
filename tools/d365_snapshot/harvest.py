"""Raccolta dei metadati D365 F&O. Incrementale e ripartibile."""
from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from . import config
from .client import NotFound, Throttled

LANG_CANDIDATES = ["it", "it-IT", "en-US", "en"]


def _write(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1))
    tmp.replace(path)


def _read(path: Path, default=None):
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            return default
    return default


# --------------------------------------------------------------------- stadi
def companies(cli, raw: Path) -> list:
    p = raw / "companies.json"
    got = _read(p)
    if got:
        return got
    print("→ Societa' (legal entities)")
    v = cli.get_all("/data/Companies?$select=DataArea,Name", "societa")
    _write(p, v)
    return v


def data_entities(cli, raw: Path) -> list:
    p = raw / "data_entities.json"
    got = _read(p)
    if got:
        print(f"→ DataEntities (da cache): {len(got)}")
        return got
    print("→ DataEntities (catalogo completo, incluse le non-OData)")
    v = cli.get_all("/metadata/DataEntities", "data entities")
    _write(p, v)
    return v


def public_entities_index(cli, raw: Path) -> list:
    p = raw / "public_entities_index.json"
    got = _read(p)
    if got:
        print(f"→ PublicEntities indice (da cache): {len(got)}")
        return got
    print("→ PublicEntities (indice)")
    v = cli.get_all("/metadata/PublicEntities", "public entities")
    _write(p, v)
    return v


def enumerations(cli, raw: Path) -> list:
    p = raw / "enumerations.json"
    got = _read(p)
    if got:
        print(f"→ Enum (da cache): {len(got)}")
        return got
    print("→ PublicEnumerations (valori + etichette)")
    v = cli.get_all("/metadata/PublicEnumerations", "enum")
    _write(p, v)
    return v


def edmx(cli, raw: Path) -> str:
    p = raw / "metadata.edmx.xml"
    if p.exists() and p.stat().st_size > 1000:
        print("→ EDMX (da cache)")
        return p.read_text()
    print("→ EDMX /data/$metadata")
    try:
        txt = cli.get("/data/$metadata", timeout=300)
        if isinstance(txt, str):
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(txt)
            print(f"  EDMX: {len(txt)/1e6:.1f} MB")
            return txt
    except Exception as e:
        print(f"  EDMX non recuperato: {e}")
    return ""


def public_entity_details(cli, index: list, raw: Path,
                          only: set[str] | None = None) -> dict:
    """Dettaglio per entita': campi, tipi, chiavi, relazioni dichiarate.

    Scrive JSONL in append: interrompibile e ripartibile senza perdere lavoro.
    """
    out = raw / "public_entities.jsonl"
    done: dict[str, dict] = {}
    if out.exists():
        for line in out.read_text().splitlines():
            try:
                d = json.loads(line)
                done[d["Name"]] = d
            except Exception:
                pass

    names = [e["Name"] for e in index if e.get("Name")]
    if only:
        names = [n for n in names if n in only]

    # /metadata/PublicEntities restituisce gia' l'entita' COMPLETA nella
    # risposta di lista: Properties, NavigationProperties, Actions. Rifare una
    # GET per entita' significherebbe 4.707 chiamate identiche a dati che
    # abbiamo gia'. Si scarica solo cio' che manca davvero.
    lock = threading.Lock()
    fh = out.open("a", encoding="utf-8")
    seeded = 0
    for e in index:
        n = e.get("Name")
        if not n or n in done or (only and n not in only):
            continue
        if e.get("Properties"):
            d = dict(e)
            d.pop("@odata.context", None)
            fh.write(json.dumps(d, ensure_ascii=False) + "\n")
            done[n] = d
            seeded += 1
    fh.flush()
    if seeded:
        print(f"→ Dettagli entita': {seeded} gia' completi nella risposta di lista "
              f"(nessuna chiamata aggiuntiva)")

    todo = [n for n in names if n not in done]
    if not todo:
        fh.close()
        _write(out.parent / "entities_failed.json", [])
        print(f"  Totale con dettaglio: {len(done)} · nessuna mancante")
        return done

    print(f"→ Dettagli entita' mancanti: {len(todo)} da scaricare")

    def fetch(name: str):
        esc = name.replace("'", "''")
        return name, cli.get(f"/metadata/PublicEntities('{esc}')")

    def pass_(names: list[str], etichetta: str) -> list[str]:
        """Un giro di scarico. Ritorna i nomi ancora mancanti."""
        falliti: list[str] = []
        t0 = time.time()
        n_ok = 0
        with ThreadPoolExecutor(max_workers=config.HARVEST_CONCURRENCY) as ex:
            futs = {ex.submit(fetch, n): n for n in names}
            for i, f in enumerate(as_completed(futs), 1):
                name = futs[f]
                try:
                    _, d = f.result()
                    d.pop("@odata.context", None)
                    with lock:
                        fh.write(json.dumps(d, ensure_ascii=False) + "\n")
                        done[name] = d
                        n_ok += 1
                        if n_ok % 100 == 0:
                            fh.flush()
                except NotFound:
                    falliti.append(name)
                except Exception:
                    falliti.append(name)
                if i % 25 == 0 or i == len(names):
                    el = time.time() - t0
                    rate = i / el if el else 0
                    eta = (len(names) - i) / rate if rate else 0
                    print(f"\r  {etichetta} {i}/{len(names)}  ok={n_ok} "
                          f"ko={len(falliti)}  {rate:.1f}/s  ETA {eta/60:.1f} min   ",
                          end="", flush=True)
        print()
        return falliti

    try:
        mancanti = pass_(todo, "giro 1")
        # Gran parte dei fallimenti e' throttling transitorio: un secondo giro,
        # piu' lento, recupera quasi tutto. La copertura deve essere completa.
        if mancanti:
            print(f"  {len(mancanti)} non recuperate al primo giro: ritento piu' piano")
            time.sleep(10)
            mancanti = pass_(mancanti, "giro 2")
    finally:
        fh.close()

    _write(out.parent / "entities_failed.json", sorted(mancanti))
    print(f"  Completato: {len(done)} entita' con dettaglio"
          + (f", {len(mancanti)} irrecuperabili (elenco in raw/entities_failed.json)"
             if mancanti else ", nessuna mancante"))
    return done


# --------------------------------------------------------------------- label
def detect_language(cli, sample_label: str) -> str:
    for lang in LANG_CANDIDATES:
        try:
            r = cli.get(f"/metadata/Labels(Id='{sample_label}',Language='{lang}')",
                        timeout=30, retries=1)
            val = r.get("Value") if isinstance(r, dict) else r
            if val and not str(val).lower().startswith("label '"):
                print(f"  lingua etichette: {lang}")
                return lang
        except Exception:
            continue
    print("  lingua etichette: nessuna risposta utile, uso 'it'")
    return "it"


def labels(cli, label_ids: set[str], raw: Path, lang: str = "it",
           limit: int | None = None) -> dict:
    """Risolve gli ID etichetta (@SYS123) nel testo leggibile. Cache su disco.

    Il servizio accetta solo la forma a chiave (una chiamata per etichetta):
    per questo la risoluzione e' mirata e memorizzata.
    """
    p = raw / f"labels_{lang}.json"
    cache: dict = _read(p, {}) or {}
    todo = sorted(x for x in label_ids if x and not cache.get(x))
    if limit:
        todo = todo[:limit]
    if not todo:
        print(f"→ Etichette: {len(cache)} in cache, nulla da risolvere")
        return cache
    gia_tentate = sum(1 for x in todo if x in cache)
    if gia_tentate:
        print(f"  ({gia_tentate} gia' tentate senza esito: si ritenta)")

    print(f"→ Etichette [{lang}]: {len(todo)} da risolvere ({len(cache)} in cache, "
          f"concorrenza {config.LABEL_CONCURRENCY})")
    lock = threading.Lock()
    t0 = time.time()

    def one(lid: str):
        esc = lid.replace("'", "''")
        try:
            r = cli.get(f"/metadata/Labels(Id='{esc}',Language='{lang}')",
                        timeout=30, retries=1)
            v = r.get("Value") if isinstance(r, dict) else r
            if isinstance(v, str) and v and not v.lower().startswith("label '"):
                return lid, v
        except Exception:
            pass
        return lid, None

    try:
        with ThreadPoolExecutor(max_workers=config.LABEL_CONCURRENCY) as ex:
            futs = [ex.submit(one, l) for l in todo]
            for i, f in enumerate(as_completed(futs), 1):
                lid, val = f.result()
                with lock:
                    cache[lid] = val or ""
                if i % 200 == 0 or i == len(todo):
                    el = time.time() - t0
                    rate = i / el if el else 0
                    print(f"\r  {i}/{len(todo)}  {rate:.0f}/s  "
                          f"ETA {(len(todo)-i)/rate/60 if rate else 0:.1f} min   ",
                          end="", flush=True)
                if i % 500 == 0:
                    _write(p, cache)
    finally:
        _write(p, cache)
    hit = sum(1 for v in cache.values() if v)
    print(f"\n  Etichette risolte: {hit}/{len(cache)}")
    return cache

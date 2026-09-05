"""Profilazione dei dati REALI: cosa e' popolato, quanto, da chi, e quali
relazioni reggono davvero al join."""
from __future__ import annotations

import json
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from . import config
from .client import build_query

SKIP_CATEGORIES = {"Parameters"}


def is_company_scoped(entity: dict) -> bool:
    """Vero se l'entita' e' partizionata per societa'.

    Molte entita' di F&O sono globali (anagrafiche di sistema, persone,
    indirizzi): filtrarle per `dataAreaId` restituisce HTTP 400, non zero righe.
    """
    return any((f.get("name") or "").lower() == "dataareaid"
               for f in entity.get("fields") or [])


def scope_query(entity: dict, company: str = "", cross_company: bool = True) -> str:
    """Query string per limitare la misura a una societa'."""
    if company and is_company_scoped(entity):
        c = str(company).replace("'", "''")
        return build_query({"cross-company": "true",
                            "$filter": f"dataAreaId eq '{c}'"})
    return "cross-company=true" if cross_company else ""


def scoped_path(out: Path, base: str, company: str = "") -> Path:
    """Misure di societa' diverse non devono sovrascriversi a vicenda."""
    return out / (f"{base}_{company}.json" if company else f"{base}.json")


def _write(path: Path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1))
    tmp.replace(path)


def count_entities(cli, model: dict, out: Path, only: list[str] | None = None,
                   cross_company: bool = True, company: str = "") -> dict:
    """Conta le righe di ogni entita' esposta su OData.

    E' il passaggio che separa 'cosa esiste nel prodotto' da 'cosa usa ISEO':
    su ~4700 entita' standard, quelle con dati sono tipicamente qualche
    centinaio.
    """
    p = scoped_path(out, "counts", company)
    counts: dict = {}
    if p.exists():
        try:
            counts = json.loads(p.read_text())
        except Exception:
            counts = {}

    targets = []
    for name, e in model["entities"].items():
        if only and name not in only:
            continue
        if not e.get("odata_enabled", True):
            continue
        if e["name"] in counts:
            continue
        targets.append(e)

    if not targets:
        print(f"→ Conteggi: {len(counts)} gia' presenti")
        return counts

    ambito = f"societa' {company}" if company else "tutte le societa'"
    n_glob = sum(1 for e in targets if company and not is_company_scoped(e))
    print(f"→ Conteggi righe su {len(targets)} entita' — ambito: {ambito} "
          f"(concorrenza {config.PROFILE_CONCURRENCY}, prudente verso la produzione)")
    if n_glob:
        print(f"  {n_glob} entita' sono globali (senza dataAreaId): contate per intero")
    lock = threading.Lock()
    t0 = time.time()

    def one(e):
        return e["name"], cli.count(e["entity_set"],
                                    scope_query(e, company, cross_company))

    with ThreadPoolExecutor(max_workers=config.PROFILE_CONCURRENCY) as ex:
        futs = [ex.submit(one, e) for e in targets]
        for i, f in enumerate(as_completed(futs), 1):
            try:
                n, c = f.result()
            except Exception:
                continue
            with lock:
                counts[n] = c
            if i % 25 == 0 or i == len(targets):
                el = time.time() - t0
                rate = i / el if el else 0
                pop = sum(1 for v in counts.values() if v)
                print(f"\r  {i}/{len(targets)}  popolate={pop}  {rate:.1f}/s  "
                      f"ETA {(len(targets)-i)/rate/60 if rate else 0:.0f} min   ",
                      end="", flush=True)
            if i % 100 == 0:
                with lock:
                    _write(p, counts)
    _write(p, counts)
    pop = sum(1 for v in counts.values() if v)
    unk = sum(1 for v in counts.values() if v is None)
    print(f"\n  Entita' con dati: {pop} · vuote: {len(counts)-pop-unk} · non misurabili: {unk}")
    return counts


def profile_fields(cli, model: dict, counts: dict, out: Path, sample: int = 200,
                   max_entities: int = 0, cross_company: bool = True,
                   company: str = "") -> dict:
    """Per le entita' popolate: percentuale di riempimento per campo, valori
    ricorrenti, intervallo date, societa' presenti."""
    p = scoped_path(out, "field_profile", company)
    prof: dict = {}
    if p.exists():
        try:
            prof = json.loads(p.read_text())
        except Exception:
            prof = {}

    ranked = sorted(((n, c) for n, c in counts.items() if c), key=lambda x: -x[1])
    if max_entities:
        ranked = ranked[:max_entities]
    todo = [n for n, _ in ranked if n not in prof]
    if not todo:
        print(f"→ Profilo campi: {len(prof)} gia' presenti")
        return prof

    print(f"→ Profilo campi su {len(todo)} entita' (campione {sample} righe"
          + (f", societa' {company})" if company else ")"))
    lock = threading.Lock()

    def one(name):
        e = model["entities"][name]
        q = scope_query(e, company, cross_company)
        rows = cli.get(f"/data/{e['entity_set']}?$top={sample}" + (f"&{q}" if q else ""),
                       timeout=90, retries=1)
        rows = rows.get("value", []) if isinstance(rows, dict) else []
        if not rows:
            return name, None
        n = len(rows)
        fields = {}
        for f in e["fields"]:
            fn = f["name"]
            vals = [r.get(fn) for r in rows]
            nonnull = [v for v in vals
                       if v not in (None, "", 0)
                       and str(v)[:10] not in ("1900-01-01",)]
            filled = round(100.0 * len(nonnull) / n, 1)
            info = {"riempimento_pct": filled, "distinti_campione": len(set(map(str, nonnull)))}
            if nonnull and info["distinti_campione"] <= 12:
                info["valori"] = [v for v, _ in Counter(map(str, nonnull)).most_common(12)]
            if f["type"] in ("DateTime", "Date") and nonnull:
                s = sorted(str(v) for v in nonnull)
                info["min"], info["max"] = s[0][:10], s[-1][:10]
            fields[fn] = info
        aree = sorted({str(r.get("dataAreaId")) for r in rows if r.get("dataAreaId")})
        return name, {"campione": n, "campi": fields, "societa_nel_campione": aree}

    with ThreadPoolExecutor(max_workers=config.PROFILE_CONCURRENCY) as ex:
        futs = {ex.submit(one, n): n for n in todo}
        for i, f in enumerate(as_completed(futs), 1):
            try:
                name, res = f.result()
            except Exception:
                continue
            if res:
                with lock:
                    prof[name] = res
            if i % 20 == 0 or i == len(todo):
                print(f"\r  {i}/{len(todo)}   ", end="", flush=True)
            if i % 50 == 0:
                with lock:
                    _write(p, prof)
    _write(p, prof)
    print(f"\n  Profilate {len(prof)} entita'")
    return prof


def verify_relations(cli, model: dict, counts: dict, out: Path,
                     max_checks: int = 0, probe: int = 5, company: str = "") -> dict:
    """Verifica sul dato reale che una relazione produca davvero join validi.

    Una relazione dichiarata nei metadati puo' essere inutilizzata; una
    inferita dai nomi puo' essere sbagliata. Qui si misura.
    """
    p = scoped_path(out, "relation_checks", company)
    res: dict = {}
    if p.exists():
        try:
            res = json.loads(p.read_text())
        except Exception:
            res = {}

    cand = []
    for name, e in model["entities"].items():
        if not counts.get(name):
            continue
        for r in e["relations"] + e["inferred"]:
            tgt = model["entities"].get(r["target"])
            if not tgt or not counts.get(r["target"]) or len(r["pairs"]) != 1:
                continue
            src_f, dst_f = r["pairs"][0]
            if not src_f or not dst_f:
                continue
            key = f"{name}.{src_f}->{r['target']}.{dst_f}"
            # gli esiti senza tasso sono tentativi falliti: si riprovano
            if res.get(key, {}).get("tasso") is not None:
                continue
            cand.append((key, e, r, tgt, src_f, dst_f))
    cand.sort(key=lambda c: -(counts.get(c[1]["name"]) or 0))
    if max_checks:
        cand = cand[:max_checks]
    if not cand:
        print(f"→ Verifica relazioni: {len(res)} gia' presenti")
        return res

    print(f"→ Verifica relazioni sul dato reale: {len(cand)} da controllare")
    lock = threading.Lock()

    NUMERICI = {"Int32", "Int64", "Decimal", "Real", "Double", "Int"}

    def tipo_campo(entity: dict, campo: str) -> str:
        for f in entity.get("fields") or []:
            if f["name"] == campo:
                return f.get("type") or "String"
        return "String"

    def esc(v, tipo: str = "String"):
        """Letterale OData del tipo giusto: un intero fra apici fa fallire la
        richiesta con un errore di tipo, non con zero risultati."""
        if tipo in NUMERICI:
            return str(v)
        if tipo in ("Guid",):
            return str(v)
        return "'" + str(v).replace("'", "''") + "'"

    def one(item):
        key, e, r, tgt, src_f, dst_f = item
        try:
            t_src = tipo_campo(e, src_f)
            # "ne ''" vale solo per le stringhe: su un numerico e' errore di tipo
            base_f = f"{src_f} ne ''" if t_src not in NUMERICI else f"{src_f} ne null"
            if company and is_company_scoped(e):
                base_f += f" and dataAreaId eq '{str(company).replace(chr(39), chr(39)*2)}'"
            q = build_query({"$top": probe, "$select": src_f, "$filter": base_f,
                             "cross-company": "true"})
            rows = cli.get(f"/data/{e['entity_set']}?{q}", timeout=60, retries=1)
            vals = [x.get(src_f) for x in (rows.get("value") or []) if x.get(src_f)]
            vals = list(dict.fromkeys(vals))[:probe]
            if not vals:
                # non e' un difetto della relazione: la colonna di aggancio
                # esiste ma non e' mai valorizzata
                return key, {"esito": "campo di partenza mai valorizzato",
                             "tipo": r["kind"]}
            t_dst = tipo_campo(tgt, dst_f)
            flt = "(" + " or ".join(f"{dst_f} eq {esc(v, t_dst)}" for v in vals) + ")"
            if company and is_company_scoped(tgt):
                flt += f" and dataAreaId eq {esc(company)}"
            q = build_query({"$top": probe * 3, "$select": dst_f, "$filter": flt,
                             "cross-company": "true"})
            hit = cli.get(f"/data/{tgt['entity_set']}?{q}", timeout=60, retries=1)
            # Il confronto va fatto senza distinguere maiuscole: D365 salva il
            # codice societa' minuscolo nelle tabelle transazionali e maiuscolo
            # in LegalEntity, e il filtro OData e' gia' case-insensitive lato
            # server. Confrontare alla lettera dava 0% a relazioni perfettamente
            # valide.
            def _norm(v):
                return str(v).strip().casefold()

            found = {_norm(x.get(dst_f)) for x in (hit.get("value") or [])}
            ok = sum(1 for v in vals if _norm(v) in found)
            return key, {"provati": len(vals), "risolti": ok,
                         "tasso": round(100.0 * ok / len(vals)),
                         "tipo": r["kind"]}
        except Exception as ex_:
            return key, {"esito": f"non verificabile ({type(ex_).__name__})", "tipo": r["kind"]}

    with ThreadPoolExecutor(max_workers=config.PROFILE_CONCURRENCY) as ex:
        futs = [ex.submit(one, c) for c in cand]
        for i, f in enumerate(as_completed(futs), 1):
            try:
                k, v = f.result()
            except Exception:
                continue
            with lock:
                res[k] = v
            if i % 20 == 0 or i == len(cand):
                print(f"\r  {i}/{len(cand)}   ", end="", flush=True)
            if i % 50 == 0:
                with lock:
                    _write(p, res)
    _write(p, res)
    good = sum(1 for v in res.values() if v.get("tasso", 0) >= 80)
    print(f"\n  Relazioni confermate (>=80% join validi): {good}/{len(res)}")
    return res

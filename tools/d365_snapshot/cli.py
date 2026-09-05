from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import build, config, enrich, harvest, profile as prof_mod
from .profile import scoped_path
from .auth import TokenProvider, device_code_login
from .client import D365Client


def _paths(args):
    out = Path(args.out or config.OUT_DIR)
    return out, out / "raw"


def _client(args, interactive=True):
    borrow = config.ISEOPILOT_TOKEN if getattr(args, "borrow_token", False) else None
    tp = TokenProvider(borrow=borrow, interactive=interactive)
    return D365Client(tp, resource=args.resource)


def _load_model(out: Path, raw: Path, cli=None, resolve_labels=False, args=None,
                counts: dict | None = None):
    det = {}
    f = raw / "public_entities.jsonl"
    if f.exists():
        for line in f.read_text().splitlines():
            try:
                d = json.loads(line)
                det[d["Name"]] = d
            except Exception:
                pass
    de = json.loads((raw / "data_entities.json").read_text()) if (raw / "data_entities.json").exists() else []
    en = json.loads((raw / "enumerations.json").read_text()) if (raw / "enumerations.json").exists() else []
    co = json.loads((raw / "companies.json").read_text()) if (raw / "companies.json").exists() else []
    lang = (args.lang if args else None) or "it"
    lab_f = raw / f"labels_{lang}.json"
    labels = json.loads(lab_f.read_text()) if lab_f.exists() else {}
    edmx_f = raw / "metadata.edmx.xml"
    sets = enrich.parse_edmx_sets(edmx_f.read_text()) if edmx_f.exists() else set()
    return enrich.build_model(det, de, en, labels, co, sets or None, counts)


# ------------------------------------------------------------------ comandi
def cmd_login(args):
    device_code_login()
    tp = TokenProvider()
    print("Identita':", json.dumps(tp.whoami(), ensure_ascii=False))


def cmd_whoami(args):
    c = _client(args)
    print("Identita':", json.dumps(c.tp.whoami(), ensure_ascii=False))
    v = c.get("/data/Companies?$top=5&$select=DataArea,Name")
    print("Accesso OData: OK ·", len(v.get("value", [])), "societa' lette")


def cmd_harvest(args):
    out, raw = _paths(args)
    raw.mkdir(parents=True, exist_ok=True)
    c = _client(args)
    print(f"Ambiente: {c.base}")
    print("Identita':", json.dumps(c.tp.whoami(), ensure_ascii=False), "\n")

    co = harvest.companies(c, raw)
    de = harvest.data_entities(c, raw)
    idx = harvest.public_entities_index(c, raw)
    en = harvest.enumerations(c, raw)
    if not args.no_edmx:
        harvest.edmx(c, raw)

    only = None
    if args.entities:
        only = set(args.entities)
    elif args.limit:
        only = {e["Name"] for e in idx[: args.limit]}
    det = harvest.public_entity_details(c, idx, raw, only)

    if not args.no_labels:
        keep = None
        if getattr(args, "labels_for_populated", False):
            cf = scoped_path(out, "counts", args.company)
            if not cf.exists():
                sys.exit("--labels-for-populated richiede un 'profile --counts-only' gia' eseguito.")
            counts = json.loads(cf.read_text())
            keep = {n for n, c in counts.items() if c}
            print(f"→ Etichette limitate alle {len(keep)} entita' popolate")
        ids = set()
        for name_, d in det.items():
            if keep is not None and name_ not in keep:
                continue
            if d.get("LabelId"):
                ids.add(d["LabelId"])
            for pr in d.get("Properties") or []:
                if pr.get("LabelId"):
                    ids.add(pr["LabelId"])
        for e in en:
            if e.get("LabelId"):
                ids.add(e["LabelId"])
            for m in e.get("Members") or []:
                if m.get("LabelId"):
                    ids.add(m["LabelId"])
        print(f"\n→ Etichette distinte da risolvere: {len(ids)}")
        sample = next(iter(ids), "@SYS1")
        lang = args.lang or harvest.detect_language(c, sample)
        harvest.labels(c, ids, raw, lang, limit=args.max_labels)

    print(f"\nRaccolta completata. Cache in {raw}")
    print(f"Chiamate HTTP: {c.calls} · errori: {c.errors}")


def cmd_profile(args):
    out, raw = _paths(args)
    c = _client(args)
    model = _load_model(out, raw, args=args)
    if not model["entities"]:
        sys.exit("Nessun metadato in cache: esegui prima 'harvest'.")
    only = args.entities or None
    if args.domains:
        doms = set(args.domains)
        only = [n for n, e in model["entities"].items() if e["domain"] in doms]
        print(f"Filtro domini {sorted(doms)}: {len(only)} entita'")
    counts = prof_mod.count_entities(c, model, out, only, company=args.company)
    if not args.counts_only:
        prof_mod.profile_fields(c, model, counts, out, sample=args.sample,
                                max_entities=args.max_entities, company=args.company)
        if args.verify_relations:
            prof_mod.verify_relations(c, model, counts, out,
                                      max_checks=args.max_checks, company=args.company)
    print(f"\nChiamate HTTP: {c.calls} · errori: {c.errors}")


def cmd_build(args):
    out, raw = _paths(args)

    def rd(base):
        f = scoped_path(out, base, args.company)
        return json.loads(f.read_text()) if f.exists() else {}

    counts, prof, checks = rd("counts"), rd("field_profile"), rd("relation_checks")
    model = _load_model(out, raw, args=args, counts=counts)
    if not model["entities"]:
        sys.exit("Nessun metadato in cache: esegui prima 'harvest'.")
    ff = raw / "entities_failed.json"
    falliti = json.loads(ff.read_text()) if ff.exists() else []
    if args.company and not counts:
        print(f"Attenzione: nessuna misura per la societa' {args.company}. "
              f"Esegui:  profile --company {args.company}")
    st = enrich.stats(model)
    meta = {"resource": args.resource or config.RESOURCE,
            "societa_misurata": args.company or "tutte",
            "generato": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
            "statistiche": st}

    build.write_sqlite(model, counts, prof, checks, out / "snapshot.sqlite", meta)
    n_md = build.write_entity_cards(model, counts, prof, checks, out / "schema",
                                    only_populated=args.only_populated,
                                    company=args.company)
    n_erd = build.write_mermaid(model, counts, out / "erd")
    build.write_iseopilot_catalog(model, counts, prof, out / "catalog_iseopilot.json",
                                  checks=checks, schema_dir=str(out / "schema"), n_md=n_md)
    build.write_report(model, counts, prof, checks, st, out / "REPORT.md", meta,
                       company=args.company, falliti=falliti)
    (out / "snapshot.json").write_text(
        json.dumps({"meta": meta, "modello": model, "conteggi": counts},
                   ensure_ascii=False), encoding="utf-8")

    print(json.dumps(st, ensure_ascii=False, indent=1))
    print(f"\nGenerati in {out}:")
    print(f"  REPORT.md                 la fotografia leggibile")
    print(f"  snapshot.sqlite           database interrogabile")
    print(f"  snapshot.json             modello completo")
    print(f"  schema/                   {n_md} schede entita' (.md)")
    print(f"  erd/                      {n_erd} diagrammi Mermaid")
    print(f"  catalog_iseopilot.json    catalogo per il connettore")


def cmd_all(args):
    cmd_harvest(args)
    if not args.no_profile:
        cmd_profile(args)
    cmd_build(args)


def cmd_query(args):
    out, _ = _paths(args)
    db = sqlite3.connect(out / "snapshot.sqlite")
    db.row_factory = sqlite3.Row
    try:
        rows = db.execute(args.sql).fetchall()
    except sqlite3.Error as e:
        sys.exit(f"SQL: {e}")
    if not rows:
        print("(nessun risultato)")
        return
    cols = rows[0].keys()
    w = {c: max(len(c), *(len(str(r[c])[:40]) for r in rows[:200])) for c in cols}
    print(" | ".join(c.ljust(w[c]) for c in cols))
    print("-+-".join("-" * w[c] for c in cols))
    for r in rows[: args.limit_rows]:
        print(" | ".join(str(r[c])[:40].ljust(w[c]) for c in cols))
    print(f"\n{len(rows)} righe")


def main(argv=None):
    p = argparse.ArgumentParser(
        prog="d365_snapshot",
        description="Fotografia completa dello schema Dynamics 365 F&O per ISEOPilot.")
    p.add_argument("--out", help=f"cartella di output (default {config.OUT_DIR})")
    p.add_argument("--resource", help="URL ambiente D365")
    p.add_argument("--borrow-token", action="store_true",
                   help="parti dal token di IseoPilot (in sola lettura)")
    p.add_argument("--lang", help="lingua etichette (it, it-IT, en-US)")
    p.add_argument("--company", default=config.COMPANY,
                   help="limita le misure a una societa' (es. IT1). "
                        "Le entita' globali restano contate per intero.")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("login", help="accesso device code").set_defaults(f=cmd_login)
    sub.add_parser("whoami", help="verifica identita' e accesso").set_defaults(f=cmd_whoami)

    h = sub.add_parser("harvest", help="scarica i metadati")
    h.add_argument("--limit", type=int, help="solo le prime N entita' (prova rapida)")
    h.add_argument("--entities", nargs="+", help="solo queste entita'")
    h.add_argument("--no-labels", action="store_true", help="salta le etichette")
    h.add_argument("--max-labels", type=int, help="tetto alle etichette da risolvere")
    h.add_argument("--no-edmx", action="store_true")
    h.add_argument("--labels-for-populated", action="store_true",
                   help="risolvi le etichette solo per le entita' con dati "
                        "(richiede un profile --counts-only gia' fatto)")
    h.set_defaults(f=cmd_harvest)

    pr = sub.add_parser("profile", help="misura i dati reali")
    pr.add_argument("--entities", nargs="+")
    pr.add_argument("--domains", nargs="+", help="es. Acquisti Vendite Magazzino")
    pr.add_argument("--sample", type=int, default=200)
    pr.add_argument("--max-entities", type=int, default=0,
                    help="tetto alle entita' da profilare (0 = tutte, default)")
    pr.add_argument("--counts-only", action="store_true")
    pr.add_argument("--verify-relations", action="store_true")
    pr.add_argument("--max-checks", type=int, default=0,
                    help="tetto alle relazioni da verificare (0 = tutte, default)")
    pr.set_defaults(f=cmd_profile)

    b = sub.add_parser("build", help="genera gli artefatti")
    b.add_argument("--only-populated", action="store_true",
                   help="schede solo per le entita' con dati")
    b.set_defaults(f=cmd_build)

    a = sub.add_parser("all", help="harvest + profile + build")
    for x in (a,):
        x.add_argument("--limit", type=int)
        x.add_argument("--entities", nargs="+")
        x.add_argument("--domains", nargs="+")
        x.add_argument("--no-labels", action="store_true")
        x.add_argument("--max-labels", type=int)
        x.add_argument("--no-edmx", action="store_true")
        x.add_argument("--labels-for-populated", action="store_true")
        x.add_argument("--no-profile", action="store_true")
        x.add_argument("--sample", type=int, default=200)
        x.add_argument("--max-entities", type=int, default=0)
        x.add_argument("--counts-only", action="store_true")
        x.add_argument("--verify-relations", action="store_true")
        x.add_argument("--max-checks", type=int, default=0)
        x.add_argument("--only-populated", action="store_true")
    a.set_defaults(f=cmd_all)

    q = sub.add_parser("query", help="interroga snapshot.sqlite")
    q.add_argument("sql")
    q.add_argument("--limit-rows", type=int, default=50)
    q.set_defaults(f=cmd_query)

    args = p.parse_args(argv)
    args.f(args)

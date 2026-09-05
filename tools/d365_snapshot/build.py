"""Generazione degli artefatti: SQLite interrogabile, schede Markdown,
ERD Mermaid, report di sintesi e catalogo compatibile con IseoPilot."""
from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

def _n(v) -> str:
    """Migliaia col punto, senza toccare il resto della riga."""
    if isinstance(v, bool) or not isinstance(v, int):
        return str(v)
    return format(v, ",").replace(",", ".")


DDL = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS entita(
  nome TEXT PRIMARY KEY, entity_set TEXT, etichetta TEXT, dominio TEXT,
  categoria TEXT, sola_lettura INT, odata INT, dmf INT,
  n_campi INT, n_relazioni INT, n_inferite INT,
  righe INT, popolata INT, societa TEXT, chiavi TEXT,
  famiglia TEXT, preferita INT, equivalenti TEXT);
CREATE TABLE IF NOT EXISTS campo(
  entita TEXT, nome TEXT, etichetta TEXT, tipo TEXT, enum TEXT,
  chiave INT, obbligatorio INT, sola_lettura INT,
  riempimento_pct REAL, distinti INT, valori TEXT, dt_min TEXT, dt_max TEXT,
  PRIMARY KEY(entita, nome));
CREATE TABLE IF NOT EXISTS relazione(
  origine TEXT, nome TEXT, destinazione TEXT, cardinalita TEXT,
  campi TEXT, tipo TEXT, confidenza INT,
  join_provati INT, join_risolti INT, join_tasso INT);
CREATE TABLE IF NOT EXISTS enum_valore(
  enum TEXT, etichetta_enum TEXT, membro TEXT, valore INT, etichetta TEXT);
CREATE TABLE IF NOT EXISTS societa(codice TEXT PRIMARY KEY, nome TEXT);
CREATE TABLE IF NOT EXISTS meta(chiave TEXT PRIMARY KEY, valore TEXT);
CREATE INDEX IF NOT EXISTS ix_campo_nome ON campo(nome);
CREATE INDEX IF NOT EXISTS ix_rel_org ON relazione(origine);
CREATE INDEX IF NOT EXISTS ix_rel_dst ON relazione(destinazione);
CREATE INDEX IF NOT EXISTS ix_ent_dom ON entita(dominio);
CREATE INDEX IF NOT EXISTS ix_ent_pop ON entita(popolata, righe);
CREATE INDEX IF NOT EXISTS ix_ent_fam ON entita(famiglia, preferita);
"""


def write_sqlite(model, counts, prof, checks, path: Path, meta: dict) -> None:
    if path.exists():
        path.unlink()
    for suf in ("-wal", "-shm"):
        q = Path(str(path) + suf)
        if q.exists():
            q.unlink()
    db = sqlite3.connect(path)
    db.executescript(DDL)

    for c in model["companies"]:
        db.execute("INSERT OR REPLACE INTO societa VALUES(?,?)", (c["code"], c["name"]))
    for k, v in meta.items():
        db.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (k, json.dumps(v, ensure_ascii=False)))

    for en, e in model["enums"].items():
        for m in e["members"]:
            db.execute("INSERT INTO enum_valore VALUES(?,?,?,?,?)",
                       (en, e["label"], m["name"], m["value"], m["label"]))

    for name, e in model["entities"].items():
        cnt = counts.get(name)
        pf = prof.get(name) or {}
        db.execute("INSERT OR REPLACE INTO entita VALUES("
                   "?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            name, e["entity_set"], e["label"], e["domain"], e.get("category"),
            int(e["read_only"]), int(e.get("odata_enabled", True)), int(e.get("dmf_enabled", False)),
            len(e["fields"]), len(e["relations"]), len(e["inferred"]),
            cnt, 1 if cnt else (0 if cnt == 0 else None),
            ",".join(pf.get("societa_nel_campione") or []), ",".join(e["keys"]),
            e.get("famiglia"), int(bool(e.get("preferita"))),
            ",".join(e.get("equivalenti") or []),
        ))
        pfields = pf.get("campi") or {}
        for f in e["fields"]:
            i = pfields.get(f["name"]) or {}
            db.execute("INSERT OR REPLACE INTO campo VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                name, f["name"], f["label"], f["type"], f.get("enum"),
                int(f["is_key"]), int(f["is_mandatory"]), int(f["read_only"]),
                i.get("riempimento_pct"), i.get("distinti_campione"),
                json.dumps(i.get("valori"), ensure_ascii=False) if i.get("valori") else None,
                i.get("min"), i.get("max"),
            ))
        for r in e["relations"] + e["inferred"]:
            src = r["pairs"][0][0] if r["pairs"] else None
            dst = r["pairs"][0][1] if r["pairs"] else None
            chk = checks.get(f"{name}.{src}->{r['target']}.{dst}") or {}
            db.execute("INSERT INTO relazione VALUES(?,?,?,?,?,?,?,?,?,?)", (
                name, r["name"], r["target"], r.get("cardinality"),
                json.dumps(r["pairs"]), r["kind"], r.get("confidenza"),
                chk.get("provati"), chk.get("risolti"), chk.get("tasso"),
            ))
    db.commit()
    db.close()


def _safe_filename(name: str) -> str:
    """Identica a dynamics_search._safe_entity_filename: i due lati devono
    concordare sul nome del file, altrimenti la scheda non viene mai trovata."""
    keep = "".join(c if (c.isalnum() or c in "-_") else "_" for c in str(name))
    return (keep[:120] or "entita") + ".md"


def _scoped(e) -> bool:
    return any((f.get("name") or "").lower() == "dataareaid" for f in e.get("fields") or [])


def write_entity_cards(model, counts, prof, checks, out: Path, only_populated=False,
                       company: str = "") -> int:
    out.mkdir(parents=True, exist_ok=True)
    for f in out.glob("*.md"):
        f.unlink()
    n = 0
    for name, e in sorted(model["entities"].items()):
        cnt = counts.get(name)
        if only_populated and not cnt:
            continue
        pf = prof.get(name) or {}
        pfields = pf.get("campi") or {}
        L = [f"# {e['label']}", "",
             f"**Entita' OData**: `{e['entity_set']}` · **Nome tecnico**: `{name}`",
             f"**Dominio**: {e['domain']} · **Categoria**: {e.get('category') or 'n/d'}"]
        if company and _scoped(e):
            ambito = f" (societa' {company})"
        elif company:
            ambito = " (entita' globale, tutte le societa')"
        else:
            ambito = " (tutte le societa')"
        if cnt is not None:
            L.append(f"**Righe in produzione**{ambito}: {_n(cnt)}")
        elif name in counts:
            L.append(f"**Righe in produzione**{ambito}: non misurabile (timeout)")
        if pf.get("societa_nel_campione"):
            L.append(f"**Societa' nel campione**: {', '.join(pf['societa_nel_campione'])}")
        if e["read_only"]:
            L.append("**Sola lettura**")
        if e.get("equivalenti"):
            if e.get("preferita"):
                L += ["", f"> **Entità di riferimento** per lo stesso dato. Varianti "
                          f"equivalenti da NON usare: "
                          + ", ".join(f"`{x}`" for x in e["equivalenti"]) + "."]
            else:
                pref = next((x for x in [e["name"]] + e["equivalenti"]), None)
                fam = e.get("famiglia") or ""
                pref = fam[4:] if fam.startswith("fam_") else pref
                L += ["", f"> **Variante equivalente**: espone lo stesso dato di "
                          f"`{pref}`, che è l'entità di riferimento. Preferire quella."]
        L += ["", f"**Chiave**: {', '.join(f'`{k}`' for k in e['keys']) or 'nessuna'}", ""]

        # L'ordine delle prime tre colonne e il marcatore chiave non sono
        # estetica: _entity_schema_extra() estrae le chiavi con una regex che
        # pretende "| Campo | Tipo | 🔑 |".
        L += ["**Campi**", "",
              "| Campo | Tipo | Chiave | Etichetta | Obbl. | Riemp. | Valori tipici |",
              "|---|---|---|---|---|---|---|"]
        flds = e["fields"]
        if pfields:
            flds = sorted(flds, key=lambda f: (-(pfields.get(f["name"], {}).get("riempimento_pct") or 0),
                                               not f["is_key"]))
        for f in flds:
            i = pfields.get(f["name"]) or {}
            t = f["type"] + (f" ({f['enum']})" if f.get("enum") else "")
            fill = f"{i['riempimento_pct']:.0f}%" if i.get("riempimento_pct") is not None else ""
            vals = ", ".join(str(v)[:22] for v in (i.get("valori") or [])[:5])
            if i.get("min"):
                vals = f"{i['min']} → {i['max']}"
            L.append(f"| {f['name']} | {t} | {'\U0001F511' if f['is_key'] else ''} | "
                     f"{f['label']} | {'SI' if f['is_mandatory'] else ''} | "
                     f"{fill} | {vals} |")

        rels = e["relations"] + e["inferred"]
        if rels:
            L += ["", "## Relazioni", "",
                  "| Verso | Cardinalita | Campi di join | Origine | Join verificato |",
                  "|---|---|---|---|---|"]
            for r in rels:
                src = r["pairs"][0][0] if r["pairs"] else None
                dst = r["pairs"][0][1] if r["pairs"] else None
                chk = checks.get(f"{name}.{src}->{r['target']}.{dst}") or {}
                v = (f"{chk['tasso']}% ({chk['risolti']}/{chk['provati']})"
                     if chk.get("tasso") is not None else chk.get("esito", ""))
                j = ", ".join(f"{a} = {b}" for a, b in r["pairs"] if a)
                k = r["kind"] + (f" {r['confidenza']}%" if r.get("confidenza") else "")
                L.append(f"| `{r['target']}` | {r.get('cardinality') or ''} | {j} | {k} | {v} |")

        enums_used = {f["enum"] for f in e["fields"] if f.get("enum")}
        shown = [x for x in sorted(enums_used) if x in model["enums"]][:12]
        if shown:
            L += ["", "**Valori enum**", ""]
            for en in shown:
                campi = [f["name"] for f in e["fields"] if f.get("enum") == en][:4]
                mm = model["enums"][en]["members"][:20]
                vals = ", ".join(f"{m['name']}={m['label']}" for m in mm)
                L.append(f"- `{en}` (campo/i: {', '.join(campi)}): {vals}")

        L += ["", "## Esempio di query OData", "", "```",
              f"GET /data/{e['entity_set']}?$top=10"
              + (f"&$select={','.join(f['name'] for f in flds[:6])}" if flds else "")
              + "&cross-company=true", "```", ""]
        # Il loader cerca il file col nome dell'ENTITY SET, non del tipo:
        # _entity_schema_extra() apre <entity_set>.md. Un file chiamato col nome
        # tecnico non verrebbe mai letto.
        (out / _safe_filename(e["entity_set"])).write_text("\n".join(L), encoding="utf-8")
        n += 1
    return n


def _campi_salienti(e, prof, quanti=8):
    """Campi che descrivono davvero l'entita': prima le chiavi, poi gli
    obbligatori, poi i piu' valorizzati sul dato reale. L'ordine di sorgente
    metteva in vetrina campi marginali."""
    pf = (prof.get(e["name"]) or {}).get("campi") or {}

    def peso(f):
        return (0 if f["is_key"] else (1 if f["is_mandatory"] else 2),
                -(pf.get(f["name"], {}).get("riempimento_pct") or 0),
                len(f["name"]))

    return sorted(e["fields"], key=peso)[:quanti]


def write_mermaid(model, counts, out: Path, min_rows=1, max_per_dom=22,
                  prof: dict | None = None) -> int:
    out.mkdir(parents=True, exist_ok=True)
    for f in out.glob("*.mmd"):
        f.unlink()
    prof = prof or {}
    # Le varianti equivalenti raddoppiano i nodi senza aggiungere informazione:
    # nel diagramma resta solo l'entita' di riferimento della famiglia.
    def tenere(e):
        return e.get("preferita", True) or not e.get("famiglia")

    by_dom = defaultdict(list)
    for name, e in model["entities"].items():
        c = counts.get(name) or 0
        if c >= min_rows and tenere(e):
            by_dom[e["domain"]].append((c, name, e))
    n = 0
    for dom, items in by_dom.items():
        items.sort(key=lambda x: -x[0])
        sel = items[:max_per_dom]
        names = {x[1] for x in sel}
        L = [f"%% ERD dominio: {dom} — entita' con dati in produzione",
             "erDiagram"]
        for c, name, e in sel:
            L.append(f"    {name} {{")
            for f in _campi_salienti(e, prof):
                t = (f["type"] or "string").replace(" ", "")
                L.append(f"        {t} {f['name']}{' PK' if f['is_key'] else ''}")
            L.append("    }")
        seen = set()
        for c, name, e in sel:
            for r in e["relations"] + e["inferred"]:
                if r["target"] not in names:
                    continue
                key = tuple(sorted((name, r["target"])))
                if key in seen:
                    continue
                seen.add(key)
                card = "||--o{" if r.get("cardinality") == "Multiple" else "}o--||"
                lbl = (r["pairs"][0][0] if r["pairs"] else r["name"])[:28]
                L.append(f"    {name} {card} {r['target']} : \"{lbl}\"")
        (out / f"{dom.replace(' ', '_')}.mmd").write_text("\n".join(L), encoding="utf-8")
        n += 1

    # ERD trasversale: i legami veri attraversano i domini (un ordine di acquisto
    # punta a un fornitore e a un articolo), quindi serve una vista unica.
    core = sorted(((counts.get(k) or 0, k, v) for k, v in model["entities"].items()
                   if (counts.get(k) or 0) >= min_rows and tenere(v)),
                  key=lambda x: -x[0])[:40]
    if core:
        names = {x[1] for x in core}
        L = ["%% ERD trasversale — le 45 entita' con piu' righe in produzione",
             "erDiagram"]
        for c, name, e in core:
            L.append(f"    {name} {{")
            for f in ([x for x in e["fields"] if x["is_key"]][:5]
                      or _campi_salienti(e, prof, 5)):
                L.append(f"        {(f['type'] or 'string').replace(' ', '')} {f['name']}"
                         f"{' PK' if f['is_key'] else ''}")
            L.append("    }")
        seen = set()
        for c, name, e in core:
            for r in e["relations"] + e["inferred"]:
                if r["target"] not in names:
                    continue
                key = tuple(sorted((name, r["target"])))
                if key in seen:
                    continue
                seen.add(key)
                card = "||--o{" if r.get("cardinality") == "Multiple" else "}o--||"
                L.append(f"    {name} {card} {r['target']} : "
                         f"\"{(r['pairs'][0][0] if r['pairs'] else r['name'])[:28]}\"")
        (out / "_trasversale.mmd").write_text("\n".join(L), encoding="utf-8")
        n += 1
    return n


def write_iseopilot_catalog(model, counts, prof, path: Path,
                            checks: dict | None = None,
                            schema_dir: str = "", n_md: int = 0) -> None:
    """Catalogo nel formato atteso da app/engines/dynamics_search.py, arricchito
    con etichette, chiavi, enum e conteggi reali. Retrocompatibile."""
    ent = {}
    for name, e in model["entities"].items():
        strings = [f["name"] for f in e["fields"] if f["type"] == "String"]
        dates = [f["name"] for f in e["fields"] if f["type"] in ("DateTime", "Date")]
        nums = [f["name"] for f in e["fields"]
                if f["type"] in ("Int32", "Int64", "Decimal", "Real", "Double")]
        checks = checks or {}

        def _set(n):
            return model["entities"].get(n, {}).get("entity_set", n)

        def _tasso(r):
            src = r["pairs"][0][0] if r["pairs"] else None
            dst = r["pairs"][0][1] if r["pairs"] else None
            return (checks.get(f"{name}.{src}->{r['target']}.{dst}") or {}).get("tasso")

        # `rel` resta la verita' dichiarata nei metadati: e' cio' su cui
        # _find_relation decide se un join e' lecito, e non va inquinato con
        # ipotesi. Le relazioni dedotte dai nomi e confermate sul dato reale
        # stanno a parte, disponibili ma non attive.
        rel = [{"nav": r["name"], "target": _set(r["target"]),
                "pairs": r["pairs"], "tasso": _tasso(r)}
               for r in e["relations"] if r["pairs"]]
        rel_inf = [{"nav": r["name"], "target": _set(r["target"]),
                    "pairs": r["pairs"], "confidenza": r.get("confidenza"),
                    "tasso": _tasso(r)}
                   for r in e["inferred"] if r["pairs"] and (_tasso(r) or 0) >= 80]
        ent[e["entity_set"]] = {
            "string": strings, "date": dates, "rel": rel,
            "rel_inferite": rel_inf,
            # estensioni: ignorate dal codice esistente, utili al modello
            "num": nums,
            "etichetta": e["label"],
            "dominio": e["domain"],
            "chiavi": e["keys"],
            "righe": counts.get(name),
            "famiglia": e.get("famiglia"),
            "preferita": bool(e.get("preferita", True)),
            "equivalenti": [model["entities"].get(x, {}).get("entity_set", x)
                            for x in (e.get("equivalenti") or [])],
            "enum": {f["name"]: f["enum"] for f in e["fields"] if f.get("enum")},
            "etichette_campi": {f["name"]: f["label"] for f in e["fields"]},
        }
    payload = {
        "entita": ent,
        "count": len(ent),
        "relazioni": sum(len(v["rel"]) for v in ent.values()),
        "generato": datetime.now(timezone.utc).isoformat(),
        "istanza": "isd365-prod",
        "versione": "3.0",
        "schema_md": {"cartella": schema_dir, "file": n_md},
        "relazioni_inferite_verificate": sum(len(v["rel_inferite"]) for v in ent.values()),
        "famiglie": model.get("famiglie") or [],
        "enum_catalogo": {k: {"etichetta": v["label"],
                              "valori": {m["name"]: m["label"] for m in v["members"]}}
                          for k, v in model["enums"].items()},
        "societa": model["companies"],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def write_report(model, counts, prof, checks, stats, out: Path, meta: dict,
                 company: str = "", falliti: list | None = None) -> None:
    ents = model["entities"]
    pop = [(c, n) for n, c in counts.items() if c]
    pop.sort(reverse=True)
    empty = sum(1 for c in counts.values() if c == 0)
    unk = sum(1 for c in counts.values() if c is None)

    by_dom = defaultdict(lambda: [0, 0])
    for n, e in ents.items():
        by_dom[e["domain"]][0] += 1
        if counts.get(n):
            by_dom[e["domain"]][1] += 1

    L = ["# Fotografia dell'ERP — Dynamics 365 F&O", "",
         f"Ambiente: `{meta.get('resource')}`  ",
         f"Generata: {meta.get('generato')}  ",
         f"Societa' misurata: **{company or 'tutte (cross-company)'}**  ",
         f"Utente: {meta.get('utente', 'n/d')}", "",
         "## In sintesi", "",
         "| Voce | Valore |", "|---|---|",
         f"| Entita' con metadati completi | {_n(stats['entita'])} |",
         f"| Campi mappati | {_n(stats['campi'])} |",
         f"| Relazioni dichiarate nei metadati | {_n(stats['relazioni_dichiarate'])} |",
         f"| Relazioni inferite dai nomi chiave | {_n(stats['relazioni_inferite'])} |",
         f"| Enumerazioni | {_n(stats['enum'])} |",
         f"| Societa' (legal entities) | {stats['societa']} |",
         f"| Entita' senza dettaglio pubblico | {_n(stats['entita_senza_dettaglio'])} |",
         ]
    if counts:
        L += [f"| Entita' misurate | {_n(len(counts))} |",
              f"| **Entita' effettivamente popolate** | **{_n(len(pop))}** |",
              f"| Entita' vuote | {_n(empty)} |",
              f"| Non misurabili (timeout) | {_n(unk)} |"]
    if company:
        glob = sum(1 for n in counts if not _scoped(ents.get(n, {})))
        L += ["",
              f"> I conteggi sono filtrati su `dataAreaId eq '{company}'`. "
              f"{glob} entita' sono globali (prive di `dataAreaId`): per quelle "
              f"il numero e' complessivo, non attribuibile a una societa'."]
    L += [""]

    if model["companies"]:
        L += ["## Societa'", "", "| Codice | Nome |", "|---|---|"]
        L += [f"| `{c['code']}` | {c['name']} |" for c in model["companies"]]
        L += [""]

    L += ["## Copertura per dominio", "",
          "| Dominio | Entita' | Con dati |", "|---|---|---|"]
    for d, (tot, p) in sorted(by_dom.items(), key=lambda x: -x[1][1]):
        L.append(f"| {d} | {tot} | {p} |")
    L += [""]

    if pop:
        L += ["## Le 60 entita' piu' voluminose (il cuore reale del gestionale)", "",
              "| # | Entita' | Etichetta | Dominio | Righe |", "|---|---|---|---|---|"]
        for i, (c, n) in enumerate(pop[:60], 1):
            e = ents[n]
            L.append(f"| {i} | `{e['entity_set']}` | {e['label']} | {e['domain']} | "
                     f"{_n(c)} |")
        L += [""]

    if checks:
        # Cardinalita' per chiave, per non scambiare una relazione sparsa per rotta.
        card = {}
        for n, e in ents.items():
            for r in e["relations"] + e["inferred"]:
                if r["pairs"]:
                    card[f"{n}.{r['pairs'][0][0]}->{r['target']}.{r['pairs'][0][1]}"] = \
                        r.get("cardinality") or "Single"

        ok, sparse, sospette, indeterminate = [], [], [], []
        for k, v in checks.items():
            t = v.get("tasso")
            if t is None:
                indeterminate.append(k)
            elif t >= 80:
                ok.append(k)
            elif card.get(k) == "Multiple":
                # A ha molti B: e' normale che gran parte degli A non abbia figli
                sparse.append(k)
            elif t < 40:
                sospette.append(k)
        L += ["## Relazioni verificate sul dato reale", "",
              "Per ogni relazione si prende un campione di valori dalla colonna di "
              "partenza e si controlla quanti trovano corrispondenza a destinazione.",
              "",
              f"- Verificate: **{len(checks)}**",
              f"- **Confermate** (join valido >= 80%): **{len(ok)}**",
              f"- Sparse: **{len(sparse)}** — relazioni uno-a-molti in cui molti "
              f"capofila non hanno righe collegate. Sono valide: un prodotto senza "
              f"ordini pianificati non e' una relazione rotta.",
              f"- **Sospette**: **{len(sospette)}** — puntano a un singolo record "
              f"che spesso non esiste. Qui l'aggancio va guardato prima di usarlo.",
              f"- Indeterminate: **{len(indeterminate)}** — colonna di partenza mai "
              f"valorizzata, oppure servizio non raggiungibile al momento della prova.",
              ""]
        if sospette:
            L += ["Le piu' rilevanti fra le sospette:", "",
                  "| Relazione | Tasso | Origine |", "|---|---|---|"]
            for k in sospette[:25]:
                L.append(f"| `{k}` | {checks[k].get('tasso')}% | {checks[k].get('tipo')} |")
            L += [""]

    fams = model.get("famiglie") or []
    if fams:
        ridondanti = sum(len(f["membri"]) - 1 for f in fams)
        L += ["## Entità equivalenti", "",
              f"D365 pubblica lo stesso dato sotto più entità: la versionata "
              f"(V2/V3), la variante analitica (`BiEntities`, `CDSEntities`), a "
              f"volte una di staging. Hanno conteggi identici e quasi gli stessi "
              f"campi.", "",
              f"Sono state riconosciute **{len(fams)} famiglie** per un totale di "
              f"**{ridondanti} entità ridondanti**. Per ciascuna è eletta "
              f"un'entità di riferimento: se ISEOPilot ne sceglie una a caso, dà "
              f"risposte diverse alla stessa domanda.", "",
              "| Righe | Da usare | Varianti equivalenti |", "|---|---|---|"]
        for f in fams[:30]:
            alt = ", ".join(f"`{m}`" for m in f["membri"] if m != f["preferita"])
            L.append(f"| {_n(f['righe'])} | `{f['preferita']}` | {alt} |")
        L += [""]

    # --- Copertura: le lacune vanno dichiarate, non nascoste -----------------
    falliti = falliti or []
    non_odata = [n for n, e in ents.items() if not e.get("odata_enabled", True)]
    non_misurate = [n for n in ents if n not in counts]
    solo_dmf = [d for d in model["senza_dettaglio"] if not d.get("odata")]
    L += ["## Copertura", "",
          "Cosa e' effettivamente ricercabile da ISEOPilot, e cosa no.", "",
          "| Livello | Entita' |", "|---|---|",
          f"| Con schema completo (campi, chiavi, relazioni) | {_n(len(ents))} |",
          f"| Interrogabili via OData | {_n(len(ents) - len(non_odata))} |",
          f"| Misurate (conteggio righe eseguito) | {_n(len(counts))} |",
          f"| Con profilo dei campi | {_n(len(prof))} |",
          f"| Con relazioni verificate sul dato | {_n(len(checks))} |",
          ""]
    lacune = []
    if falliti:
        lacune.append(f"- **{len(falliti)}** entita' il cui dettaglio non e' stato "
                      f"recuperato nemmeno al secondo tentativo "
                      f"(`raw/entities_failed.json`): "
                      + ", ".join(f"`{x}`" for x in falliti[:10])
                      + (" …" if len(falliti) > 10 else ""))
    if non_odata:
        lacune.append(f"- **{len(non_odata)}** entita' non esposte su OData: hanno "
                      f"schema ma non sono interrogabili a runtime.")
    if solo_dmf:
        lacune.append(f"- **{len(solo_dmf)}** entita' presenti solo nel framework di "
                      f"data management, senza entita' pubblica: fuori portata per "
                      f"una ricerca OData.")
    if non_misurate:
        lacune.append(f"- **{len(non_misurate)}** entita' mai misurate: manca un "
                      f"`profile` su di esse.")
    if lacune:
        L += ["### Lacune note", ""] + lacune + [""]
    else:
        L += ["Nessuna lacuna: la copertura e' completa su tutti i livelli.", ""]

    L += ["## Limiti noti di questa fotografia", "",
          "- La sorgente e' il **livello entita** di D365, non le tabelle fisiche AOT.",
          "  Le tabelle fisiche (`PurchReqTable`, `InventTrans`, ...) e le loro relazioni",
          "  non sono esposte da alcuna API in produzione: richiedono i metadati AOT",
          "  da un ambiente Tier-2/dev, oppure Synapse Link. Vedi README, sezione",
          "  \"Livello tabella\".",
          "- Le relazioni marcate *inferita* nascono dal confronto fra nomi campo e",
          "  chiavi: sono ipotesi, promosse a certezza solo dalla verifica sul dato.",
          "- I conteggi fotografano l'istante della generazione.",
          ""]
    out.write_text("\n".join(L), encoding="utf-8")

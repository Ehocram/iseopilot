"""Costruzione del modello: campi, relazioni dichiarate e relazioni inferite."""
from __future__ import annotations

import re
from collections import defaultdict

from . import config

_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")

# Campi tecnici ricorrenti: il servizio etichette non li traduce, ma per un
# assistente sapere che dataAreaId "e' la societa'" cambia la qualita' delle risposte.
TECH_LABELS = {
    "dataareaid": "Societa' (legal entity)",
    "recid": "Identificativo interno riga",
    "partition": "Partizione",
    "recversion": "Versione riga",
    "createddatetime": "Data/ora creazione",
    "modifieddatetime": "Data/ora ultima modifica",
    "createdby": "Creato da",
    "modifiedby": "Modificato da",
    "itemnumber": "Codice articolo",
    "vendoraccountnumber": "Codice fornitore",
    "customeraccount": "Codice cliente",
    "lineNumber".lower(): "Numero riga",
}


def humanize(name: str) -> str:
    t = TECH_LABELS.get((name or "").lower())
    if t:
        return t
    return _CAMEL.sub(" ", name).replace("_", " ").strip()


def short_enum(type_name: str) -> str | None:
    if not type_name:
        return None
    if type_name.startswith("Microsoft.Dynamics.DataEntities."):
        return type_name.rsplit(".", 1)[-1]
    return None


def build_model(details: dict, de_index: list, enums: list, labels: dict,
                companies: list, edmx_sets: set[str] | None = None,
                counts: dict | None = None) -> dict:
    lbl = lambda i: (labels.get(i) or "").strip() if i else ""

    enum_map = {}
    for e in enums:
        enum_map[e["Name"]] = {
            "label": lbl(e.get("LabelId")) or humanize(e["Name"]),
            "label_id": e.get("LabelId"),
            "members": [
                {"name": m.get("Name"), "value": m.get("Value"),
                 "label": lbl(m.get("LabelId")) or humanize(m.get("Name") or "")}
                for m in (e.get("Members") or [])
            ],
        }

    # DataEntities e' l'indice piu' ampio (6001): contiene anche le entita'
    # esposte solo al framework di data management, non a OData.
    de_by_public = {}
    for d in de_index:
        if d.get("PublicEntityName"):
            de_by_public[d["PublicEntityName"]] = d

    entities: dict = {}
    for name, d in details.items():
        de = de_by_public.get(name, {})
        fields = []
        keys = []
        for p in d.get("Properties") or []:
            en = short_enum(p.get("TypeName")) if p.get("DataType") == "Enum" else None
            f = {
                "name": p.get("Name"),
                "label": lbl(p.get("LabelId")) or humanize(p.get("Name") or ""),
                "label_id": p.get("LabelId"),
                "type": p.get("DataType"),
                "odata_type": p.get("TypeName"),
                "enum": en,
                "is_key": bool(p.get("IsKey")),
                "is_mandatory": bool(p.get("IsMandatory")),
                "read_only": p.get("AllowEdit") is False and p.get("AllowEditOnCreate") is False,
                "is_dimension": bool(p.get("IsDimension")),
            }
            fields.append(f)
            if f["is_key"]:
                keys.append(f["name"])

        rels = []
        for n in d.get("NavigationProperties") or []:
            pairs = []
            fixed = []
            for c in n.get("Constraints") or []:
                t = str(c.get("@odata.type", ""))
                if "Referential" in t or (c.get("Property") and c.get("ReferencedProperty")):
                    pairs.append([c.get("Property"), c.get("ReferencedProperty")])
                elif "Fixed" in t:
                    fixed.append({"property": c.get("Property") or c.get("RelatedProperty"),
                                  "value": c.get("Value")})
            rels.append({
                "name": n.get("Name"),
                "target": n.get("RelatedEntity"),
                "relation_name": n.get("RelatedRelationName"),
                "cardinality": n.get("Cardinality"),
                "pairs": pairs,
                "fixed": fixed,
                "kind": "dichiarata",
            })

        entity_set = d.get("EntitySetName") or de.get("PublicCollectionName") or name
        etichetta = lbl(d.get("LabelId")) or humanize(name)
        entities[name] = {
            "name": name,
            "entity_set": entity_set,
            "label": etichetta,
            "label_id": d.get("LabelId"),
            "domain": config.domain_of(name, etichetta),
            "read_only": bool(d.get("IsReadOnly")),
            "config_enabled": bool(d.get("ConfigurationEnabled")),
            "aot_entity": de.get("Name"),
            "category": de.get("EntityCategory"),
            "odata_enabled": bool(de.get("DataServiceEnabled", True)),
            "dmf_enabled": bool(de.get("DataManagementEnabled", False)),
            "in_edmx": (entity_set in edmx_sets) if edmx_sets else None,
            "keys": keys,
            "fields": fields,
            "relations": rels,
            "inferred": [],
            "actions": [a.get("Name") for a in (d.get("Actions") or [])][:40],
        }

    # entita' presenti in DataEntities ma senza dettaglio pubblico:
    # vanno segnalate, sono buchi noti della fotografia
    missing = [d for d in de_index
               if d.get("PublicEntityName") and d["PublicEntityName"] not in entities]

    _infer_relations(entities)
    famiglie = _detect_families(entities, counts or {})

    return {
        "famiglie": famiglie,
        "entities": entities,
        "enums": enum_map,
        "companies": [{"code": c.get("DataArea"), "name": c.get("Name")} for c in companies],
        "senza_dettaglio": [
            {"name": d.get("Name"), "public": d.get("PublicEntityName"),
             "category": d.get("EntityCategory"),
             "odata": bool(d.get("DataServiceEnabled")),
             "dmf": bool(d.get("DataManagementEnabled"))}
            for d in missing
        ],
    }


def _infer_relations(entities: dict) -> None:
    """Deduce le relazioni NON dichiarate confrontando i nomi campo con le
    chiavi delle altre entita'. In F&O moltissimi legami reali esistono solo
    come convenzione di naming (nessuna FK fisica in SQL), quindi senza questo
    passaggio la mappa resta piena di isole."""
    key_index: dict[str, list[str]] = defaultdict(list)
    for name, e in entities.items():
        real_keys = [k for k in e["keys"] if k and k.lower() != "dataareaid"]
        if len(real_keys) == 1:
            k = real_keys[0].lower()
            if k not in config.NOISE_FIELDS and len(k) > 4:
                key_index[k].append(name)

    ftype = {(n, f["name"].lower()): f["type"]
             for n, e in entities.items() for f in e["fields"]}

    for name, e in entities.items():
        declared_fields = {p[0].lower() for r in e["relations"] for p in r["pairs"] if p[0]}
        own_keys = {k.lower() for k in e["keys"]}
        seen = set()
        for f in e["fields"]:
            fl = (f["name"] or "").lower()
            if fl in declared_fields or fl in own_keys or fl in config.NOISE_FIELDS:
                continue
            targets = key_index.get(fl)
            if not targets or fl in seen:
                continue
            seen.add(fl)
            # troppi candidati = nome poco distintivo, la relazione non e' affidabile
            if len(targets) > 6:
                continue
            for t in targets:
                if t == name:
                    continue
                tt = ftype.get((t, fl))
                if tt and f["type"] and tt != f["type"]:
                    continue
                score = 60
                if len(fl) > 10:
                    score += 15
                if len(targets) == 1:
                    score += 20
                if f["is_mandatory"]:
                    score += 5
                e["inferred"].append({
                    "name": f"{f['name']}→{t}",
                    "target": t,
                    "cardinality": "Single",
                    "pairs": [[f["name"], f["name"]]],
                    "kind": "inferita",
                    "confidenza": min(score, 99),
                })


# Penalizzazioni per scegliere l'entita' di riferimento in una famiglia:
# le varianti analitiche (Bi/CDS/Staging) espongono lo stesso dato ma non sono
# quelle giuste su cui interrogare o scrivere.
_PENALITA = (
    ("Obsolete", 90), ("Staging", 80), ("BiEntit", 60), ("CDSEntit", 55),
    ("CDREntit", 55), ("CDS", 45), ("CDR", 45), ("AIEntity", 40),
    ("Legacy", 40), ("Bundle", 35), ("D365", 30), ("Preview", 30),
    ("Import", 25), ("Export", 25), ("Entities", 10), ("Entity", 8),
)

# Frammenti che distinguono una variante dalla sostanza dell'entita'.
_RUMORE = re.compile(
    r"(CDSEntity|CDREntity|BiEntity|CDSEntities|CDREntities|BiEntities|"
    r"AIEntity|CDS|CDR|Bundle|D365|Entities|Entity|Staging|V\d+)", re.I)


def _normalizza(nome: str) -> str:
    """Nome ridotto alla sostanza: InventItemPriceV3 e InventItemPrice
    collassano entrambi su 'inventitemprice'."""
    return _RUMORE.sub("", nome).lower()


def _versione(nome: str) -> int:
    """Versione anche a meta' nome: SalesInvoiceV4Line -> 4."""
    vs = [int(x) for x in re.findall(r"V(\d+)(?=[A-Z]|$)", nome)]
    return max(vs) if vs else 1


def _punteggio(e: dict) -> int:
    """Piu' alto = candidato migliore come entita' di riferimento."""
    n = e["name"]
    p = 100 + _versione(n) * 12
    for frammento, malus in _PENALITA:
        if frammento in n:
            p -= malus
    if e.get("read_only"):
        p -= 15
    if not e.get("odata_enabled", True):
        p -= 50
    if e.get("keys"):
        p += 10
    p -= len(n) // 12
    return p


def _detect_families(entities: dict, counts: dict) -> list:
    """Raggruppa le entita' che espongono lo stesso dato.

    D365 pubblica lo stesso contenuto sotto piu' entita': la versionata
    (V2/V3), la variante analitica (BiEntities/CDSEntities), a volte una di
    staging. Hanno conteggi identici e quasi gli stessi campi. Per un
    assistente sono un problema: scegliendone una a caso dà risposte diverse
    alla stessa domanda. Qui vengono raggruppate e se ne elegge una.
    """
    per_conteggio: dict = defaultdict(list)
    for n, e in entities.items():
        c = counts.get(n)
        if c:  # solo entita' popolate: due entita' vuote non sono equivalenti
            per_conteggio[c].append(n)

    famiglie = []
    for c, nomi in per_conteggio.items():
        if len(nomi) < 2:
            continue
        campi = {n: {f["name"].lower() for f in entities[n]["fields"]} for n in nomi}
        norm = {n: _normalizza(n) for n in nomi}
        restanti = list(nomi)
        while restanti:
            capo = restanti.pop(0)
            gruppo = [capo]
            for altro in list(restanti):
                a, b = campi[capo], campi[altro]
                if not a or not b:
                    continue
                # Due entita' possono avere lo stesso conteggio per puro caso:
                # si richiede anche che i nomi condividano la sostanza.
                na, nb = norm[capo], norm[altro]
                if not (na and nb and (na in nb or nb in na)):
                    continue
                inter = len(a & b)
                jac = inter / len(a | b)
                if jac >= 0.6 or inter >= 0.9 * min(len(a), len(b)):
                    gruppo.append(altro)
                    restanti.remove(altro)
            if len(gruppo) < 2:
                continue
            gruppo.sort(key=lambda n: -_punteggio(entities[n]))
            preferita = gruppo[0]
            fid = f"fam_{preferita}"
            for n in gruppo:
                entities[n]["famiglia"] = fid
                entities[n]["preferita"] = (n == preferita)
                entities[n]["equivalenti"] = [x for x in gruppo if x != n]
            famiglie.append({
                "id": fid, "preferita": preferita, "righe": c,
                "membri": gruppo,
                "etichetta": entities[preferita]["label"],
                "dominio": entities[preferita]["domain"],
            })
    famiglie.sort(key=lambda f: -f["righe"])
    return famiglie


def parse_edmx_sets(edmx_text: str) -> set[str]:
    """Estrae i nomi degli EntitySet realmente esposti su /data."""
    if not edmx_text:
        return set()
    return set(re.findall(r'<EntitySet\s+Name="([^"]+)"', edmx_text))


def stats(model: dict) -> dict:
    ents = model["entities"]
    nf = sum(len(e["fields"]) for e in ents.values())
    nr = sum(len(e["relations"]) for e in ents.values())
    ni = sum(len(e["inferred"]) for e in ents.values())
    by_dom = defaultdict(int)
    for e in ents.values():
        by_dom[e["domain"]] += 1
    return {
        "entita": len(ents),
        "campi": nf,
        "relazioni_dichiarate": nr,
        "relazioni_inferite": ni,
        "enum": len(model["enums"]),
        "societa": len(model["companies"]),
        "entita_senza_dettaglio": len(model["senza_dettaglio"]),
        "famiglie_duplicate": len(model.get("famiglie") or []),
        "entita_ridondanti": sum(len(f["membri"]) - 1 for f in (model.get("famiglie") or [])),
        "per_dominio": dict(sorted(by_dom.items(), key=lambda x: -x[1])),
    }

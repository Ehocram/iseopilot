"""Generazione di file scaricabili (Word, PowerPoint, Excel, PDF).

Flusso: l'utente chiede in chat "creami un word/excel/ppt/pdf …". Qui si rileva
il formato, si fa generare a Claude il CONTENUTO in JSON strutturato, e si
costruisce il file con le librerie sul server. Word e PowerPoint usano i TEMPLATE
aziendali (templateIseo.docx, Presentation_template_1.pptx); Excel è generato da
zero. Per il PDF si converte il Word (LibreOffice se presente) con fallback a un
PDF in stile ISEO.

I file finiti vanno in una cartella temporanea e vengono serviti via token
(vedi connectors.register_report / main download endpoint).
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path

from . import orchestrator

TEMPLATES_DIR = Path(__file__).resolve().parent / "doc_templates"
DOCX_TEMPLATE = TEMPLATES_DIR / "templateIseo.docx"
PPTX_TEMPLATE = TEMPLATES_DIR / "Presentation_template_1.pptx"
OUT_DIR = Path(tempfile.gettempdir()) / "iseopilot_docs"
OUT_DIR.mkdir(parents=True, exist_ok=True)

ISEO_RED = "EC1D2B"

# ── 1) Rilevamento intento + formato ───────────────────────
_FMT_WORD = ("word", "documento word", ".docx", "in word", "un doc ")
_FMT_XLS = ("excel", "foglio di calcolo", "foglio elettronico", "spreadsheet", ".xlsx", "in excel")
_FMT_PPT = ("powerpoint", "power point", "presentazione", "slide", "slides", "ppt", ".pptx", "deck")
_FMT_PDF = ("pdf", ".pdf", "in pdf")
# Verbi di generazione: forme FORTI (creazione esplicita, con le coniugazioni
# reali: "mi generi", "mi crei", "fammi") e forme DEBOLI (cortesia/desiderio).
# Le deboli valgono solo se la frase non è una richiesta di spiegazione
# ("puoi spiegarmi come funziona Excel?" resta una chat, non genera file).
_STRONG_VERB_RE = re.compile(
    r"\b(crea(re|mi|temi)?|crei|genera(re|mi)?|generi|fammi|fai|fatemi|"
    r"produci|produrre|produrmi|prepara(re|mi)?|prepari|esporta(re|mi)?|esporti|"
    r"scarica(re|mi)?|scarichi|redigi|redigere|stila(re)?|stilami|"
    r"realizza(re|mi)?|realizzi|costruisci(mi)?|costruire|buttami|"
    r"create|generate|make|build|produce|export|download|prepare|draft)\b",
    re.IGNORECASE)
_WEAK_VERB_RE = re.compile(
    r"\b(vorrei|voglio|desidero|mi\s+serve|servirebbe|avrei\s+bisogno|"
    r"puoi|potresti|riesci|sapresti|"
    r"can\s+you|could\s+you|i\s+need|i\s+want|i'd\s+like)\b",
    re.IGNORECASE)
_EXPLAIN_RE = re.compile(
    r"\b(spiega|spiegami|spieghi|cos'?\s?è|cosa\s+è|come\s+funziona|"
    r"come\s+si\s+usa|aiutami\s+a\s+capire|che\s+cos|differenza\s+tra|"
    r"explain|what\s+is|how\s+does|how\s+to\s+use)\b",
    re.IGNORECASE)


def detect_request(text: str) -> str | None:
    """Ritorna il formato richiesto ('docx'|'xlsx'|'pptx'|'pdf') o None."""
    tl = (text or "").lower()
    # l'ordine conta: pptx e xlsx prima di docx/pdf generici
    if any(k in tl for k in _FMT_PPT):
        fmt = "pptx"
    elif any(k in tl for k in _FMT_XLS):
        fmt = "xlsx"
    elif any(k in tl for k in _FMT_WORD):
        fmt = "docx"
    elif any(k in tl for k in _FMT_PDF):
        fmt = "pdf"
    else:
        return None
    # estensione esplicita: intento inequivocabile
    if any(e in tl for e in (".docx", ".xlsx", ".pptx", ".pdf")):
        return fmt
    if _STRONG_VERB_RE.search(tl):
        return fmt
    if _WEAK_VERB_RE.search(tl) and not _EXPLAIN_RE.search(tl):
        return fmt
    return None


# ── 2) Generazione del contenuto strutturato (Claude → JSON) ─
def _parse_json(raw: str) -> dict:
    raw = (raw or "").strip()
    raw = re.sub(r"^```(?:json)?", "", raw).strip()
    raw = re.sub(r"```$", "", raw).strip()
    # estrai il primo oggetto JSON bilanciato
    start = raw.find("{")
    if start < 0:
        raise ValueError("Nessun JSON nella risposta del modello.")
    depth, end = 0, None
    for i in range(start, len(raw)):
        if raw[i] == "{":
            depth += 1
        elif raw[i] == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    return json.loads(raw[start:end] if end else raw[start:])


_SCHEMAS = {
    "docx": (
        'Genera SOLO un oggetto JSON valido, senza testo prima o dopo, con questo schema:\n'
        '{"title": "Titolo del documento",\n'
        ' "subtitle": "sottotitolo opzionale",\n'
        ' "sections": [{"heading": "Titolo sezione", "paragraphs": ["par1","par2"], "bullets": ["punto1","punto2"]}]}\n'
        'Ogni sezione può avere "paragraphs" e/o "bullets". Scrivi contenuto reale, completo e professionale.'
    ),
    "pdf": None,  # usa lo schema docx
    "pptx": (
        'Genera SOLO un oggetto JSON valido, senza testo prima o dopo, con questo schema:\n'
        '{"title": "Titolo presentazione", "subtitle": "sottotitolo",\n'
        ' "slides": [{"title": "Titolo slide", "bullets": ["punto1","punto2","punto3"]}]}\n'
        'Da 4 a 12 slide. Ogni slide 3-6 bullet concisi (max ~12 parole). Contenuto reale e professionale.'
    ),
    "xlsx": (
        'Genera SOLO un oggetto JSON valido, senza testo prima o dopo, con questo schema:\n'
        '{"title": "Titolo", "sheets": [{"name": "Foglio1",\n'
        '  "columns": ["Col A","Col B","Importo"],\n'
        '  "rows": [["v1","v2",100],["v3","v4",200]],\n'
        '  "total_columns": [2]}]}\n'
        '"total_columns" (opzionale) = indici (0-based) delle colonne numeriche da totalizzare con una formula SOMMA. '
        'Usa numeri reali (non stringhe) nelle celle numeriche. Contenuto reale e completo.'
    ),
}


def build_spec(fmt: str, user_request: str, context: str, settings: dict,
               history_text: str = "") -> dict:
    schema = _SCHEMAS["docx"] if fmt in ("docx", "pdf") else _SCHEMAS[fmt]
    lang = settings.get("reply_lang", "Italiano")
    system = (
        f"Sei un generatore di documenti aziendali ISEO. Rispondi nella lingua: {lang}. "
        "Produci ESCLUSIVAMENTE JSON valido secondo lo schema indicato, senza commenti, "
        "senza markdown, senza testo aggiuntivo. Il contenuto deve essere reale, utile e "
        "PERTINENTE alla richiesta specifica (mai generico), basato sul materiale e sulla "
        "conversazione forniti quando presenti."
    )
    ctx = ("\n\n=== MATERIALE DI RIFERIMENTO ===\n" + context.strip()) if context and context.strip() else ""
    hist = ("\n\n=== CONVERSAZIONE PRECEDENTE (per capire cosa vuole davvero l'utente) ===\n"
            + history_text.strip()) if history_text and history_text.strip() else ""
    user = f"{schema}\n\nRICHIESTA DELL'UTENTE:\n{user_request}{hist}{ctx}"
    raw = orchestrator.complete(system, user, settings, max_tokens=4000)
    return _parse_json(raw)


# ── 3) Costruttori per formato ─────────────────────────────
def _safe_name(title: str, ext: str) -> str:
    base = re.sub(r"[^0-9A-Za-zÀ-ÿ _-]+", "", (title or "documento")).strip()[:48] or "documento"
    return f"{base}.{ext}"


def gen_docx(spec: dict) -> tuple[str, str]:
    from docx import Document
    doc = Document(str(DOCX_TEMPLATE)) if DOCX_TEMPLATE.is_file() else Document()
    # svuota i paragrafi placeholder del template (mantiene header/footer/logo)
    for p in list(doc.paragraphs):
        p._element.getparent().remove(p._element)

    def style_or(name, fallback):
        try:
            _ = doc.styles[name]
            return name
        except Exception:
            return fallback

    title_style = style_or("ISEO Titolo", "Title")
    body_style = style_or("Paragrafo base ISEO", "Normal")

    doc.add_paragraph(spec.get("title", "Documento"), style=title_style)
    if spec.get("subtitle"):
        doc.add_paragraph(spec["subtitle"], style=body_style)
    for sec in spec.get("sections", []):
        if sec.get("heading"):
            try:
                doc.add_heading(sec["heading"], level=1)
            except Exception:
                doc.add_paragraph(sec["heading"], style=title_style)
        for par in sec.get("paragraphs", []):
            doc.add_paragraph(str(par), style=body_style)
        for b in sec.get("bullets", []):
            try:
                doc.add_paragraph(str(b), style="List Bullet")
            except Exception:
                doc.add_paragraph("• " + str(b), style=body_style)

    name = _safe_name(spec.get("title", "documento"), "docx")
    path = OUT_DIR / (next(tempfile._get_candidate_names()) + ".docx")
    doc.save(str(path))
    return str(path), name


def gen_pptx(spec: dict) -> tuple[str, str]:
    from pptx import Presentation
    from pptx.util import Inches, Pt
    from pptx.dml.color import RGBColor
    from pptx.enum.text import PP_ALIGN

    prs = Presentation(str(PPTX_TEMPLATE)) if PPTX_TEMPLATE.is_file() else Presentation()
    # rimuovi le slide di esempio del template (mantiene tema, master, footer):
    # va rimossa sia la voce nell'elenco sia la relazione, altrimenti le part
    # restano orfane e generano nomi duplicati nello zip.
    from pptx.oxml.ns import qn
    sld_id_lst = prs.slides._sldIdLst
    for sld_id in list(sld_id_lst):
        rId = sld_id.get(qn("r:id"))
        try:
            prs.part.drop_rel(rId)
        except Exception:
            pass
        sld_id_lst.remove(sld_id)

    layout = prs.slide_layouts[0]
    W = prs.slide_width

    def add_textbox(slide, left, top, width, height):
        tb = slide.shapes.add_textbox(left, top, width, height)
        tb.text_frame.word_wrap = True
        return tb

    # copertina
    cover = prs.slides.add_slide(layout)
    t = add_textbox(cover, Inches(0.6), Inches(1.8), W - Inches(1.2), Inches(1.6))
    run = t.text_frame.paragraphs[0].add_run()
    run.text = spec.get("title", "Presentazione")
    run.font.size = Pt(32); run.font.bold = True; run.font.color.rgb = RGBColor.from_string(ISEO_RED)
    if spec.get("subtitle"):
        st = add_textbox(cover, Inches(0.6), Inches(3.3), W - Inches(1.2), Inches(0.8))
        r2 = st.text_frame.paragraphs[0].add_run()
        r2.text = spec["subtitle"]; r2.font.size = Pt(16)

    # slide di contenuto
    for sl in spec.get("slides", []):
        s = prs.slides.add_slide(layout)
        th = add_textbox(s, Inches(0.6), Inches(0.4), W - Inches(1.2), Inches(0.9))
        tr = th.text_frame.paragraphs[0].add_run()
        tr.text = sl.get("title", ""); tr.font.size = Pt(22); tr.font.bold = True
        tr.font.color.rgb = RGBColor.from_string(ISEO_RED)
        body = add_textbox(s, Inches(0.7), Inches(1.5), W - Inches(1.4), Inches(3.4))
        tf = body.text_frame
        first = True
        for b in sl.get("bullets", []):
            p = tf.paragraphs[0] if first else tf.add_paragraph()
            first = False
            run = p.add_run(); run.text = "•  " + str(b); run.font.size = Pt(16)
            p.space_after = Pt(6)

    name = _safe_name(spec.get("title", "presentazione"), "pptx")
    path = OUT_DIR / (next(tempfile._get_candidate_names()) + ".pptx")
    prs.save(str(path))
    return str(path), name


def gen_xlsx(spec: dict) -> tuple[str, str]:
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.utils import get_column_letter

    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    header_fill = PatternFill("solid", fgColor=ISEO_RED)
    header_font = Font(color="FFFFFF", bold=True)

    sheets = spec.get("sheets") or [{"name": "Foglio1", "columns": [], "rows": []}]
    for sh in sheets:
        ws = wb.create_sheet(title=(sh.get("name") or "Foglio")[:31])
        cols = sh.get("columns", [])
        for j, c in enumerate(cols, 1):
            cell = ws.cell(row=1, column=j, value=str(c))
            cell.fill = header_fill; cell.font = header_font
            cell.alignment = Alignment(horizontal="center")
        rows = sh.get("rows", [])
        for i, row in enumerate(rows, 2):
            for j, val in enumerate(row, 1):
                ws.cell(row=i, column=j, value=val)
        # totali con formula SOMMA per le colonne indicate
        totals = sh.get("total_columns", [])
        if rows and totals:
            tr = len(rows) + 2
            ws.cell(row=tr, column=1, value="TOTALE").font = Font(bold=True)
            for idx in totals:
                col = idx + 1
                if col <= len(cols):
                    letter = get_column_letter(col)
                    ws.cell(row=tr, column=col,
                            value=f"=SUM({letter}2:{letter}{len(rows)+1})").font = Font(bold=True)
        # larghezza colonne
        for j, c in enumerate(cols, 1):
            width = max(12, min(40, len(str(c)) + 4))
            ws.column_dimensions[get_column_letter(j)].width = width

    name = _safe_name(spec.get("title", "dati"), "xlsx")
    path = OUT_DIR / (next(tempfile._get_candidate_names()) + ".xlsx")
    wb.save(str(path))
    return str(path), name


def _docx_to_pdf(docx_path: str) -> str | None:
    """Converte docx→pdf con LibreOffice headless, se disponibile."""
    import shutil
    import subprocess
    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    if not soffice:
        return None
    try:
        subprocess.run([soffice, "--headless", "--convert-to", "pdf", "--outdir",
                        str(OUT_DIR), docx_path], timeout=120,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        pdf = Path(docx_path).with_suffix(".pdf")
        return str(pdf) if pdf.is_file() else None
    except Exception:
        return None


def _pdf_reportlab(spec: dict) -> tuple[str, str]:
    """Fallback PDF in stile ISEO (se LibreOffice non è disponibile)."""
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import cm
    from reportlab.lib import colors
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, ListFlowable, ListItem

    name = _safe_name(spec.get("title", "documento"), "pdf")
    path = OUT_DIR / (next(tempfile._get_candidate_names()) + ".pdf")
    styles = getSampleStyleSheet()
    red = colors.HexColor("#" + ISEO_RED)
    h_title = ParagraphStyle("ISEOTitle", parent=styles["Title"], textColor=red, fontSize=22)
    h_sec = ParagraphStyle("ISEOSec", parent=styles["Heading1"], textColor=red, fontSize=14)
    body = styles["BodyText"]
    doc = SimpleDocTemplate(str(path), pagesize=A4, topMargin=2*cm, bottomMargin=2*cm)
    flow = [Paragraph(spec.get("title", "Documento"), h_title)]
    if spec.get("subtitle"):
        flow.append(Paragraph(spec["subtitle"], body))
    flow.append(Spacer(1, 0.4*cm))
    for sec in spec.get("sections", []):
        if sec.get("heading"):
            flow.append(Paragraph(sec["heading"], h_sec))
        for par in sec.get("paragraphs", []):
            flow.append(Paragraph(str(par), body)); flow.append(Spacer(1, 0.15*cm))
        bullets = sec.get("bullets", [])
        if bullets:
            flow.append(ListFlowable([ListItem(Paragraph(str(b), body)) for b in bullets],
                                     bulletType="bullet"))
        flow.append(Spacer(1, 0.25*cm))
    doc.build(flow)
    return str(path), name


def gen_pdf(spec: dict) -> tuple[str, str]:
    # preferito: Word sul template ISEO -> PDF (fedele a header/footer/logo)
    try:
        docx_path, _ = gen_docx(spec)
        pdf = _docx_to_pdf(docx_path)
        if pdf:
            return pdf, _safe_name(spec.get("title", "documento"), "pdf")
    except Exception:
        pass
    # fallback: PDF in stile ISEO con reportlab
    return _pdf_reportlab(spec)


_BUILDERS = {"docx": gen_docx, "pptx": gen_pptx, "xlsx": gen_xlsx, "pdf": gen_pdf}


def generate(fmt: str, user_request: str, context: str, settings: dict,
             history_text: str = "") -> tuple[str, str]:
    """Genera il file richiesto. Ritorna (path, filename_visibile)."""
    spec = build_spec(fmt, user_request, context, settings, history_text)
    return _BUILDERS[fmt](spec)

"""
dubstudio.py — ISEO Dub Studio dentro ISEOPilot (Incremento 9).

Due modalità per ogni job:
  A "doppiaggio": voce dell'utente clonata dal SUO profilo registrato con
    consenso (mai dall'audio del video) — trascrizione → traduzione →
    revisione umana → sintesi Chatterbox → time-fit → remux.
  B "sottotitoli": audio ORIGINALE + sottotitoli impressi nella lingua scelta
    — stessi primi tre stadi, poi burn-in ffmpeg. Nessun modello ML pesante.

Architettura a DUE processi sulla stessa volume /data (coda su file, atomica):
  * app web (questo modulo): crea job, valida limiti, TRADUZIONE (chiave e
    modello admin vivono qui), revisione, SRT, montaggio sottotitoli (B),
    gestione voce. Runner leggero in thread.
  * dub worker (container separato, worker/dub_worker.py): SOLO gli stadi ML
    (trascrizione faster-whisper, sintesi+assemblaggio Chatterbox) — immagine
    propria per via dei pin rigidi di chatterbox-tts (torch/transformers
    esatti). Coda profondità 1 per costruzione: un worker, un job alla volta.

Stati del job (campo `stato` in job.json, transizioni atomiche via rename):
  coda_trascrizione → trascrizione → tradurre → traduzione → revisione
  → [B] coda_sottotitoli → montaggio → pronto
  → [A] coda_sintesi → sintesi → pronto
  → errore (campo `errore` sempre parlante)
Il gate umano è `revisione`: nulla viene sintetizzato o impresso prima
che l'utente confermi i testi.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import threading
import time
import uuid
from pathlib import Path

from . import store
from .engines import dub_pipeline, dub_translate

DATA_DIR = Path(os.environ.get("APP_DATA_DIR", "/data"))
DUB_DIR = DATA_DIR / "dub"
HEARTBEAT = DUB_DIR / "worker_heartbeat.json"
HEARTBEAT_STALE_S = 120

LANGS = ("it", "en", "es", "fr", "de")
LANG_LABELS = {"it": "Italiano", "en": "Inglese", "es": "Spagnolo",
               "fr": "Francese", "de": "Tedesco"}

# Stati di competenza del WORKER (ML) e dell'APP (leggeri)
WORKER_QUEUE_STATES = ("coda_trascrizione", "coda_sintesi")
APP_QUEUE_STATES = ("tradurre", "coda_sottotitoli")
RUNNING_STATES = ("trascrizione", "traduzione", "sintesi", "montaggio")
FINAL_STATES = ("pronto", "errore")


# ── Impostazioni (admin) ────────────────────────────────────
def enabled() -> bool:
    return store.get_setting("dub_enabled", "0") == "1"


def user_allowed(username: str) -> bool:
    """Grant PER-UTENTE deciso dall'admin (pagina Utenti)."""
    return store.get_user_setting(username, "dub_access", "0") == "1"


def settings() -> dict:
    def _int(key, default):
        try:
            return max(1, int(store.get_setting(key, str(default)) or default))
        except Exception:
            return default
    return {
        "model": store.get_setting("claude_model_dub", "claude-sonnet-4-6"),
        "whisper_model": store.get_setting("dub_whisper_model", "small"),
        "max_mb": _int("dub_max_mb", 300),
        "max_min": _int("dub_max_min", 20),
        "tts_threads": _int("dub_tts_threads", 2),
        "whisper_threads": _int("dub_whisper_threads", 4),
    }


# ── Percorsi per-utente (stesso hashing dei token) ──────────
def _user_dir(username: str) -> Path:
    import hashlib
    d = DUB_DIR / hashlib.sha256(username.encode()).hexdigest()[:16]
    (d / "jobs").mkdir(parents=True, exist_ok=True)
    return d


def _voice_dir(username: str) -> Path:
    d = _user_dir(username) / "voice"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _job_dir(username: str, jid: str) -> Path:
    return _user_dir(username) / "jobs" / jid


# ── job.json atomico ────────────────────────────────────────
def _read_json(p: Path) -> dict:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_json(p: Path, data: dict) -> None:
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, p)


def job_read(username: str, jid: str) -> dict:
    return _read_json(_job_dir(username, jid) / "job.json")


def _job_update(username: str, jid: str, **campi) -> dict:
    p = _job_dir(username, jid) / "job.json"
    j = _read_json(p)
    j.update(campi)
    _write_json(p, j)
    return j


def _log(username: str, jid: str, msg: str) -> None:
    try:
        with open(_job_dir(username, jid) / "job.log", "a", encoding="utf-8") as f:
            f.write(time.strftime("[%H:%M:%S] ") + str(msg) + "\n")
    except Exception:
        pass


# ── Voce personale ──────────────────────────────────────────
def voice_status(username: str) -> dict:
    d = _voice_dir(username)
    wav, meta = d / "voce.wav", d / "voce.json"
    if not wav.is_file():
        return {"present": False}
    m = _read_json(meta)
    return {"present": True, "created": m.get("created", ""),
            "duration_s": m.get("duration_s", 0), "consent": bool(m.get("consent"))}


def voice_check(raw: bytes, orig_name: str) -> dict:
    """Analisi qualità della registrazione caricata (senza salvarla)."""
    from .engines import dub_enroll
    import tempfile
    suf = "." + (orig_name.rsplit(".", 1)[-1].lower() if "." in orig_name else "webm")
    tmp = tempfile.mktemp(suffix=suf)
    Path(tmp).write_bytes(raw)
    try:
        a, rate = dub_enroll.to_float_audio(tmp)
    finally:
        try:
            os.remove(tmp)
        except Exception:
            pass
    m = dub_enroll.analyze(a, rate)
    ok, righe = dub_enroll.verdicts(m)
    return {"ok": ok, "righe": righe, "metriche": m, "_audio": (a, rate)}


def voice_save(username: str, raw: bytes, orig_name: str, consent: bool) -> dict:
    """Verifica e salva il profilo voce. SENZA consenso non si salva."""
    if not consent:
        return {"ok": False, "errore": "Il consenso esplicito è obbligatorio: "
                                       "spunta la casella per salvare la tua voce."}
    from .engines import dub_enroll
    chk = voice_check(raw, orig_name)
    if not chk["ok"]:
        return {"ok": False, "errore": "Qualità registrazione insufficiente.",
                "righe": chk["righe"]}
    a, rate = chk["_audio"]
    dub_enroll.save_profile(a, rate, str(_voice_dir(username)), "voce", consent=True)
    return {"ok": True, "righe": chk["righe"], "status": voice_status(username)}


def voice_delete(username: str) -> bool:
    d = _voice_dir(username)
    tolto = False
    for f in (d / "voce.wav", d / "voce.json"):
        if f.is_file():
            f.unlink()
            tolto = True
    return tolto


# ── Creazione job ───────────────────────────────────────────
_SAFE_NAME = re.compile(r"[^\w\-. ]+")


def job_create(username: str, raw: bytes, filename: str,
               src: str, dst: str, mode: str) -> dict:
    """Valida i limiti (parlante) e accoda la trascrizione. Ritorna
    {ok, jid} o {ok: False, errore}."""
    cfg = settings()
    if src not in LANGS or dst not in LANGS:
        return {"ok": False, "errore": "Lingua non supportata."}
    if src == dst:
        return {"ok": False, "errore": "Sorgente e destinazione coincidono: scegli due lingue diverse."}
    if mode not in ("doppiaggio", "sottotitoli"):
        return {"ok": False, "errore": "Modalità non valida."}
    mb = len(raw) / (1024 * 1024)
    if mb > cfg["max_mb"]:
        return {"ok": False, "errore":
                f"Il video pesa {mb:.0f} MB: il limite è {cfg['max_mb']} MB. "
                "Chiedi all'amministratore di alzarlo dalla pagina Motore."}
    if mode == "doppiaggio":
        vs = voice_status(username)
        if not (vs.get("present") and vs.get("consent")):
            return {"ok": False, "errore":
                    "La modalità doppiaggio usa la TUA voce: registra prima il "
                    "profilo voce (con consenso) dal pannello qui sopra. In "
                    "alternativa usa la modalità sottotitoli."}
    jid = uuid.uuid4().hex[:12]
    jd = _job_dir(username, jid)
    jd.mkdir(parents=True, exist_ok=True)
    safe = _SAFE_NAME.sub("", filename or "video.mp4").strip() or "video.mp4"
    ext = "." + safe.rsplit(".", 1)[-1].lower() if "." in safe else ".mp4"
    video = jd / ("video" + ext)
    video.write_bytes(raw)
    # limite di DURATA (ffprobe): è la durata, non i MB, a governare i tempi
    try:
        dur_s = dub_pipeline.ffprobe_duration(str(video))
    except Exception as e:
        shutil.rmtree(jd, ignore_errors=True)
        return {"ok": False, "errore": f"Il file non sembra un video leggibile: {e}"}
    if dur_s > cfg["max_min"] * 60:
        shutil.rmtree(jd, ignore_errors=True)
        return {"ok": False, "errore":
                f"Il video dura {dur_s/60:.0f} minuti: il limite è {cfg['max_min']} "
                "minuti. Chiedi all'amministratore di alzarlo dalla pagina Motore."}
    if mode == "doppiaggio":
        shutil.copyfile(_voice_dir(username) / "voce.wav", jd / "voice_ref.wav")
    _write_json(jd / "job.json", {
        "id": jid, "utente": username, "video": video.name,
        "nome_video": safe, "src": src, "dst": dst, "mode": mode,
        "stato": "coda_trascrizione", "progress": 0,
        "creato": time.strftime("%Y-%m-%d %H:%M:%S"),
        "whisper_model": cfg["whisper_model"],
        "whisper_threads": cfg["whisper_threads"],
        "tts_threads": cfg["tts_threads"],
        "durata_s": round(dur_s, 1), "errore": "",
    })
    _log(username, jid, f"job creato: {safe} · {src}→{dst} · modalità {mode} · "
                        f"{dur_s/60:.1f} min")
    return {"ok": True, "jid": jid}


def job_list(username: str) -> list[dict]:
    jobs = []
    base = _user_dir(username) / "jobs"
    for d in base.iterdir() if base.is_dir() else []:
        j = _read_json(d / "job.json")
        if j.get("id"):
            jobs.append(j)
    jobs.sort(key=lambda x: x.get("creato", ""), reverse=True)
    return jobs


def job_status(username: str, jid: str) -> dict:
    j = job_read(username, jid)
    if not j:
        return {"errore": "Job inesistente."}
    log_tail = ""
    lp = _job_dir(username, jid) / "job.log"
    if lp.is_file():
        try:
            log_tail = "\n".join(lp.read_text(encoding="utf-8").splitlines()[-12:])
        except Exception:
            pass
    j["log_tail"] = log_tail
    j["worker_ok"] = worker_alive()
    return j


def job_delete(username: str, jid: str) -> bool:
    jd = _job_dir(username, jid)
    if not (jd / "job.json").is_file():
        return False
    shutil.rmtree(jd, ignore_errors=True)
    return True


# ── Revisione (gate umano) ──────────────────────────────────
def review_get(username: str, jid: str) -> dict:
    j = job_read(username, jid)
    segs = _read_json(_job_dir(username, jid) / "segments.json") or []
    budgets = []
    if segs:
        class _S:  # char_budgets vuole oggetti con start/end
            def __init__(self, d):
                self.start, self.end = d["start"], d["end"]
        budgets = dub_pipeline.char_budgets([_S(s) for s in segs])
    return {"job": j, "segments": segs, "budgets": budgets}


def review_save(username: str, jid: str, testi: list[str]) -> dict:
    p = _job_dir(username, jid) / "segments.json"
    segs = _read_json(p) or []
    if len(testi) != len(segs):
        return {"ok": False, "errore": f"Attesi {len(segs)} testi, ricevuti {len(testi)}."}
    j = job_read(username, jid)
    dst = j.get("dst", "en")
    for s, t in zip(segs, testi):
        s.setdefault("tr", {})[dst] = str(t)
    _write_json(p, segs)
    _log(username, jid, f"revisione salvata ({len(segs)} segmenti)")
    return {"ok": True}


def srt_text(username: str, jid: str) -> str:
    j = job_read(username, jid)
    segs = _read_json(_job_dir(username, jid) / "segments.json") or []
    dst = j.get("dst", "en")

    def ts(t):
        h, r = divmod(t, 3600)
        m, s = divmod(r, 60)
        return f"{int(h):02d}:{int(m):02d}:{int(s):02d},{int((s % 1) * 1000):03d}"
    out = []
    for i, s in enumerate(segs, 1):
        txt = (s.get("tr") or {}).get(dst) or s.get("text", "")
        out.append(f"{i}\n{ts(s['start'])} --> {ts(s['end'])}\n{txt}\n")
    return "\n".join(out)


def job_continue(username: str, jid: str) -> dict:
    """Dopo la revisione: B accoda il montaggio sottotitoli (app), A accoda la
    sintesi (worker)."""
    j = job_read(username, jid)
    if j.get("stato") != "revisione":
        return {"ok": False, "errore": f"Il job è in stato '{j.get('stato')}', "
                                       "non in revisione."}
    nuovo = ("coda_sottotitoli" if j.get("mode") == "sottotitoli" else "coda_sintesi")
    _job_update(username, jid, stato=nuovo)
    _log(username, jid, f"revisione confermata → {nuovo}")
    _ensure_runner()
    return {"ok": True, "stato": nuovo}


# ── Worker heartbeat ────────────────────────────────────────
def worker_alive() -> bool:
    hb = _read_json(HEARTBEAT)
    try:
        return (time.time() - float(hb.get("ts", 0))) < HEARTBEAT_STALE_S
    except Exception:
        return False


# ── Runner APP-SIDE (stadi leggeri: traduzione, burn-in B) ──
_RUNNER = {"thread": None}
_RUNNER_LOCK = threading.Lock()


def _iter_jobs_in(stati: tuple) -> list[tuple[str, str]]:
    out = []
    if not DUB_DIR.is_dir():
        return out
    for udir in DUB_DIR.iterdir():
        jbase = udir / "jobs"
        if not jbase.is_dir():
            continue
        for jd in jbase.iterdir():
            j = _read_json(jd / "job.json")
            if j.get("stato") in stati:
                out.append((j.get("utente", ""), j.get("id", "")))
    return out


def _runner_tick() -> int:
    """Un giro del runner: processa AL PIÙ un job per stato. Ritorna quanti
    job ha lavorato (per i test)."""
    fatti = 0
    for utente, jid in _iter_jobs_in(("tradurre",)):
        _do_translate(utente, jid)
        fatti += 1
        break
    for utente, jid in _iter_jobs_in(("coda_sottotitoli",)):
        _do_burn(utente, jid)
        fatti += 1
        break
    return fatti


def _runner_loop():
    while True:
        try:
            if enabled():
                _runner_tick()
        except Exception as e:
            try:
                (DUB_DIR / "runner_error.log").open("a").write(
                    time.strftime("[%H:%M:%S] ") + repr(e) + "\n")
            except Exception:
                pass
        time.sleep(3)


def _ensure_runner():
    with _RUNNER_LOCK:
        t = _RUNNER.get("thread")
        if t and t.is_alive():
            return
        t = threading.Thread(target=_runner_loop, daemon=True,
                             name="dub-app-runner")
        _RUNNER["thread"] = t
        t.start()


def _do_translate(username: str, jid: str) -> None:
    jd = _job_dir(username, jid)
    j = _job_update(username, jid, stato="traduzione", progress=35)
    _log(username, jid, "traduzione in corso…")
    try:
        segs = _read_json(jd / "segments.json") or []
        if not segs:
            raise RuntimeError("nessun segmento trascritto")
        api_key = store.get_setting("claude_api_key", "")
        if not api_key:
            raise RuntimeError("chiave Claude non configurata (pagina Motore)")
        cfg = settings()

        class _S:
            def __init__(self, d):
                self.start, self.end = d["start"], d["end"]
        budgets = dub_pipeline.char_budgets([_S(s) for s in segs])
        testi = [s.get("text", "") for s in segs]
        tr = dub_translate.translate_claude(
            testi, j["dst"], api_key, model=cfg["model"],
            src=j.get("src", "it"), limits=budgets)
        for s, t in zip(segs, tr):
            s.setdefault("tr", {})[j["dst"]] = t
        _write_json(jd / "segments.json", segs)
        _job_update(username, jid, stato="revisione", progress=50)
        _log(username, jid, f"traduzione completata ({len(segs)} segmenti, "
                            f"modello {cfg['model']}): in attesa di revisione")
    except Exception as e:
        _job_update(username, jid, stato="errore",
                    errore=f"Traduzione fallita: {e}")
        _log(username, jid, f"ERRORE traduzione: {e}")


def _do_burn(username: str, jid: str) -> None:
    jd = _job_dir(username, jid)
    j = _job_update(username, jid, stato="montaggio", progress=70)
    _log(username, jid, "imprimo i sottotitoli (burn-in)…")
    try:
        stem = os.path.splitext(j["nome_video"])[0]
        srt = jd / f"{stem}_{j['dst'].upper()}.srt"
        srt.write_text(srt_text(username, jid), encoding="utf-8")
        out = jd / f"{stem}_{j['dst'].upper()}_sub.mp4"
        dub_pipeline.burn_subtitles(str(jd / j["video"]), str(srt), str(out),
                                    log=lambda m: _log(username, jid, m))
        _job_update(username, jid, stato="pronto", progress=100,
                    output=out.name, srt=srt.name)
        _log(username, jid, f"pronto: {out.name}")
    except Exception as e:
        _job_update(username, jid, stato="errore",
                    errore=f"Montaggio sottotitoli fallito: {e}")
        _log(username, jid, f"ERRORE montaggio: {e}")


def output_path(username: str, jid: str, quale: str) -> Path | None:
    """Percorso di un output ('video' | 'srt') se pronto."""
    j = job_read(username, jid)
    nome = j.get("output" if quale == "video" else "srt", "")
    if not nome:
        return None
    p = _job_dir(username, jid) / nome
    return p if p.is_file() else None

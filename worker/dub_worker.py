"""
dub_worker.py — worker ML di ISEO Dub Studio (container SEPARATO).

Perché separato: chatterbox-tts pinna torch==2.6.0, torchaudio==2.6.0 e
transformers==5.2.0 ESATTI — incompatibili per costruzione con lo stack
dell'app (sentence-transformers). Questo container ha la SUA immagine
(Dockerfile.dubworker) e condivide con l'app solo la volume /data.

Protocollo (file-based, atomico, zero infrastruttura nuova):
  * scansiona /data/dub/*/jobs/*/job.json cercando stato in
    (coda_trascrizione, coda_sintesi), il più vecchio prima;
  * UN job alla volta (coda profondità 1 per costruzione);
  * trascrizione: estrae audio, faster-whisper CPU int8 (thread cap admin),
    scrive segments.json, stato → "tradurre" (la traduzione la fa l'APP,
    che custodisce chiave e modello Claude);
  * sintesi: Chatterbox CPU (thread cap admin) con la voce del profilo
    dell'utente (voice_ref.wav nel job — mai dall'audio del video),
    time-fit, assemblaggio, remux; stato → "pronto";
  * heartbeat ogni giro: l'app mostra "worker non attivo" se manca.
Ciclo di vita modelli SEQUENZIALE: Whisper e Chatterbox mai insieme in RAM.
"""
import json
import os
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dub_pipeline  # copia di app/engines/dub_pipeline.py (vedi Dockerfile.dubworker)

DATA_DIR = Path(os.environ.get("APP_DATA_DIR", "/data"))
DUB_DIR = DATA_DIR / "dub"
HEARTBEAT = DUB_DIR / "worker_heartbeat.json"
POLL_S = 3


def _read(p: Path) -> dict:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write(p: Path, d: dict) -> None:
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, p)


def _update(jd: Path, **campi) -> dict:
    j = _read(jd / "job.json")
    j.update(campi)
    _write(jd / "job.json", j)
    return j


def _log(jd: Path, msg: str) -> None:
    try:
        with open(jd / "job.log", "a", encoding="utf-8") as f:
            f.write(time.strftime("[%H:%M:%S] ") + str(msg) + "\n")
    except Exception:
        pass


def _heartbeat(stato: str) -> None:
    try:
        DUB_DIR.mkdir(parents=True, exist_ok=True)
        _write(HEARTBEAT, {"ts": time.time(), "stato": stato,
                           "pid": os.getpid()})
    except Exception:
        pass


def _next_job():
    """(job_dir, job) più vecchio in coda, o (None, None)."""
    cand = []
    if not DUB_DIR.is_dir():
        return None, None
    for udir in DUB_DIR.iterdir():
        jbase = udir / "jobs"
        if not jbase.is_dir():
            continue
        for jd in jbase.iterdir():
            j = _read(jd / "job.json")
            if j.get("stato") in ("coda_trascrizione", "coda_sintesi"):
                cand.append((j.get("creato", ""), jd, j))
    if not cand:
        return None, None
    cand.sort(key=lambda x: x[0])
    return cand[0][1], cand[0][2]


def _apply_thread_caps(j: dict) -> None:
    dub_pipeline.WHISPER_THREADS = int(j.get("whisper_threads", 4) or 4)
    dub_pipeline.TTS_THREADS = int(j.get("tts_threads", 2) or 2)


def _do_transcribe(jd: Path, j: dict) -> None:
    _update(jd, stato="trascrizione", progress=10)
    _log(jd, f"trascrizione avviata (whisper {j.get('whisper_model', 'small')}, "
             f"thread {j.get('whisper_threads', 4)})")
    _apply_thread_caps(j)
    awav = jd / "audio16k.wav"
    dub_pipeline.extract_audio(str(jd / j["video"]), str(awav))
    segs = dub_pipeline.transcribe(
        str(awav), model_size=j.get("whisper_model", "small"),
        log=lambda m: _log(jd, m), language=j.get("src", "it"))
    _write(jd / "segments.json",
           [{"idx": s.idx, "start": s.start, "end": s.end,
             "text": s.text, "tr": {}} for s in segs])
    _update(jd, stato="tradurre", progress=30,
            n_segmenti=len(segs))
    _log(jd, f"trascrizione completata: {len(segs)} segmenti → traduzione (app)")


def _do_synth(jd: Path, j: dict) -> None:
    _update(jd, stato="sintesi", progress=55)
    _apply_thread_caps(j)
    ref = jd / "voice_ref.wav"
    if not ref.is_file():
        raise RuntimeError("profilo voce assente nel job (voice_ref.wav): "
                           "la modalità doppiaggio richiede la voce registrata")
    raw = _read(jd / "segments.json") or []
    if not raw:
        raise RuntimeError("segmenti assenti")
    dst = j["dst"]
    segs = [dub_pipeline.Segment(d["idx"], d["start"], d["end"], d["text"],
                                 tr=dict(d.get("tr") or {}))
            for d in raw]
    total = dub_pipeline.ffprobe_duration(str(jd / j["video"]))
    import tempfile
    tmpdir = tempfile.mkdtemp(prefix="dubw_")
    _log(jd, f"sintesi avviata (Chatterbox CPU, thread {j.get('tts_threads', 2)}): "
             f"{len(segs)} segmenti — su CPU richiede tempo, avanzamento nel log")
    n = len(segs)
    for i, s in enumerate(segs):
        out = os.path.join(tmpdir, f"seg_{dst}_{s.idx}.wav")
        txt = s.tr.get(dst, s.text)
        dub_pipeline._synth_long(dub_pipeline.synth_segment, txt, dst,
                                 str(ref), out, "cpu",
                                 lambda m: _log(jd, m))
        s.gen[dst] = out
        _update(jd, progress=55 + int(35 * (i + 1) / n))
        _log(jd, f"  sintesi {i + 1}/{n}")
    _log(jd, "assemblaggio e remux…")
    dub = dub_pipeline.assemble(segs, dst, total, tmpdir,
                                log=lambda m: _log(jd, m))
    stem = os.path.splitext(j["nome_video"])[0]
    mp4 = jd / f"{stem}_{dst.upper()}_dub.mp4"
    srt = jd / f"{stem}_{dst.upper()}.srt"
    dub_pipeline.remux(str(jd / j["video"]), dub, str(mp4),
                       comment=f"ISEO DubStudio {dub_pipeline.PIPE_VERSION}")
    dub_pipeline.write_srt(segs, dst, str(srt))
    _update(jd, stato="pronto", progress=100, output=mp4.name, srt=srt.name)
    _log(jd, f"pronto: {mp4.name}")


def main() -> None:
    print(f"[dub-worker] avvio · pipeline {dub_pipeline.PIPE_VERSION} · "
          f"data={DATA_DIR}", flush=True)
    while True:
        _heartbeat("in attesa")
        jd, j = _next_job()
        if not jd:
            time.sleep(POLL_S)
            continue
        stato = j.get("stato")
        _heartbeat(f"lavoro job {j.get('id')} ({stato})")
        print(f"[dub-worker] job {j.get('id')} · {stato}", flush=True)
        try:
            if stato == "coda_trascrizione":
                _do_transcribe(jd, j)
            elif stato == "coda_sintesi":
                _do_synth(jd, j)
        except Exception as e:
            _update(jd, stato="errore",
                    errore=f"{type(e).__name__}: {e}")
            _log(jd, "ERRORE worker:\n" + traceback.format_exc()[-800:])
            print(f"[dub-worker] ERRORE job {j.get('id')}: {e}", flush=True)


if __name__ == "__main__":
    main()

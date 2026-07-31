"""
dub_pipeline.py — core del doppiaggio (porting server-side di ISEO Dub Studio).
Identico al desktop salvo: tetti di thread configurabili (VM condivisa),
burn-in sottotitoli per la modalità B, device TTS forzabile. Nessuna GUI.

Fasi: estrazione audio -> trascrizione (faster-whisper) -> traduzione (translate.py)
      -> sintesi con voce clonata (Chatterbox) -> time-fit -> assemblaggio -> remux.
Le fasi "pesanti" (trascrizione, sintesi) sono iniettabili per i test.
"""
import os
import re
import shutil
import subprocess
import tempfile
import wave
from dataclasses import dataclass, field

import numpy as np

PIPE_VERSION = "2026-07-29 iseopilot"
WHISPER_THREADS = 4   # tetto CPU trascrizione (admin: dub_whisper_threads)
TTS_THREADS = 2       # tetto CPU sintesi (admin: dub_tts_threads)
SR = 24000            # rate della traccia doppiata (output Chatterbox)
MAX_STRETCH = 1.30    # accelerazione massima per rientrare nei tempi
LANG_NAMES = {"en": "inglese", "es": "spagnolo", "fr": "francese", "de": "tedesco"}


def _which(name):
    """Trova un binario anche col PATH minimale delle app GUI macOS."""
    p = shutil.which(name)
    if p:
        return p
    for c in (f"/opt/homebrew/bin/{name}", f"/usr/local/bin/{name}",
              f"/opt/local/bin/{name}"):
        if os.path.exists(c):
            return c
    return None


def _ffmpeg_path():
    p = _which("ffmpeg")
    if p:
        return p
    try:  # binario statico nel bundle (pip install imageio-ffmpeg)
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


FFMPEG = _ffmpeg_path()
FFPROBE = _which("ffprobe")


def _ff():
    if not FFMPEG:
        raise RuntimeError(
            "ffmpeg non trovato: 'brew install ffmpeg' (cerco anche in "
            "/opt/homebrew/bin, /usr/local/bin e nel pacchetto imageio-ffmpeg).")
    return FFMPEG


# ----------------------------------------------------------------------- utils
def _run(cmd):
    try:
        r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except FileNotFoundError:
        raise RuntimeError(f"binario '{cmd[0]}' non trovato (PATH dell'app GUI)")
    if r.returncode != 0:
        raise RuntimeError(f"{cmd[0]} fallito: {r.stderr.decode(errors='ignore')[-400:]}")


def ffprobe_duration(path):
    if FFPROBE:
        r = subprocess.run([FFPROBE, "-v", "error", "-show_entries", "format=duration",
                            "-of", "default=nw=1:nk=1", path],
                           capture_output=True, text=True)
        try:
            return float(r.stdout.strip())
        except ValueError:
            pass
    # fallback senza ffprobe: parse dell'output di ffmpeg
    r = subprocess.run([_ff(), "-i", path], capture_output=True, text=True)
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", r.stderr)
    if not m:
        raise RuntimeError(f"durata non determinabile per {path}")
    return float(m.group(1)) * 3600 + float(m.group(2)) * 60 + float(m.group(3))


def load_wav(path):
    with wave.open(path, "rb") as w:
        a = np.frombuffer(w.readframes(w.getnframes()), np.int16).astype(np.float32) / 32768.0
        return a, w.getframerate()


def save_wav(path, a, rate=SR):
    with wave.open(path, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(int(rate))
        w.writeframes((np.clip(a, -1, 1) * 32767).astype(np.int16).tobytes())


def extract_audio(video, wav_out, rate=16000):
    _run([_ff(), "-y", "-v", "error", "-i", video, "-vn", "-ac", "1",
          "-ar", str(rate), wav_out])


def to_sr(src, rate=SR):
    """Riporta un wav qualsiasi a mono/`rate`; ritorna array float32."""
    a, r = load_wav(src)
    if r == rate:
        return a
    tmp = src + ".rs.wav"
    _run([_ff(), "-y", "-v", "error", "-i", src, "-ac", "1", "-ar", str(rate), tmp])
    a, _ = load_wav(tmp); os.remove(tmp)
    return a


def _rnnoise_model():
    try:
        from .theme import asset_path
        p = asset_path("rnnoise_voice.rnnn")
        return p if os.path.isfile(p) else None
    except Exception:
        return None


def denoise_wav(src, dst, strength=12):
    """Riduzione rumore di fondo: RNNoise (neurale) se il modello è presente,
    altrimenti afftdn. La voce resta intatta, il floor scende di ~20 dB."""
    m = _rnnoise_model()
    af = (f"highpass=f=60,arnndn=m='{m}'" if m
          else f"highpass=f=60,afftdn=nr={int(strength)}:nf=-40:tn=1")
    tmp = dst + ".dn.wav"
    _run([_ff(), "-y", "-v", "error", "-i", src, "-af", af, tmp])
    os.replace(tmp, dst)


def atempo(src, dst, factor):
    _run([_ff(), "-y", "-v", "error", "-i", src,
          "-filter:a", f"atempo={factor:.4f}", dst])


# -------------------------------------------------------------------- segmenti
@dataclass
class Segment:
    idx: int
    start: float
    end: float
    text: str                      # italiano (trascrizione)
    tr: dict = field(default_factory=dict)    # lang -> testo tradotto
    gen: dict = field(default_factory=dict)   # lang -> wav sintetizzato


# ---------------------------------------------------------------- trascrizione
_SENT_END = tuple(".!?…")

def char_budgets(segments, cps=15.0, tail=3.0, floor=25):
    """Budget di caratteri per segmento dal tempo davvero disponibile (slot)."""
    out = []
    for i, s in enumerate(segments):
        nxt = segments[i + 1].start if i + 1 < len(segments) else s.end + tail
        out.append(max(floor, int((nxt - s.start) * cps)))
    return out


def sanitize_segments(segs, log=print):
    """Catena anti-degenerazione della trascrizione: clamp dei timestamp
    sovrapposti (crosstalk), scarto dei segmenti a durata ~zero, collasso
    dei loop di ripetizione allucinati (finestra sugli ultimi 2)."""
    clean = []
    for s in segs:
        if clean and s.start < clean[-1].end:   # crosstalk: timestamp sovrapposti
            s.start = clean[-1].end
            if s.end - s.start < 0.2:
                continue
        clean.append(s)
    if len(clean) < len(segs):
        log(f"  timestamp sovrapposti sanificati: {len(segs) - len(clean)} scarti")
    dedup, dropped = [], 0
    def _norm(x):
        return re.sub(r"[^\w]+", " ", x.lower()).strip()
    for s in clean:
        if s.end - s.start < 0.15:            # segmenti a durata ~zero
            dropped += 1
            continue
        if dedup and _norm(s.text) and \
                _norm(s.text) in (_norm(p.text) for p in dedup[-2:]):
            dedup[-1].end = max(dedup[-1].end, s.end)   # loop di ripetizione
            dropped += 1
            continue
        dedup.append(s)
    if dropped:
        log(f"  allucinazioni/ripetizioni Whisper rimosse: {dropped}")
    return dedup


def merge_sentences(segs, max_dur=14.0, min_chars=20):
    """Fonde i segmenti Whisper fino a fine frase: prosodia e traduzioni migliori.
    Le micro-frasi ("Okay.", "Sì.") vengono assorbite nel vicino: da sole
    mandano in crash la sintesi (bug Chatterbox sui testi <=5 token)."""
    out = []
    for s in segs:
        if out and ((not out[-1].text.rstrip().endswith(_SENT_END)
                     or len(s.text.strip()) < min_chars
                     or len(out[-1].text.strip()) < min_chars)
                    and (s.end - out[-1].start) <= max_dur):
            p = out[-1]
            p.text = (p.text.rstrip() + " " + s.text.lstrip()).strip()
            p.end = s.end
        else:
            out.append(s)
    for i, s in enumerate(out):
        s.idx = i
    return out


def transcribe(audio_wav, model_size="small", log=print, language="it"):
    """Trascrizione italiana con timestamp (faster-whisper, CPU int8)."""
    from faster_whisper import WhisperModel
    log(f"Carico Whisper '{model_size}' (primo avvio: download modello)...")
    model = WhisperModel(model_size, device="cpu", compute_type="int8",
                         cpu_threads=max(1, int(WHISPER_THREADS)))
    try:  # sanity check: la lingua reale contraddice il selettore Sorgente?
        det_wav = audio_wav + ".det.wav"
        _run([_ff(), "-y", "-v", "error", "-i", audio_wav, "-t", "30", det_wav])
        _, det = model.transcribe(det_wav, language=None, vad_filter=True)
        os.remove(det_wav)
        if det.language and det.language != language \
                and (det.language_probability or 0) > 0.7:
            log(f"⚠️ ATTENZIONE: nel video rilevo '{det.language}' "
                f"(p={det.language_probability:.2f}) ma la Sorgente impostata è "
                f"'{language}'. Controlla il selettore Sorgente!")
    except Exception:
        pass
    raw, info = model.transcribe(audio_wav, language=language, vad_filter=True,
                                 condition_on_previous_text=False)
    segs = []
    for i, s in enumerate(raw):
        t = s.text.strip()
        if t:
            segs.append(Segment(len(segs), float(s.start), float(s.end), t))
        log(f"  [{s.start:6.1f}-{s.end:6.1f}] {t}")
    segs = merge_sentences(sanitize_segments(segs, log))
    log(f"Trascrizione completata: {len(segs)} frasi, {info.duration:.1f}s")
    return segs


# --------------------------------------------------------------------- sintesi
_CB_MODEL = None

def _chatterbox(device=None, log=print):
    global _CB_MODEL
    if _CB_MODEL is not None:
        return _CB_MODEL
    # pre-check: perth (watermarker anti-deepfake di Chatterbox) si carica via
    # pkg_resources; nei venv recenti senza setuptools degrada a None.
    try:
        import perth
        if getattr(perth, "PerthImplicitWatermarker", None) is None:
            raise ImportError("perth.PerthImplicitWatermarker = None "
                              "(pkg_resources/setuptools assente)")
    except Exception as e:
        raise RuntimeError(
            f"Watermarker 'perth' di Chatterbox non disponibile ({e}). "
            'Fix nel venv:  pip install "setuptools<81"  poi riavvia l\'app.')
    import torch
    torch.set_num_threads(max(1, int(TTS_THREADS)))   # VM condivisa: mai affamarla
    if device is None:
        if torch.cuda.is_available():
            device = "cuda"
        elif getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
            device = "mps"
        else:
            device = "cpu"
    if device == "mps":  # checkpoint salvati per CUDA: remap
        _orig = torch.load
        def _load(*a, **k):
            k.setdefault("map_location", torch.device("mps"))
            return _orig(*a, **k)
        torch.load = _load
    log(f"Carico Chatterbox Multilingual su {device}...")
    from chatterbox.mtl_tts import ChatterboxMultilingualTTS
    _CB_MODEL = ChatterboxMultilingualTTS.from_pretrained(device=device)
    return _CB_MODEL


def synth_segment(text, lang, ref_wav, out_wav, device=None, log=print):
    """Sintetizza `text` in `lang` con la voce di `ref_wav` -> out_wav (SR nativo)."""
    model = _chatterbox(device, log)
    w = model.generate(text, language_id=lang, audio_prompt_path=ref_wav)
    a = w.squeeze().detach().cpu().numpy().astype(np.float32)
    save_wav(out_wav, a, model.sr)


# ---------------------------------------------------- time-fit + assemblaggio
def assemble(segments, lang, total_dur, tmpdir, log=print,
             voice_rms=0.055, room_tone_db=-57.0):
    """Colloca i segmenti nei tempi originali; livella la voce, aggiunge room
    tone continuo (niente 'vuoto digitale' nei gap) e ritorna il wav finale."""
    n = int(total_dur * SR)
    track = np.zeros(n, np.float32)
    prev_end = 0
    max_late = 0.0
    for i, s in enumerate(segments):
        src = s.gen.get(lang)
        if not src:
            continue
        avail = (segments[i + 1].start if i + 1 < len(segments) else total_dur) - s.start
        a = to_sr(src)
        dur = len(a) / SR
        if dur > avail * 1.02 and avail > 0.2:
            f = min(dur / avail, MAX_STRETCH)
            dst = os.path.join(tmpdir, f"fit_{lang}_{s.idx}.wav")
            atempo(src, dst, f)
            a = to_sr(dst)
            log(f"  seg {s.idx:02d}: {dur:.2f}s in {avail:.2f}s -> atempo x{f:.2f}"
                + ("  (ancora lungo, sborda)" if len(a) / SR > avail * 1.05 else ""))
        # livello uniforme tra segmenti generati separatamente
        r = float(np.sqrt((a ** 2).mean())) if len(a) else 0.0
        if r > 1e-4:
            a = a * float(np.clip(voice_rms / r, 0.5, 2.0))
        # fade morbidi (40 ms) verso il room tone
        k = min(int(0.04 * SR), len(a) // 3)
        if k > 0:
            w = 0.5 - 0.5 * np.cos(np.linspace(0, np.pi, k, dtype=np.float32))
            a[:k] *= w
            a[-k:] *= w[::-1]
        off = int(s.start * SR)
        min_off = prev_end + int(0.35 * SR)
        if min_off > off:                 # niente sovrapposizioni: recupero
            late = (min_off - off) / SR
            max_late = max(max_late, late)
            if late > 1.5:                # recupero attivo: comprimo questo seg
                tmpw = os.path.join(tmpdir, f"rec_{lang}_{s.idx}.wav")
                save_wav(tmpw, a, SR)
                atempo(tmpw, tmpw + ".x.wav", 1.12)
                a = to_sr(tmpw + ".x.wav")
                log(f"  seg {s.idx:02d}: ritardo {late:.1f}s -> recupero atempo x1.12")
            elif late > 0.7:
                log(f"  seg {s.idx:02d}: parte {late:.1f}s in ritardo "
                    "(coda della frase precedente)")
            off = min_off
        end = min(n, off + len(a))
        track[off:end] += a[:end - off]
        prev_end = end
    if max_late:
        log(f"  [{lang}] ritardo massimo accumulato: {max_late:.1f}s "
            "(si riassorbe alle pause)")
    if room_tone_db is not None:
        noise = np.random.default_rng(7).standard_normal(n).astype(np.float32)
        noise = np.convolve(noise, np.ones(6, np.float32) / 6, mode="same")
        noise *= (10 ** (room_tone_db / 20)) / (float(np.sqrt((noise ** 2).mean())) + 1e-12)
        track += noise
    peak = float(np.abs(track).max()) or 1.0
    track *= min(1.0, 0.92 / peak)
    out = os.path.join(tmpdir, f"dub_{lang}.wav")
    save_wav(out, track, SR)
    return out


def remux(video, dubbed_wav, out_mp4, comment=None):
    meta = ["-metadata", f"comment={comment}"] if comment else []
    _run([_ff(), "-y", "-v", "error", "-i", video, "-i", dubbed_wav,
          *meta,
          "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy",
          "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart",
          "-shortest", out_mp4])


def burn_subtitles(video, srt_path, out_mp4, log=print):
    """Modalità B: video con AUDIO ORIGINALE e sottotitoli impressi (burn-in).
    Re-encoding video necessario (i sottotitoli entrano nei fotogrammi):
    preset veloce, qualità visivamente trasparente, audio copiato."""
    # il filtro subtitles vuole il percorso con ':' e '\'' escapati
    esc = srt_path.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")
    style = "FontName=DejaVu Sans,FontSize=20,Outline=1,Shadow=0,MarginV=28"
    _run([_ff(), "-y", "-v", "error", "-i", video,
          "-vf", f"subtitles='{esc}':force_style='{style}'",
          "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
          "-c:a", "copy", "-movflags", "+faststart", out_mp4])
    log(f"  sottotitoli impressi -> {out_mp4}")


def write_srt(segments, lang, path):
    def ts(t):
        h, r = divmod(t, 3600); m, s = divmod(r, 60)
        return f"{int(h):02d}:{int(m):02d}:{int(s):02d},{int((s % 1) * 1000):03d}"
    with open(path, "w", encoding="utf-8") as f:
        for i, s in enumerate(segments, 1):
            txt = s.tr.get(lang, s.text)
            f.write(f"{i}\n{ts(s.start)} --> {ts(s.end)}\n{txt}\n\n")


# ---------------------------------------------------- controlli anti-buchi
def _speech_regions(a, rate, min_sil=0.45, min_seg=0.35):
    hop = int(rate * 0.02)
    n = len(a) // hop
    db = 20 * np.log10(np.sqrt((a[:n * hop].reshape(n, hop) ** 2).mean(1)) + 1e-9)
    thr = float(np.clip(max(np.percentile(db, 10) + 10,
                            np.percentile(db, 95) - 28), -58, -22))
    sp = db > thr
    regs, i = [], 0
    while i < n:
        if sp[i]:
            j = i
            while j < n and sp[j]:
                j += 1
            regs.append([i * 0.02, j * 0.02])
            i = j
        else:
            i += 1
    out = []
    for r in regs:
        if out and r[0] - out[-1][1] < min_sil:
            out[-1][1] = r[1]
        else:
            out.append(list(r))
    return [tuple(r) for r in out if r[1] - r[0] >= min_seg]


def speech_coverage(audio_wav, segments, min_hole=1.2):
    """Tratti di parlato dell'originale NON coperti dai segmenti trascritti."""
    a, r = load_wav(audio_wav)
    holes = []
    for s, e in _speech_regions(a, r):
        cov = sum(max(0.0, min(e, g.end) - max(s, g.start)) for g in segments)
        if (e - s) >= min_hole and cov / (e - s) < 0.5:
            holes.append((round(s, 1), round(e, 1)))
    return holes


def _synth_checked(synth, text, lang, ref, out, device, log, retries=1):
    """Sintetizza e verifica la durata attesa: anti-troncamento (forced EOS)."""
    exp = max(1.0, len(text) / 15.0)
    dur = rms = 0.0
    if not text.strip():
        save_wav(out, np.zeros(int(0.4 * SR), np.float32), SR)
        return 0.4
    best = None                                   # (rms, dur) del tentativo migliore
    for attempt in range(retries + 1):
        try:
            synth(text, lang, ref, out, device=device, log=log)
        except Exception as e:
            if len(text.strip()) < 25:
                log(f"  ⚠️ micro-testo non sintetizzabile ({e.__class__.__name__}): "
                    f"\"{text.strip()}\" -> inserisco una pausa e proseguo")
                save_wav(out, np.zeros(int(0.4 * SR), np.float32), SR)
                return 0.4
            raise
        a, r = load_wav(out)
        dur = len(a) / r
        rms = float(np.sqrt((a ** 2).mean())) if len(a) else 0.0
        if dur >= 0.55 * exp and rms >= 0.015:    # durata E livello da parlato vero
            return dur
        if best is None or rms > best[0]:
            shutil.copyfile(out, out + ".best")
            best = (rms, dur)
        if attempt < retries:
            why = "quasi muta" if rms < 0.015 else "corta"
            log(f"  ⚠️ sintesi {why} ({dur:.1f}s, RMS {rms:.3f}): ritento...")
    if best is not None:
        shutil.copyfile(out + ".best", out)
        os.remove(out + ".best")
        rms, dur = best
    log(f"  ⚠️ segmento degradato dopo i tentativi (RMS {rms:.3f}): "
        f"\"{text[:60]}...\"")
    return dur


_SENT_SPLIT = re.compile(r"(?<=[.!?…])\s+")

def _synth_long(synth, text, lang, ref, out, device, log, max_chars=220):
    """Chatterbox tronca i testi lunghi: spezza in gruppi di frasi <= max_chars."""
    parts = [p.strip() for p in _SENT_SPLIT.split(text) if p.strip()]
    if len(text) <= max_chars or len(parts) <= 1:
        return _synth_checked(synth, text, lang, ref, out, device, log)
    groups, cur = [], ""
    for p in parts:
        if cur and len(cur) + len(p) + 1 > max_chars:
            groups.append(cur)
            cur = p
        else:
            cur = (cur + " " + p).strip()
    if cur:
        groups.append(cur)
    if len(groups) > 1 and len(groups[-1]) < 20:   # coda micro: nel penultimo
        groups[-2] = (groups[-2] + " " + groups[-1]).strip()
        groups.pop()
    waves, rate = [], None
    for gi, g in enumerate(groups):
        pth = f"{out}.part{gi}.wav"
        _synth_checked(synth, g, lang, ref, pth, device, log)
        w, r = load_wav(pth)
        os.remove(pth)
        rate = rate or r
        waves.append(w)
    gap = np.zeros(int(0.18 * rate), np.float32)
    full = []
    for w in waves:
        full.extend([w, gap])
    save_wav(out, np.concatenate(full[:-1]), rate)



# ------------------------------------------------------------- orchestratore
def dub_video(video, langs, ref_wav, outdir, translator,
              transcriber=None, synthesizer=None, segments=None, shortener=None,
              model_size="small", denoise=False, src_lang="it", device=None, log=print, pct=lambda p: None,
              stop=lambda: False):
    """
    Pipeline completa. `translator(texts, lang) -> list[str]`.
    `transcriber`/`synthesizer` iniettabili (test); `segments` già pronti salta
    trascrizione+traduzione (fase B della GUI).
    Ritorna: (segments, {lang: (mp4, srt)}).
    """
    log(f"ISEO Dub Studio pipeline {PIPE_VERSION}")
    os.makedirs(outdir, exist_ok=True)
    tmpdir = tempfile.mkdtemp(prefix="dub_")
    total = ffprobe_duration(video)
    stem = os.path.splitext(os.path.basename(video))[0]

    if segments is None:
        log("1/5 Estrazione audio...")
        awav = os.path.join(tmpdir, "audio16k.wav")
        extract_audio(video, awav)
        if denoise:
            log("   riduzione rumore di fondo (RNNoise)...")
            denoise_wav(awav, awav)
        pct(5)
        if stop(): return None, {}
        log("2/5 Trascrizione (italiano)...")
        if transcriber:
            segments = transcriber(awav, model_size=model_size, log=log)
        else:
            segments = transcribe(awav, model_size=model_size, log=log,
                                  language=src_lang)
        pct(30)
        if stop(): return segments, {}
        log("3/5 Traduzione...")
        texts = [s.text for s in segments]
        budgets = char_budgets(segments)
        for lang in langs:
            log(f"  -> {LANG_NAMES.get(lang, lang)}")
            tr = translator(texts, lang, budgets)
            for s, t in zip(segments, tr):
                s.tr[lang] = t
        pct(45)

    if stop(): return segments, {}
    log("4/5 Sintesi con voce clonata...")
    slots = {}
    for i, s in enumerate(segments):
        nxt = segments[i + 1].start if i + 1 < len(segments) else total
        slots[s.idx] = max(0.5, nxt - s.start)
    todo = [(s, lg) for lg in langs for s in segments]
    synth = synthesizer or synth_segment
    for j, (s, lg) in enumerate(todo):
        if stop(): return segments, {}
        out = os.path.join(tmpdir, f"seg_{lg}_{s.idx}.wav")
        txt = s.tr.get(lg, s.text)
        _synth_long(synth, txt, lg, ref_wav, out, device, log)
        a_, r_ = load_wav(out)
        dur, slot = len(a_) / r_, slots[s.idx]
        if shortener and slot > 1.0 and dur > slot * 1.02:
            target = max(30, int(slot * 13))
            log(f"  seg {s.idx:02d} [{lg}]: {dur:.1f}s in {slot:.1f}s "
                f"-> accorcio a ~{target} caratteri e risintetizzo")
            try:
                new = shortener(txt, lg, target)
            except Exception as e:
                new = None
                log(f"     accorciatore non disponibile: {e}")
            if new and len(new) < len(txt):
                s.tr[lg] = new
                _synth_long(synth, new, lg, ref_wav, out, device, log)
                a_, r_ = load_wav(out)
                log(f"     -> {len(a_) / r_:.1f}s dopo il refit")
        s.gen[lg] = out
        pct(45 + int(45 * (j + 1) / len(todo)))
        log(f"  [{lg}] {s.idx + 1}/{len(segments)}")

    log("5/5 Assemblaggio e remux...")
    outputs = {}
    for lg in langs:
        dub = assemble(segments, lg, total, tmpdir, log=log)
        mp4 = os.path.join(outdir, f"{stem}_{lg.upper()}.mp4")
        srt = os.path.join(outdir, f"{stem}_{lg.upper()}.srt")
        remux(video, dub, mp4, comment=f"ISEO DubStudio {PIPE_VERSION}")
        write_srt(segments, lg, srt)
        outputs[lg] = (mp4, srt)
        log(f"  -> {mp4}")
    pct(100)
    return segments, outputs

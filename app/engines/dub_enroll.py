"""
dub_enroll.py — profilo voce per il doppiaggio (porting server-side).
Parte PURA di enroll.py del desktop: analisi qualità (durata, clipping,
livello, SNR), trim silenzi, salvataggio profilo 24 kHz mono con sidecar
JSON di CONSENSO. La registrazione avviene nel browser (MediaRecorder);
qui arriva l'audio già catturato.

Regola non negoziabile: la voce usata nel doppiaggio è SOLO quella che
l'utente registra volontariamente con consenso esplicito — mai estratta
o clonata dall'audio del video sorgente.
"""
import json
import os
import re
import tempfile
from datetime import datetime

import numpy as np

from .dub_pipeline import _ff, _run, denoise_wav, save_wav

REC_RATE = 48000   # rate tipico di cattura browser
OUT_RATE = 24000   # rate del profilo salvato (input Chatterbox)

ENROLL_TEXT = (
    "Buongiorno, questa è la registrazione della mia voce per il sistema di "
    "doppiaggio aziendale.\n"
    "La sicurezza informatica è responsabilità di ciascuno di noi, ogni giorno, "
    "in ogni messaggio che riceviamo.\n"
    "Quando un'email sembra urgente, strana o troppo bella per essere vera, "
    "fermati un attimo e rifletti.\n"
    "Controlla sempre l'indirizzo del mittente, i collegamenti sospetti e gli "
    "allegati inattesi.\n"
    "Hai mai ricevuto una richiesta di pagamento improvvisa da un fornitore "
    "sconosciuto?\n"
    "Nel dubbio, meglio una verifica in più che un incidente in meno!\n"
    "Le password robuste, l'autenticazione a più fattori e gli aggiornamenti "
    "regolari proteggono il nostro lavoro.\n"
    "Ogni segnalazione migliora le nostre difese: basta un piccolo gesto per "
    "evitare un grande danno.\n"
    "Cinque, ventitré, quarantotto, cento: anche i numeri fanno parte della "
    "mia voce.\n"
    "Fermati, verifica, segnala."
)

ENROLL_TEXT_EN = (
    "Good morning, this is the recording of my voice for the company dubbing "
    "system.\n"
    "Information security is everyone's responsibility, every day, in every "
    "message we receive.\n"
    "When an email looks urgent, strange or too good to be true, stop for a "
    "moment and think.\n"
    "Always check the sender's address, suspicious links and unexpected "
    "attachments.\n"
    "Have you ever received a sudden payment request from an unknown supplier?\n"
    "When in doubt, one extra check is better than one more incident!\n"
    "Strong passwords, multi-factor authentication and regular updates protect "
    "our work.\n"
    "Every report improves our defences: a small gesture can prevent great "
    "damage.\n"
    "Five, twenty-three, forty-eight, one hundred: numbers are part of my "
    "voice too.\n"
    "Stop, verify, report."
)


# ------------------------------------------------------------------ analisi
def _frame_db(a, rate, hop_s=0.02):
    hop = max(1, int(rate * hop_s))
    n = len(a) // hop
    if n == 0:
        return np.array([-90.0])
    rms = np.sqrt((a[: n * hop].reshape(n, hop) ** 2).mean(1) + 1e-12)
    return 20 * np.log10(rms)


def analyze(a, rate):
    """Metriche di qualità della registrazione."""
    db = _frame_db(a, rate)
    floor = float(np.percentile(db, 10))
    speech = float(np.percentile(db, 95))
    return {
        "duration": len(a) / rate,
        "peak": float(np.abs(a).max()) if len(a) else 0.0,
        "clip_ratio": float((np.abs(a) > 0.985).mean()) if len(a) else 0.0,
        "floor_db": floor,
        "speech_db": speech,
        "snr_db": speech - floor,
    }


def verdicts(m):
    """(ok_complessivo, [righe di esito])."""
    rows, ok = [], True
    if m["duration"] < 15:
        rows.append("❌ Registrazione troppo corta (<15s): rileggi il testo intero.")
        ok = False
    elif m["duration"] < 45:
        rows.append(f"⚠️ Durata {m['duration']:.0f}s: funziona, ma 60–90s danno un clone migliore.")
    else:
        rows.append(f"✅ Durata {m['duration']:.0f}s.")
    if m["clip_ratio"] > 0.001:
        rows.append("⚠️ Segnale in clipping: abbassa il volume di ingresso o allontanati dal microfono.")
    else:
        rows.append("✅ Nessun clipping.")
    if m["speech_db"] < -30:
        rows.append("⚠️ Voce molto bassa: avvicinati al microfono.")
    else:
        rows.append("✅ Livello voce adeguato.")
    if m["snr_db"] < 15:
        rows.append(f"⚠️ Ambiente rumoroso (SNR {m['snr_db']:.0f} dB): spegni ventole/musica e riprova.")
    else:
        rows.append(f"✅ Rumore di fondo ok (SNR {m['snr_db']:.0f} dB).")
    if m["speech_db"] <= -55:
        rows.append("❌ Non sento parlato: microfono giusto selezionato?")
        ok = False
    return ok, rows


def trim_silence(a, rate, thr_off=12.0, pad=0.15):
    """Toglie silenzio iniziale/finale (soglia relativa al floor)."""
    db = _frame_db(a, rate)
    thr = float(np.percentile(db, 10)) + thr_off
    idx = np.where(db > thr)[0]
    if not len(idx):
        return a
    hop = max(1, int(rate * 0.02))
    s = max(0, int(idx[0] * hop - pad * rate))
    e = min(len(a), int((idx[-1] + 1) * hop + pad * rate))
    return a[s:e]


def to_float_audio(path):
    """Qualsiasi audio caricato (webm/ogg/wav/m4a) -> (float32 mono, REC_RATE)
    via ffmpeg. Errori parlanti se il file non è decodificabile."""
    tmp = tempfile.mktemp(suffix=".wav")
    _run([_ff(), "-y", "-v", "error", "-i", path, "-ac", "1",
          "-ar", str(REC_RATE), tmp])
    import wave
    with wave.open(tmp, "rb") as w:
        a = (np.frombuffer(w.readframes(w.getnframes()), np.int16)
             .astype(np.float32) / 32768.0)
        rate = w.getframerate()
    os.remove(tmp)
    return a, rate


def save_profile(a, rate, voices_dir, name, consent=True, denoise=False):
    """Trim + normalizza + salva profilo 24 kHz mono con sidecar di consenso.
    SENZA consenso esplicito il profilo NON viene salvato."""
    if not consent:
        raise ValueError("Consenso esplicito assente: il profilo voce non viene salvato.")
    a = trim_silence(np.asarray(a, np.float32), rate)
    peak = float(np.abs(a).max()) or 1.0
    a = a * min(1.0, 0.9 / peak)
    safe = re.sub(r"[^\w\- ]+", "", name).strip().replace(" ", "_") or "voce"
    os.makedirs(voices_dir, exist_ok=True)
    out = os.path.join(voices_dir, f"{safe}.wav")
    tmp = tempfile.mktemp(suffix=".wav")
    save_wav(tmp, a, rate)
    if denoise:
        denoise_wav(tmp, tmp)
    _run([_ff(), "-y", "-v", "error", "-i", tmp,
          "-ac", "1", "-ar", str(OUT_RATE), out])
    os.remove(tmp)
    meta = {"name": name, "created": datetime.now().isoformat(timespec="seconds"),
            "consent": True, "duration_s": round(len(a) / rate, 1),
            "text": "enroll_v1"}
    with open(os.path.join(voices_dir, f"{safe}.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=1)
    return out

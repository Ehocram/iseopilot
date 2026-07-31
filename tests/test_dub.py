"""
Test DUB STUDIO (Incremento 9). Tutto OFFLINE: gli stadi ML (Whisper,
Chatterbox) girano nel container worker e qui sono SIMULATI scrivendo i file
di stato che il worker produrrebbe; ffmpeg/ffprobe reali dove servono
(presenti nell'immagine). Il protocollo file-based è quindi testato per come
è: job.json atomici, transizioni di stato, gate di revisione.
Esecuzione:
    PYTHONPATH=. APP_DATA_DIR=./data_test python -m pytest tests/ -q
"""
import io
import json
import os
import wave

import numpy as np

os.environ.setdefault("APP_DATA_DIR", "./data_test")
from cryptography.fernet import Fernet
os.environ.setdefault("APP_SECRET_KEY", Fernet.generate_key().decode())

from fastapi.testclient import TestClient
from app.main import app
from app import store, auth, dubstudio
from app.engines import dub_enroll, dub_pipeline

store.init_db()


def _mk_user(name, dept="IT", is_admin=False):
    store.create_user(name, auth.hash_password("Password123"), dept, is_admin=is_admin)
    c = TestClient(app)
    c.post("/login", data={"username": name, "password": "Password123"},
           follow_redirects=False)
    return c


def _on():
    store.set_setting("dub_enabled", "1")


def _off():
    store.set_setting("dub_enabled", "0")


def _grant(user, si=True):
    store.set_user_setting(user, "dub_access", "1" if si else "0")


def _sine_wav(seconds=60.0, rate=16000, freq=180.0, amp=0.25) -> bytes:
    """Wav sintetico 'parlato-plausibile' per analyze/verdicts (livello ok,
    niente clipping, SNR alto)."""
    t = np.arange(int(seconds * rate)) / rate
    a = (amp * np.sin(2 * np.pi * freq * t)
         * (0.6 + 0.4 * np.sin(2 * np.pi * 2.0 * t))).astype(np.float32)
    bio = io.BytesIO()
    with wave.open(bio, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes((a * 32767).astype(np.int16).tobytes())
    return bio.getvalue()


def _fake_video(username, monkeypatch, c, mode="sottotitoli", src="it", dst="en"):
    """Crea un job con video finto: ffprobe è simulato (60s)."""
    monkeypatch.setattr(dubstudio.dub_pipeline, "ffprobe_duration", lambda p: 60.0)
    r = c.post("/dub/job", data={"src": src, "dst": dst, "mode": mode},
               files={"file": ("demo.mp4", b"\x00\x00\x00 ftypmp42finto", "video/mp4")})
    return r.json()


def _worker_simula_trascrizione(username, jid):
    """Fa quello che farebbe il worker: segments.json + stato 'tradurre'."""
    jd = dubstudio._job_dir(username, jid)
    dubstudio._write_json(jd / "segments.json", [
        {"idx": 0, "start": 0.0, "end": 3.2, "text": "Benvenuti al corso.", "tr": {}},
        {"idx": 1, "start": 3.4, "end": 7.9, "text": "Oggi parliamo di sicurezza.", "tr": {}},
    ])
    dubstudio._job_update(username, jid, stato="tradurre", progress=30)


# ── Gate: kill-switch + grant individuale ───────────────────
def test_gate_spento_niente_superficie():
    _off()
    c = _mk_user("dub_off@test")
    assert "Dub Studio" not in c.get("/").text          # nav assente
    assert c.get("/dub").status_code == 404
    assert c.post("/dub/job/xxx/delete").status_code == 404


def test_gate_acceso_senza_grant_403_parlante():
    _on()
    c = _mk_user("dub_nogrант@test")
    r = c.get("/dub")
    assert r.status_code == 403
    assert "Dub Studio" not in c.get("/").text          # nav solo col grant
    _off()


def test_gate_acceso_con_grant_pagina_ok():
    _on()
    c = _mk_user("dub_ok@test")
    _grant("dub_ok@test")
    assert "Dub Studio" in c.get("/").text              # pulsante in home
    html = c.get("/dub").text
    assert "dub-upload" in html and "rec-start" in html
    _off()


def test_admin_users_ha_spunta_dub_e_audit():
    _on()
    admin = _mk_user("dub_admin@test", is_admin=True)
    _mk_user("dub_target@test")
    html = admin.get("/admin/users").text
    assert 'name="dub_access"' in html
    admin.post("/admin/users/update", data={
        "username": "dub_target@test", "department": "IT",
        "is_admin": "0", "active": "1", "dub_access": "1"},
        follow_redirects=False)
    assert dubstudio.user_allowed("dub_target@test") is True
    azioni = {a["action"] for a in store.audit_query(username="dub_admin@test")}
    assert "dub_grant" in azioni
    _off()


def test_admin_motore_ha_pannello_dub():
    c = _mk_user("dub_adm2@test", is_admin=True)
    html = c.get("/admin").text
    assert 'name="dub_enabled"' in html
    assert 'name="claude_model_dub"' in html
    assert 'name="dub_max_min"' in html


# ── Voce: qualità, consenso, ciclo di vita ──────────────────
def test_voce_verdetti_su_audio_sintetico():
    a = np.frombuffer(_sine_wav(70)[44:], np.int16).astype(np.float32) / 32768.0
    m = dub_enroll.analyze(a, 16000)
    ok, righe = dub_enroll.verdicts(m)
    assert ok is True
    assert any("Durata" in r for r in righe)


def test_voce_corta_bocciata():
    a = np.frombuffer(_sine_wav(5)[44:], np.int16).astype(np.float32) / 32768.0
    ok, righe = dub_enroll.verdicts(dub_enroll.analyze(a, 16000))
    assert ok is False and any("troppo corta" in r for r in righe)


def test_voce_api_consenso_obbligatorio_e_roundtrip():
    _on()
    c = _mk_user("dub_voce@test")
    _grant("dub_voce@test")
    wav = _sine_wav(70)
    r = c.post("/dub/voice", data={"consent": "0"},
               files={"file": ("voce.wav", wav, "audio/wav")}).json()
    assert r["ok"] is False and "consenso" in r["errore"].lower()
    r = c.post("/dub/voice", data={"consent": "1"},
               files={"file": ("voce.wav", wav, "audio/wav")}).json()
    assert r["ok"] is True
    assert dubstudio.voice_status("dub_voce@test")["present"] is True
    assert dubstudio.voice_status("dub_voce@test")["consent"] is True
    r = c.post("/dub/voice/delete").json()
    assert r["ok"] is True and r["status"]["present"] is False
    azioni = {a["action"] for a in store.audit_query(username="dub_voce@test")}
    assert {"dub_voce_salvata", "dub_voce_eliminata"} <= azioni
    _off()


# ── Job: limiti parlanti e stati ────────────────────────────
def test_job_limiti_parlanti(monkeypatch):
    _on()
    c = _mk_user("dub_lim@test")
    _grant("dub_lim@test")
    store.set_setting("dub_max_mb", "1")
    r = c.post("/dub/job", data={"src": "it", "dst": "en", "mode": "sottotitoli"},
               files={"file": ("big.mp4", b"x" * (2 * 1024 * 1024), "video/mp4")}).json()
    assert r["ok"] is False and "limite è 1 MB" in r["errore"]
    store.set_setting("dub_max_mb", "300")
    store.set_setting("dub_max_min", "1")
    monkeypatch.setattr(dubstudio.dub_pipeline, "ffprobe_duration", lambda p: 300.0)
    r = c.post("/dub/job", data={"src": "it", "dst": "en", "mode": "sottotitoli"},
               files={"file": ("long.mp4", b"finto", "video/mp4")}).json()
    assert r["ok"] is False and "minuti" in r["errore"]
    store.set_setting("dub_max_min", "20")
    r = c.post("/dub/job", data={"src": "it", "dst": "it", "mode": "sottotitoli"},
               files={"file": ("x.mp4", b"finto", "video/mp4")}).json()
    assert r["ok"] is False and "diverse" in r["errore"]
    _off()


def test_job_doppiaggio_richiede_profilo_voce(monkeypatch):
    _on()
    c = _mk_user("dub_novoce@test")
    _grant("dub_novoce@test")
    r = _fake_video("dub_novoce@test", monkeypatch, c, mode="doppiaggio")
    assert r["ok"] is False and "voce" in r["errore"].lower()
    _off()


def test_flusso_sottotitoli_end_to_end(monkeypatch):
    """caricato → [worker simulato] → tradurre → traduzione (mock Claude) →
    revisione → conferma → montaggio (burn mock) → pronto + download."""
    _on()
    u = "dub_e2e@test"
    c = _mk_user(u)
    _grant(u)
    r = _fake_video(u, monkeypatch, c)
    assert r["ok"] is True
    jid = r["jid"]
    assert dubstudio.job_read(u, jid)["stato"] == "coda_trascrizione"

    _worker_simula_trascrizione(u, jid)
    store.set_setting("claude_api_key", "chiave-finta")
    monkeypatch.setattr(dubstudio.dub_translate, "translate_claude",
                        lambda testi, lang, key, model="", src="it", limits=None:
                        [f"[{lang}] {t}" for t in testi])
    assert dubstudio._runner_tick() >= 1                 # traduzione
    j = dubstudio.job_read(u, jid)
    assert j["stato"] == "revisione"

    rev = c.get(f"/dub/job/{jid}/review").json()
    assert len(rev["segments"]) == 2
    assert rev["segments"][0]["tr"]["en"].startswith("[en]")
    assert len(rev["budgets"]) == 2 and rev["budgets"][0] > 0

    r = c.post(f"/dub/job/{jid}/review",
               json={"testi": ["Welcome to the course.", "Today we talk about security."]}).json()
    assert r["ok"] is True
    srt = c.get(f"/dub/job/{jid}/srt").text
    assert "00:00:00,000 --> 00:00:03,200" in srt
    assert "Welcome to the course." in srt

    def fake_burn(video, srt_path, out_mp4, log=print):
        open(out_mp4, "wb").write(b"MP4FINTO")
    monkeypatch.setattr(dubstudio.dub_pipeline, "burn_subtitles", fake_burn)
    r = c.post(f"/dub/job/{jid}/continue").json()
    assert r["ok"] is True and r["stato"] == "coda_sottotitoli"
    assert dubstudio._runner_tick() >= 1                 # burn-in
    j = dubstudio.job_read(u, jid)
    assert j["stato"] == "pronto" and j["output"].endswith("_sub.mp4")
    r = c.get(f"/dub/job/{jid}/download/video")
    assert r.status_code == 200 and r.content == b"MP4FINTO"
    assert c.post(f"/dub/job/{jid}/delete").json()["ok"] is True
    _off()


def test_flusso_doppiaggio_va_in_coda_sintesi(monkeypatch):
    _on()
    u = "dub_a@test"
    c = _mk_user(u)
    _grant(u)
    wav = _sine_wav(70)
    assert c.post("/dub/voice", data={"consent": "1"},
                  files={"file": ("voce.wav", wav, "audio/wav")}).json()["ok"]
    r = _fake_video(u, monkeypatch, c, mode="doppiaggio")
    assert r["ok"] is True
    jid = r["jid"]
    assert (dubstudio._job_dir(u, jid) / "voice_ref.wav").is_file()  # voce congelata nel job
    _worker_simula_trascrizione(u, jid)
    monkeypatch.setattr(dubstudio.dub_translate, "translate_claude",
                        lambda testi, lang, key, model="", src="it", limits=None: list(testi))
    dubstudio._runner_tick()
    r = c.post(f"/dub/job/{jid}/continue").json()
    assert r["ok"] is True and r["stato"] == "coda_sintesi"          # tocca al worker
    c.post(f"/dub/job/{jid}/delete")
    c.post("/dub/voice/delete")
    _off()


def test_continue_rifiutato_fuori_revisione(monkeypatch):
    _on()
    u = "dub_gatekeeper@test"
    c = _mk_user(u)
    _grant(u)
    jid = _fake_video(u, monkeypatch, c)["jid"]
    r = c.post(f"/dub/job/{jid}/continue").json()
    assert r["ok"] is False and "coda_trascrizione" in r["errore"]   # il gate umano regge
    _off()


def test_errore_traduzione_parlante(monkeypatch):
    _on()
    u = "dub_err@test"
    c = _mk_user(u)
    _grant(u)
    jid = _fake_video(u, monkeypatch, c)["jid"]
    _worker_simula_trascrizione(u, jid)
    store.set_setting("claude_api_key", "")              # chiave assente
    dubstudio._runner_tick()
    j = dubstudio.job_read(u, jid)
    assert j["stato"] == "errore" and "chiave Claude" in j["errore"]
    store.set_setting("claude_api_key", "chiave-finta")
    _off()


def test_worker_heartbeat_stantio():
    dubstudio._write_json(dubstudio.HEARTBEAT, {"ts": 0})
    assert dubstudio.worker_alive() is False
    import time
    dubstudio._write_json(dubstudio.HEARTBEAT, {"ts": time.time()})
    assert dubstudio.worker_alive() is True


def test_burn_subtitles_ffmpeg_reale(tmp_path):
    """Smoke REALE del burn-in con ffmpeg di sistema: video sintetico 2s."""
    import subprocess
    video = tmp_path / "in.mp4"
    subprocess.run([dub_pipeline._ff(), "-y", "-v", "error",
                    "-f", "lavfi", "-i", "color=c=black:s=320x240:d=2",
                    "-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono",
                    "-t", "2", "-c:v", "libx264", "-c:a", "aac", str(video)],
                   check=True)
    srt = tmp_path / "s.srt"
    srt.write_text("1\n00:00:00,000 --> 00:00:02,000\nCiao mondo\n\n",
                   encoding="utf-8")
    out = tmp_path / "out.mp4"
    dub_pipeline.burn_subtitles(str(video), str(srt), str(out), log=lambda m: None)
    assert out.is_file() and out.stat().st_size > 1000


if __name__ == "__main__":
    import inspect
    import sys
    import tempfile
    from pathlib import Path

    class _MP:
        def setattr(self, obj, name, value):
            setattr(obj, name, value)

    fns = [f for n, f in sorted(globals().items())
           if n.startswith("test_") and inspect.isfunction(f)]
    failed = 0
    for f in fns:
        try:
            kwargs = {}
            sig = inspect.signature(f)
            td = None
            if "tmp_path" in sig.parameters:
                td = tempfile.TemporaryDirectory()
                kwargs["tmp_path"] = Path(td.name)
            if "monkeypatch" in sig.parameters:
                kwargs["monkeypatch"] = _MP()
            f(**kwargs)
            print(f"  PASS  {f.__name__}")
            if td:
                td.cleanup()
        except Exception as e:
            failed += 1
            print(f"  FAIL  {f.__name__}: {e}")
    print(f"\n{len(fns)-failed}/{len(fns)} test superati.")
    sys.exit(1 if failed else 0)

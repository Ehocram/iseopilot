#!/usr/bin/env python3
"""Agente di deploy di ISEOPilot. Gira SULL'HOST, non nel container.

Perche' esiste: l'applicazione non deve poter modificare se stessa ne' avere
accesso al socket Docker. Qui c'e' l'unico punto che scrive sul repository e
tocca i container — e tocca SOLO i propri.

AMBITO, cablato e non configurabile dalla richiesta:
  repository : /opt/iseopilot
  servizio   : iseopilot   (dentro docker-compose.prod.yml di quel repository)
Sull'host girano anche Flusso-AI e iseotraining: nessun parametro in arrivo
puo' spostare l'agente su un altro percorso o un altro servizio, perche' nessun
comando qui costruisce il proprio bersaglio dai dati ricevuti.

Solo su 127.0.0.1, con token condiviso.
"""
from __future__ import annotations

import json
import os
import socketserver
import subprocess
import sys
import time
import urllib.request
from http.server import BaseHTTPRequestHandler
from pathlib import Path

# ── Ambito: costanti, non parametri ────────────────────────────────────────
REPO = Path("/opt/iseopilot")
COMPOSE = "docker-compose.prod.yml"
SERVIZIO = "iseopilot"
IMMAGINE = "iseopilot:latest"
IMMAGINE_PREC = "iseopilot:precedente"
SALUTE_URL = "http://127.0.0.1:8000/healthz"

# Socket unix invece di una porta: l'applicazione gira in un container, dove
# 127.0.0.1 e' il container stesso e NON l'host. Una porta andrebbe quindi
# esposta su un'interfaccia raggiungibile dai container — cioe' anche dalla
# rete aziendale. Il socket attraversa il confine come un file montato: nessuna
# porta aperta da nessuna parte, e i permessi fanno da controllo d'accesso.
SOCKET = Path(os.environ.get("DEPLOY_AGENT_SOCKET",
                             "/run/iseopilot/deploy-agent.sock"))
SOCKET_GID = int(os.environ.get("DEPLOY_AGENT_GID", "10001"))   # appuser nel container
TOKEN_FILE = Path(os.environ.get("DEPLOY_AGENT_TOKEN_FILE",
                                 "/etc/iseopilot/deploy-agent.token"))
ATTESA_SALUTE = int(os.environ.get("DEPLOY_AGENT_HEALTH_WAIT", "90"))
LOG = Path(os.environ.get("DEPLOY_AGENT_LOG", "/var/log/iseopilot-deploy.log"))


def _log(msg: str) -> None:
    riga = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(riga, flush=True)
    try:
        with LOG.open("a", encoding="utf-8") as fh:
            fh.write(riga + "\n")
    except OSError:
        pass


def _esegui(argv: list[str], inp: str | None = None, timeout: int = 900) -> tuple[int, str]:
    """Esegue un comando con argomenti SEMPRE espliciti: mai una shell, mai
    una stringa composta con dati in arrivo."""
    r = subprocess.run(argv, cwd=str(REPO), input=inp, text=True,
                       capture_output=True, timeout=timeout)
    return r.returncode, ((r.stdout or "") + (r.stderr or "")).strip()


def _in_salute() -> bool:
    scadenza = time.time() + ATTESA_SALUTE
    while time.time() < scadenza:
        try:
            with urllib.request.urlopen(SALUTE_URL, timeout=5) as r:
                if r.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(3)
    return False


def pubblica(patch: str, riassunto: str, autore: str) -> dict:
    """Applica, committa, pubblica e verifica. Se la verifica fallisce, torna
    indietro da sola: chi ha premuto il pulsante non deve trovarsi con
    l'applicazione giu' e nessuno strumento per rimediare."""
    passi = []

    def step(nome, argv, **kw):
        rc, out = _esegui(argv, **kw)
        passi.append({"passo": nome, "esito": rc, "output": out[-600:]})
        _log(f"{nome}: rc={rc} {out[:200]}")
        return rc == 0

    if not (REPO / ".git").is_dir():
        return {"ok": False, "errore": f"{REPO} non e' un repository git"}

    rc, sporco = _esegui(["git", "status", "--porcelain"])
    if rc != 0 or sporco:
        return {"ok": False, "errore": "Il repository sul server ha modifiche "
                                       "non committate: risolvile a mano prima "
                                       "di pubblicare.\n" + sporco[:400],
                "passi": passi}

    if not step("git pull", ["git", "pull", "--ff-only"]):
        return {"ok": False, "errore": "git pull fallito", "passi": passi}

    rc, sha_prima = _esegui(["git", "rev-parse", "HEAD"])
    sha_prima = sha_prima.strip()

    if not step("applica patch", ["git", "apply", "--index", "-"], inp=patch):
        return {"ok": False, "errore": "La patch non si applica al codice "
                                       "attuale del server: probabilmente e' "
                                       "cambiato qualcosa. Rigenerala.",
                "passi": passi}

    msg = (f"{riassunto.strip() or 'Modifica dalla chat sviluppatore'}\n\n"
           f"Pubblicata da {autore} tramite la pagina sviluppatore di ISEOPilot.\n")
    if not step("commit", ["git", "-c", "user.name=ISEOPilot",
                           "-c", "user.email=iseopilot@iseo.com",
                           "commit", "-m", msg]):
        _esegui(["git", "reset", "--hard", sha_prima])
        return {"ok": False, "errore": "commit fallito", "passi": passi}

    # L'immagine attuale viene messa da parte PRIMA di ricostruire: e' il
    # ritorno indietro rapido, senza dover ricompilare nulla.
    _esegui(["docker", "tag", IMMAGINE, IMMAGINE_PREC], timeout=120)

    if not step("build", ["docker", "compose", "-f", COMPOSE, "build", SERVIZIO]):
        _esegui(["git", "reset", "--hard", sha_prima])
        return {"ok": False, "errore": "build fallita: nulla e' stato messo in "
                                       "produzione, il codice e' stato riportato "
                                       "indietro.", "passi": passi}

    step("avvio", ["docker", "compose", "-f", COMPOSE, "up", "-d", SERVIZIO])

    if _in_salute():
        step("push", ["git", "push", "origin", "HEAD:main"])
        _log("pubblicazione riuscita")
        return {"ok": True, "commit": _esegui(["git", "rev-parse", "--short", "HEAD"])[1],
                "passi": passi}

    # Non risponde: si torna all'immagine e al codice di prima.
    _log("healthcheck FALLITO: ritorno indietro")
    passi.append({"passo": "healthcheck", "esito": 1,
                  "output": f"nessuna risposta da {SALUTE_URL} entro {ATTESA_SALUTE}s"})
    _esegui(["git", "reset", "--hard", sha_prima])
    _esegui(["docker", "tag", IMMAGINE_PREC, IMMAGINE], timeout=120)
    _esegui(["docker", "compose", "-f", COMPOSE, "up", "-d", SERVIZIO])
    tornata = _in_salute()
    return {"ok": False, "rollback": True, "rollback_riuscito": tornata,
            "errore": ("La nuova versione non risponde: sono tornato alla "
                       "precedente. " + ("Il servizio e' di nuovo in salute."
                                         if tornata else
                                         "ATTENZIONE: nemmeno la versione "
                                         "precedente risponde, serve un "
                                         "intervento manuale.")),
            "passi": passi}


class Gestore(BaseHTTPRequestHandler):
    def _rispondi(self, codice: int, corpo: dict):
        dati = json.dumps(corpo, ensure_ascii=False).encode()
        self.send_response(codice)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(dati)))
        self.end_headers()
        self.wfile.write(dati)

    def do_POST(self):
        if self.path != "/pubblica":
            return self._rispondi(404, {"errore": "non trovato"})
        atteso = TOKEN_FILE.read_text().strip() if TOKEN_FILE.is_file() else ""
        if not atteso or self.headers.get("X-Deploy-Token", "") != atteso:
            _log("richiesta rifiutata: token non valido")
            return self._rispondi(403, {"errore": "token non valido"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            corpo = json.loads(self.rfile.read(n).decode())
        except Exception as e:
            return self._rispondi(400, {"errore": f"richiesta non valida: {e}"})
        patch = str(corpo.get("patch") or "")
        if not patch.strip():
            return self._rispondi(400, {"errore": "patch mancante"})
        autore = str(corpo.get("autore") or "sconosciuto")[:80]
        _log(f"pubblicazione richiesta da {autore}")
        try:
            self._rispondi(200, pubblica(patch, str(corpo.get("riassunto") or ""), autore))
        except subprocess.TimeoutExpired:
            self._rispondi(504, {"ok": False, "errore": "operazione scaduta"})
        except Exception as e:
            _log(f"errore imprevisto: {type(e).__name__}: {e}")
            self._rispondi(500, {"ok": False, "errore": f"{type(e).__name__}: {e}"})

    def log_message(self, *a):
        pass          # il log lo scriviamo noi, con piu' contesto


class ServerUnix(socketserver.ThreadingUnixStreamServer):
    """HTTP su socket unix. BaseHTTPRequestHandler si aspetta un indirizzo
    a coppia: su AF_UNIX non c'e', e gliene diamo uno fittizio."""
    allow_reuse_address = True

    def get_request(self):
        conn, _ = super().get_request()
        return conn, ("locale", 0)


if __name__ == "__main__":
    if not REPO.is_dir():
        sys.exit(f"Repository non trovato: {REPO}")
    SOCKET.parent.mkdir(parents=True, exist_ok=True)
    if SOCKET.exists():
        SOCKET.unlink()          # avanzo di un'esecuzione precedente
    srv = ServerUnix(str(SOCKET), Gestore)
    try:
        os.chown(SOCKET, 0, SOCKET_GID)   # gruppo dell'utente del container
        os.chmod(SOCKET, 0o660)           # nessun altro utente dell'host
    except OSError as e:
        _log(f"ATTENZIONE: permessi del socket non impostati ({e}). "
             f"Il container potrebbe non riuscire a contattare l'agente.")
    _log(f"agente avviato su {SOCKET} — ambito: {REPO} / servizio {SERVIZIO}")
    srv.serve_forever()

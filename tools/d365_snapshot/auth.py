"""Autenticazione Azure AD (device code + refresh), solo stdlib."""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.parse
import urllib.request
import urllib.error
from pathlib import Path

from . import config


def _post_form(url: str, data: dict) -> tuple[int, dict]:
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors="replace")
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"error": "http_error", "error_description": raw[:400]}


def _scope() -> str:
    return f"{config.RESOURCE}/.default offline_access"


def _save(tok: dict, path: Path) -> None:
    tok = dict(tok)
    tok["expires_at"] = time.time() + int(tok.get("expires_in", 3600)) - 120
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(tok, indent=1))
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def device_code_login(path: Path | None = None) -> dict:
    """Login interattivo: stampa un codice, l'utente lo incolla nel browser."""
    path = path or config.TOKEN_FILE
    st, dc = _post_form(
        f"{config.AUTHORITY}/oauth2/v2.0/devicecode",
        {"client_id": config.CLIENT_ID, "scope": _scope()},
    )
    if st != 200:
        raise SystemExit(f"Device code fallito: {dc.get('error_description', dc)}")

    print("\n" + "=" * 62)
    print("  ACCESSO A DYNAMICS 365")
    print("=" * 62)
    print(f"  1. Apri: {dc['verification_uri']}")
    print(f"  2. Codice: {dc['user_code']}")
    print("  3. Accedi con il tuo account ISEO.")
    print("=" * 62 + "\n", flush=True)

    interval = int(dc.get("interval", 5))
    deadline = time.time() + int(dc.get("expires_in", 900))
    while time.time() < deadline:
        time.sleep(interval)
        st, tok = _post_form(
            f"{config.AUTHORITY}/oauth2/v2.0/token",
            {"grant_type": "urn:ietf:params:oauth:grant-type:device_code",
             "client_id": config.CLIENT_ID, "device_code": dc["device_code"]},
        )
        if st == 200:
            _save(tok, path)
            print(f"Accesso riuscito. Token salvato in {path}\n")
            return tok
        err = tok.get("error", "")
        if err == "authorization_pending":
            continue
        if err == "slow_down":
            interval += 5
            continue
        raise SystemExit(f"Login fallito: {tok.get('error_description', err)}")
    raise SystemExit("Login scaduto: riprova.")


def _refresh(tok: dict, path: Path) -> dict | None:
    rt = tok.get("refresh_token")
    if not rt:
        return None
    st, new = _post_form(
        f"{config.AUTHORITY}/oauth2/v2.0/token",
        {"grant_type": "refresh_token", "client_id": config.CLIENT_ID,
         "refresh_token": rt, "scope": _scope()},
    )
    if st != 200:
        return None
    new.setdefault("refresh_token", rt)
    _save(new, path)
    return new


class TokenProvider:
    """Fornisce un access token valido, rinnovandolo quando serve.

    `borrow` permette di partire dal token di IseoPilot SENZA riscriverlo:
    il rinnovo finisce sempre nel file dedicato, cosi' il connettore in
    produzione non perde il suo refresh token.
    """

    def __init__(self, path: Path | None = None, borrow: Path | None = None,
                 interactive: bool = True):
        self.path = path or config.TOKEN_FILE
        self.borrow = borrow
        self.interactive = interactive
        self._tok: dict | None = None

    def _load(self) -> dict | None:
        for p in (self.path, self.borrow):
            if p and p.exists():
                try:
                    tok = json.loads(p.read_text())
                    if tok.get("access_token"):
                        return tok
                except Exception:
                    continue
        return None

    def token(self) -> str:
        tok = self._tok or self._load()
        if tok and tok.get("expires_at", 0) > time.time() + 60:
            self._tok = tok
            return tok["access_token"]
        if tok:
            new = _refresh(tok, self.path)
            if new:
                self._tok = new
                return new["access_token"]
        if not self.interactive:
            raise SystemExit(
                "Token assente o scaduto. Esegui prima:  python3 -m d365_snapshot login"
            )
        self._tok = device_code_login(self.path)
        return self._tok["access_token"]

    def whoami(self) -> dict:
        """Decodifica il JWT (solo i claim identificativi, nessun segreto)."""
        import base64
        t = self.token().split(".")
        if len(t) < 2:
            return {}
        pad = t[1] + "=" * (-len(t[1]) % 4)
        try:
            c = json.loads(base64.urlsafe_b64decode(pad).decode())
        except Exception:
            return {}
        return {k: c.get(k) for k in ("name", "upn", "unique_name", "tid", "aud") if c.get(k)}

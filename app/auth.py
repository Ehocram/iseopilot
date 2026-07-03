"""
auth.py — Autenticazione locale gestita dall'admin.

Password: hash scrypt (memory-hard) della libreria standard — nessuna dipendenza
esterna. Formato memorizzato: scrypt$n$r$p$salt_b64$hash_b64.

Identità: derivata dalla SESSIONE (cookie firmato), non più dall'header del
proxy. L'admin crea e gestisce gli utenti; ogni utente appartiene a un
dipartimento che governa la conoscenza KB visibile.

Bootstrap: al primo avvio, se non esistono utenti, viene creato un admin
iniziale da BOOTSTRAP_ADMIN_USER / BOOTSTRAP_ADMIN_PASSWORD. Cambiare la
password al primo accesso e rimuovere quelle variabili d'ambiente.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os

from fastapi import Request

from . import store

# Parametri scrypt: ~16 MB di memoria per hash (n*r*128 byte).
_SCRYPT_N = 16384
_SCRYPT_R = 8
_SCRYPT_P = 1
_MAXMEM = 64 * 1024 * 1024


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.scrypt(
        password.encode("utf-8"), salt=salt,
        n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=32, maxmem=_MAXMEM,
    )
    return "scrypt${}${}${}${}${}".format(
        _SCRYPT_N, _SCRYPT_R, _SCRYPT_P,
        base64.b64encode(salt).decode(), base64.b64encode(dk).decode(),
    )


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt_b64, hash_b64 = stored.split("$")
        if algo != "scrypt":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(hash_b64)
        dk = hashlib.scrypt(
            password.encode("utf-8"), salt=salt,
            n=int(n), r=int(r), p=int(p), dklen=len(expected), maxmem=_MAXMEM,
        )
        return hmac.compare_digest(dk, expected)
    except Exception:
        return False


def authenticate(username: str, password: str) -> dict | None:
    """Verifica le credenziali. Ritorna il record utente o None."""
    u = store.get_user((username or "").strip())
    if not u or not u["active"]:
        return None
    if not verify_password(password or "", u["password_hash"]):
        return None
    return u


def current_user(request: Request) -> dict | None:
    """Utente loggato (dalla sessione), o None. Revoca immediata: se l'utente è
    stato disattivato o eliminato dall'admin, la sessione decade subito."""
    username = request.session.get("user")
    if not username:
        return None
    u = store.get_user(username)
    if not u or not u["active"]:
        request.session.clear()
        return None
    return u


def bootstrap_admin() -> None:
    """Crea l'admin iniziale se non esistono utenti."""
    if store.user_count() > 0:
        return
    user = os.environ.get("BOOTSTRAP_ADMIN_USER", "").strip()
    pwd = os.environ.get("BOOTSTRAP_ADMIN_PASSWORD", "")
    if not user or not pwd:
        return  # nessun bootstrap configurato: l'admin lo creerà manualmente
    dept = os.environ.get("BOOTSTRAP_ADMIN_DEPARTMENT", "IT").strip() or "IT"
    if not store.department_exists(dept):
        store.add_department(dept)
    store.create_user(user, hash_password(pwd), dept, is_admin=True)


# ── Politica password ISEOPilot ─────────────────────────────
# Minimo 12 caratteri, almeno una lettera maiuscola e almeno un carattere
# speciale. Unica funzione per TUTTI i punti in cui nasce una password
# (self-service, creazione utente, reset admin): una sola verità.
import re as _re

PASSWORD_POLICY_HINT = ("Almeno 12 caratteri, con almeno una lettera maiuscola "
                        "e un carattere speciale (es. ! ? @ # - _).")


def validate_password(pwd: str) -> str | None:
    """Ritorna il messaggio d'errore se la password non rispetta la politica,
    None se è conforme."""
    p = pwd or ""
    problemi = []
    if len(p) < 12:
        problemi.append("almeno 12 caratteri")
    if not _re.search(r"[A-Z]", p):
        problemi.append("una lettera maiuscola")
    if not _re.search(r"[^A-Za-z0-9]", p):
        problemi.append("un carattere speciale")
    if problemi:
        return "La password deve contenere " + ", ".join(problemi) + "."
    return None

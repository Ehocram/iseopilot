"""Diagnostica in-app: tee di stderr su un ring buffer consultabile
dall'amministratore (Admin → Log). Cattura TUTte le righe [attach],
[attach-ctx], [web], [folder_index] ecc. senza toccare i punti di stampa."""
from __future__ import annotations

import sys
import datetime as _dt
from collections import deque

BUFFER: deque = deque(maxlen=800)
_real_stderr = sys.stderr


class _Tee:
    def write(self, s):
        try:
            _real_stderr.write(s)
        except Exception:
            pass
        try:
            t = s.rstrip()
            if t:
                BUFFER.append(f"[{_dt.datetime.now():%H:%M:%S}] {t}")
        except Exception:
            pass
        return len(s)

    def flush(self):
        try:
            _real_stderr.flush()
        except Exception:
            pass

    def isatty(self):
        return False


def install():
    if not isinstance(sys.stderr, _Tee):
        sys.stderr = _Tee()


def tail(n: int = 400, filtro: str = "") -> list:
    righe = list(BUFFER)[-n:]
    if filtro:
        f = filtro.lower()
        righe = [r for r in righe if f in r.lower()]
    return righe

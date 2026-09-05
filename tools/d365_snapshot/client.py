"""Client HTTP verso D365 F&O: retry, throttling, paging OData."""
from __future__ import annotations

import gzip
import json
import random
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from . import config


def build_query(params: dict) -> str:
    """Query string OData correttamente codificata.

    I $filter contengono spazi ("Campo eq 'x'"): passati grezzi a urllib
    sollevano InvalidURL e la richiesta non parte nemmeno. Gli apici singoli
    restano leggibili, sono legali in una query string.
    """
    parti = []
    for k, v in params.items():
        if v is None or v == "":
            continue
        parti.append(f"{k}={urllib.parse.quote(str(v), safe=chr(39))}")
    return "&".join(parti)


class Throttled(Exception):
    pass


class NotFound(Exception):
    pass


class D365Client:
    def __init__(self, token_provider, resource: str | None = None, verbose: bool = True):
        self.tp = token_provider
        self.base = (resource or config.RESOURCE).rstrip("/")
        self.verbose = verbose
        self._lock = threading.Lock()
        self.calls = 0
        self.errors = 0
        # Pausa globale: se il servizio ci limita, tutti i thread rallentano.
        self._pause_until = 0.0

    # ---------------------------------------------------------------- basso livello
    def _raw(self, url: str, timeout: int,
             accept: str = "application/json") -> tuple[int, dict, bytes]:
        req = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {self.tp.token()}",
            "Accept": accept,
            "Accept-Encoding": "gzip",
            "OData-MaxVersion": "4.0",
            "OData-Version": "4.0",
            "User-Agent": "IseoPilot-D365-Snapshot/1.0",
        })
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    data = gzip.decompress(data)
                return r.status, dict(r.headers), data
        except urllib.error.HTTPError as e:
            data = e.read()
            if e.headers.get("Content-Encoding") == "gzip":
                try:
                    data = gzip.decompress(data)
                except Exception:
                    pass
            return e.code, dict(e.headers), data

    def get(self, path: str, timeout: int | None = None, retries: int | None = None,
            accept: str = "application/json"):
        """GET su un percorso relativo. Ritorna JSON (dict/list) o testo.

        `accept` va forzato a application/xml per /data/$metadata: con
        application/json il servizio risponde 400 (serializza l'EDMX come JSON
        e incappa in un riferimento circolare).
        """
        timeout = timeout or config.HTTP_TIMEOUT
        retries = config.MAX_RETRIES if retries is None else retries
        url = path if path.startswith("http") else self.base + path
        last = ""
        for attempt in range(retries + 1):
            wait = self._pause_until - time.time()
            if wait > 0:
                time.sleep(min(wait, 60))
            status, headers, body = self._raw(url, timeout, accept)
            with self._lock:
                self.calls += 1
            if status == 200:
                txt = body.decode("utf-8-sig", errors="replace")
                ct = (headers.get("Content-Type") or "").lower()
                if "json" in ct:
                    return json.loads(txt) if txt.strip() else {}
                return txt
            if status == 404:
                raise NotFound(path)
            if status in (401, 403):
                # token scaduto a meta' harvest: forziamo un rinnovo e ritentiamo
                self.tp._tok = None
                last = f"HTTP {status}"
                time.sleep(2)
                continue
            if status in (429, 503, 504, 502, 500):
                ra = headers.get("Retry-After")
                delay = float(ra) if ra and ra.isdigit() else min(60, 2 ** attempt * 3)
                delay += random.uniform(0, 1.5)
                with self._lock:
                    self._pause_until = max(self._pause_until, time.time() + delay)
                last = f"HTTP {status}"
                continue
            last = f"HTTP {status}: {body[:200].decode(errors='replace')}"
            break
        with self._lock:
            self.errors += 1
        raise Throttled(f"{path} -> {last}")

    # ---------------------------------------------------------------- OData
    def get_all(self, path: str, page_note: str = "", cap: int | None = None) -> list:
        """Scarica una collection OData seguendo @odata.nextLink."""
        out: list = []
        url = path
        while url:
            r = self.get(url)
            if not isinstance(r, dict):
                break
            out.extend(r.get("value", []))
            url = r.get("@odata.nextLink")
            if self.verbose and page_note:
                print(f"\r  {page_note}: {len(out)}", end="", flush=True)
            if cap and len(out) >= cap:
                break
        if self.verbose and page_note:
            print(f"\r  {page_note}: {len(out)}   ")
        return out

    def count(self, entity_set: str, query: str = "", timeout: int = 45) -> int | None:
        """Conteggio righe. `query` e' la query string gia' composta (senza '?').

        None = non calcolabile: timeout, entita' non interrogabile, oppure
        filtro non applicabile.
        """
        q = ("?" + query) if query else ""
        try:
            r = self.get(f"/data/{entity_set}/$count{q}", timeout=timeout, retries=1)
            return int(str(r).strip())
        except Exception:
            return None

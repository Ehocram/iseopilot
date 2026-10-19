"""
Test di `devchat.leggi`: la chat sviluppatore non deve poter leggere
credenziali o uscire dal repository. Il percorso arriva dal modello, quindi è
input non fidato. Tutto offline, su un repository finto in una cartella
temporanea (più una verifica in sola lettura sul repository vero).
Esecuzione:
    PYTHONPATH=. python tests/test_devchat_leggi.py
"""
import os
import tempfile
from pathlib import Path

from app import devchat

REPO_VERO = Path(__file__).resolve().parent.parent
SEGRETO = "CANARINO_SEGRETO_7f3a"


def _repo_finto() -> tuple[tempfile.TemporaryDirectory, Path, Path]:
    """Repo finto + una cartella ACCANTO (fuori dal repo) con un segreto."""
    td = tempfile.TemporaryDirectory()
    base = Path(td.name)
    repo, fuori = base / "repo", base / "fuori"
    (repo / "app").mkdir(parents=True)
    (repo / ".git").mkdir()
    fuori.mkdir()
    (repo / "app" / "main.py").write_text("print('ciao')\n", encoding="utf-8")
    (repo / "Dockerfile").write_text("FROM python:3.11-slim\n", encoding="utf-8")
    (repo / ".env").write_text(f"CLAUDE_API_KEY={SEGRETO}\n", encoding="utf-8")
    (repo / ".git" / "config").write_text(f"[remote] url={SEGRETO}\n", encoding="utf-8")
    (repo / "LEGGIMI").write_text(f"{SEGRETO}\n", encoding="utf-8")
    (repo / "app" / "secrets.json").write_text(f'{{"k": "{SEGRETO}"}}\n', encoding="utf-8")
    (repo / "app" / "api_key.py").write_text(f"K = '{SEGRETO}'\n", encoding="utf-8")
    (repo / "app" / "token.txt").write_text(f"{SEGRETO}\n", encoding="utf-8")
    (repo / "config.env").write_text(f"X={SEGRETO}\n", encoding="utf-8")
    (fuori / "segreto.txt").write_text(f"{SEGRETO}\n", encoding="utf-8")
    # link simbolici: verso fuori dal repo, e verso un file innocuo DENTRO il repo
    os.symlink(fuori / "segreto.txt", repo / "app" / "link_fuori.txt")
    os.symlink(repo / "app" / "main.py", repo / "app" / "link_dentro.py")
    os.symlink(repo / ".env", repo / "app" / "config.py")
    os.symlink(fuori, repo / "cartella_link")
    return td, repo, fuori


def _rifiutato(rel: str) -> bool:
    """Rifiutato = errore restituito (o FuoriRepo sollevato) e nessun contenuto."""
    try:
        r = devchat.leggi(rel)
    except devchat.FuoriRepo:
        return True
    return "errore" in r and "contenuto" not in r and SEGRETO not in str(r)


def _con_repo(monkeypatch, fn):
    td, repo, fuori = _repo_finto()
    try:
        monkeypatch.setattr(devchat, "REPO", repo)
        fn(repo, fuori)
    finally:
        td.cleanup()


def test_env_rifiutato(monkeypatch):
    def corpo(repo, fuori):
        for rel in (".env", "/.env", "./.env", "app/../.env", "config.env"):
            assert _rifiutato(rel), rel
    _con_repo(monkeypatch, corpo)


def test_git_config_rifiutato(monkeypatch):
    def corpo(repo, fuori):
        assert _rifiutato(".git/config")
        assert _rifiutato("app/../.git/config")
    _con_repo(monkeypatch, corpo)


def test_risalita_rifiutata(monkeypatch):
    def corpo(repo, fuori):
        for rel in ("../fuori/segreto.txt", "app/../../fuori/segreto.txt",
                    "..", "../../../../etc/passwd"):
            assert _rifiutato(rel), rel
    _con_repo(monkeypatch, corpo)


def test_link_simbolici_rifiutati(monkeypatch):
    def corpo(repo, fuori):
        assert _rifiutato("app/link_fuori.txt")       # porta fuori dal repo
        assert _rifiutato("app/link_dentro.py")       # resta dentro: rifiutato lo stesso
        assert _rifiutato("app/config.py")            # nome innocuo, punta a .env
        assert _rifiutato("cartella_link/segreto.txt")  # cartella-link
    _con_repo(monkeypatch, corpo)


def test_senza_estensione_rifiutato(monkeypatch):
    def corpo(repo, fuori):
        assert _rifiutato("LEGGIMI")
    _con_repo(monkeypatch, corpo)


def test_nomi_credenziali_rifiutati(monkeypatch):
    def corpo(repo, fuori):
        for rel in ("app/secrets.json", "app/api_key.py", "app/token.txt",
                    "app/Secrets.json"):
            assert _rifiutato(rel), rel
    _con_repo(monkeypatch, corpo)


def test_main_e_dockerfile_ammessi(monkeypatch):
    def corpo(repo, fuori):
        r = devchat.leggi("app/main.py")
        assert "errore" not in r and "print('ciao')" in r["contenuto"]
        r = devchat.leggi("/app/main.py")
        assert "errore" not in r and r["righe_totali"] == 1
        r = devchat.leggi("Dockerfile")
        assert "errore" not in r and "FROM python" in r["contenuto"]
    _con_repo(monkeypatch, corpo)


def test_repo_vero(monkeypatch):
    """Sul repository vero: app/main.py e Dockerfile si leggono, .env no."""
    monkeypatch.setattr(devchat, "REPO", REPO_VERO)
    r = devchat.leggi("app/main.py", 1, 5)
    assert "errore" not in r and r["righe_totali"] > 5
    if (REPO_VERO / "Dockerfile").is_file():
        r = devchat.leggi("Dockerfile", 1, 3)
        assert "errore" not in r and r["contenuto"]
    r = devchat.leggi(".env")
    assert "errore" in r and "contenuto" not in r


def test_cerca_salta_i_file_non_leggibili(monkeypatch):
    """`cerca` applica le stesse regole di `leggi`: nessuna riga da file
    riservati, nascosti o raggiunti tramite link."""
    def corpo(repo, fuori):
        (repo / "app" / "main.py").write_text(f"x = 1  # {SEGRETO}\n", encoding="utf-8")
        trovati = devchat.cerca(SEGRETO)
        assert [r["file"] for r in trovati] == ["app/main.py"], trovati
    _con_repo(monkeypatch, corpo)


def test_strumento_leggi_restituisce_errore(monkeypatch):
    """Dal ciclo con Claude: il rifiuto torna come errore JSON, mai come contenuto."""
    def corpo(repo, fuori):
        for rel in (".env", "../fuori/segreto.txt", "app/link_fuori.txt"):
            out = devchat._esegui_strumento("leggi", {"percorso": rel})
            assert "errore" in out and SEGRETO not in out, rel
    _con_repo(monkeypatch, corpo)


if __name__ == "__main__":
    import inspect
    import sys

    class _MP:
        """monkeypatch minimale: ripristina gli attributi a fine test."""
        def __init__(self):
            self._salvati = []

        def setattr(self, obj, name, value):
            self._salvati.append((obj, name, getattr(obj, name)))
            setattr(obj, name, value)

        def undo(self):
            for obj, name, v in reversed(self._salvati):
                setattr(obj, name, v)

    fns = [f for n, f in sorted(globals().items())
           if n.startswith("test_") and inspect.isfunction(f)]
    failed = 0
    for f in fns:
        mp = _MP()
        try:
            kwargs = {}
            if "monkeypatch" in inspect.signature(f).parameters:
                kwargs["monkeypatch"] = mp
            f(**kwargs)
            print(f"  PASS  {f.__name__}")
        except Exception as e:
            failed += 1
            import traceback
            traceback.print_exc()
            print(f"  FAIL  {f.__name__}: {e!r}")
        finally:
            mp.undo()
    print(f"\n{len(fns)-failed}/{len(fns)} test superati.")
    sys.exit(1 if failed else 0)

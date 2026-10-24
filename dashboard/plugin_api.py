"""pinned-folders: server-side layout store.

One JSON file per Hermes home: ``$HERMES_HOME/pinned-folders/layout.json``.
Every Desktop app connected to this backend reads and writes the same file, so
the folder layout follows you between machines, and an agent on the server can
file chats by editing the file (the revision is the file's mtime, so a direct
edit is picked up by the apps on their next poll).

Routes (mounted at /api/plugins/pinned-folders/):
  GET  /layout  -> {"rev": str|None, "layout": dict|None}
  PUT  /layout  body {"layout": dict, "base": str|None}
               -> {"rev": str, "layout": dict}, or 409 {"rev", "layout"} when
                  ``base`` is given and no longer matches the file (someone else
                  wrote since the caller last read).

No network, no other files touched. The layout is validated for shape and size
only; it holds folder names, colors and session ids, never chat content.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Body
from fastapi.responses import JSONResponse

router = APIRouter()
_lock = threading.Lock()
MAX_BYTES = 2_000_000


def _home() -> Path:
    try:
        from hermes_constants import get_hermes_home

        return Path(get_hermes_home())
    except Exception:
        return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")


def _path() -> Path:
    return _home() / "pinned-folders" / "layout.json"


def _read() -> tuple[Optional[str], Optional[dict]]:
    p = _path()
    try:
        st = p.stat()
    except FileNotFoundError:
        return None, None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return str(st.st_mtime_ns), None
    return str(st.st_mtime_ns), data if isinstance(data, dict) else None


def _valid(layout: Any) -> bool:
    if not isinstance(layout, dict):
        return False
    if not isinstance(layout.get("folders", []), list):
        return False
    if not isinstance(layout.get("placed", {}), dict):
        return False
    if not isinstance(layout.get("order", []), list):
        return False
    return len(json.dumps(layout)) <= MAX_BYTES


def _write(layout: dict) -> str:
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=".layout.", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(layout, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, p)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return str(p.stat().st_mtime_ns)


@router.get("/layout")
def get_layout():
    with _lock:
        rev, layout = _read()
    return {"rev": rev, "layout": layout}


@router.put("/layout")
def put_layout(payload: dict = Body(...)):
    layout = payload.get("layout") if isinstance(payload, dict) else None
    if not _valid(layout):
        return JSONResponse({"error": "invalid layout"}, status_code=400)
    base = payload.get("base")
    with _lock:
        rev, current = _read()
        if base is not None and rev is not None and base != rev:
            return JSONResponse({"rev": rev, "layout": current}, status_code=409)
        new_rev = _write(layout)
    return {"rev": new_rev, "layout": layout}


@router.post("/title")
def set_title(payload: dict = Body(...)):
    """Rename a chat (user-provenance title) in the owning profile's state.db."""
    sid = str((payload or {}).get("session_id") or "").strip()
    title = str((payload or {}).get("title") or "").strip()
    profile = str((payload or {}).get("profile") or "default").strip() or "default"
    if not sid or not title or len(title) > 200:
        return JSONResponse({"error": "session_id and title (1-200 chars) required"}, status_code=400)
    try:
        from hermes_cli.profiles import get_profile_dir
        from hermes_state import SessionDB

        db = SessionDB(Path(get_profile_dir(profile)) / "state.db")
        try:
            ok = db.set_session_title(sid, title)
            if not ok and not db.get_session(sid):
                return JSONResponse({"error": "unknown session"}, status_code=404)
        finally:
            close = getattr(db, "close", None)
            if callable(close):
                close()
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=409)
    return {"ok": True, "title": title}


# ── devbox file explorer ─────────────────────────────────────────────────────
# Everything below serves the "Fichiers" pane. Scope: the user's $HOME on the
# machine running this backend, minus secrets. Every path goes through
# ``_resolve`` (realpath, must stay inside $HOME, must not be denied), so
# neither "..", nor a symlink pointing out of $HOME or at a secret, nor a
# secret path itself can be listed, read, written, renamed or deleted.
#
# Routes (all POST, JSON in / JSON out, errors as {"error": str} + status):
#   /fs/open      {candidates: [path]}      -> listing of the first existing dir
#   /fs/list      {path}                    -> {path, home, parent, entries[], truncated}
#   /fs/read      {path}                    -> {kind: text|image|pdf|binary, ...}
#   /fs/download  {path}                    -> {name, size, mime, base64}
#   /fs/write     {path, text|base64, overwrite?} -> {ok, path, size}  (409 if exists)
#   /fs/mkdir     {path}                    -> {ok, path}
#   /fs/rename    {path, new_name}          -> {ok, path}
#   /fs/delete    {path}                    -> {ok}

import base64
import fnmatch
import mimetypes
import shutil

FS_TEXT_CAP = 2 * 1024 * 1024
FS_MEDIA_CAP = 12 * 1024 * 1024
FS_DOWNLOAD_CAP = 50 * 1024 * 1024
FS_WRITE_CAP = 25 * 1024 * 1024
FS_LIST_CAP = 5000

_DENIED_REL = (
    ".ssh", ".gnupg", ".aws", ".kube", ".git-credentials", ".netrc", ".npmrc",
    ".docker/config.json", ".config/devbox", ".config/gh", ".config/hermes-m365",
    ".hermes/mcp-tokens", ".hermes/.room-link-grant-secret",
)
_DENIED_NAMES = (
    "*.pem", "*.key", "*.p12", "*.pfx", "*.keystore", "id_rsa*", "id_ed25519*",
    "id_ecdsa*", "id_dsa*", ".env", ".env.*", "*msal*", "*token_cache*", "*token-cache*",
)
_ENV_SAFE = {".env.example", ".env.sample", ".env.template"}


class FsError(Exception):
    def __init__(self, msg: str, status: int = 400, **extra: Any):
        super().__init__(msg)
        self.msg, self.status, self.extra = msg, status, extra


def _fs_home() -> Path:
    return Path(os.path.realpath(Path.home()))


def _hermes_homes() -> list[Path]:
    hh = Path(os.path.realpath(_home()))
    return [hh]


def _denied_roots(home: Path) -> list[Path]:
    roots = [home / r for r in _DENIED_REL]
    for hh in _hermes_homes():
        roots += [hh / "mcp-tokens", hh / ".env", hh / "auth.json", hh / "auth.lock"]
    return roots


def _hermes_bases() -> list[Path]:
    """The Hermes home and each profile dir: auth*/.env* directly inside are secrets."""
    bases = []
    for hh in _hermes_homes():
        bases.append(hh)
        prof = hh / "profiles"
        try:
            bases += [c for c in prof.iterdir() if c.is_dir()]
        except OSError:
            pass
    return bases


def _is_denied(p: Path, home: Optional[Path] = None) -> bool:
    """True when ``p`` is a secret, or lies inside one."""
    home = home or _fs_home()
    for root in _denied_roots(home):
        if p == root or root in p.parents:
            return True
    try:
        rel = p.relative_to(home)
    except ValueError:
        rel = Path(*p.parts[1:]) if p.is_absolute() else p
    for part in rel.parts:
        low = part.lower()
        if low in _ENV_SAFE:
            continue
        if any(fnmatch.fnmatch(low, pat) for pat in _DENIED_NAMES):
            return True
    for base in _hermes_bases():
        if p.parent == base and p.name.startswith(("auth", ".env")) and p.name.lower() not in _ENV_SAFE:
            return True
        try:
            parts = p.relative_to(base).parts
        except ValueError:
            continue
        if parts and (parts[0].startswith("auth") or parts[0] == "mcp-tokens"):
            return True
        if len(parts) >= 3 and parts[0] == "platforms" and parts[1] == "whatsapp" and parts[2].startswith("session"):
            return True
    return False


def _shields_denied(p: Path, home: Path) -> bool:
    """True when ``p`` is an ancestor of a secret (deleting/renaming it would destroy or move the secret)."""
    return any(p in root.parents for root in _denied_roots(home))


def _resolve(raw: Any, follow: bool = True) -> Path:
    if not isinstance(raw, str) or not raw.strip() or "\0" in raw:
        raise FsError("chemin invalide")
    home = _fs_home()
    p = Path(raw).expanduser()
    if not p.is_absolute():
        p = home / p
    lex = Path(os.path.normpath(p))
    base = lex if follow else lex.parent
    real = Path(os.path.realpath(base))
    target = real if follow else real / lex.name
    if target != home and home not in target.parents:
        raise FsError("chemin hors du dossier personnel", 403)
    if _is_denied(lex, home) or _is_denied(target, home):
        raise FsError("chemin protégé", 403)
    return target


def _entry(path: Path, home: Path) -> Optional[dict]:
    try:
        st = path.lstat()
        link = path.is_symlink()
        if link:
            real = Path(os.path.realpath(path))
            if (real != home and home not in real.parents) or _is_denied(real, home):
                return None
            st = real.stat()
        is_dir = os.path.isdir(path) if link else (st.st_mode & 0o170000) == 0o040000
    except OSError:
        return None
    return {"name": path.name, "type": "dir" if is_dir else "file", "size": 0 if is_dir else st.st_size, "mtime": int(st.st_mtime), "link": link}


def _listing(target: Path) -> dict:
    home = _fs_home()
    if not target.is_dir():
        raise FsError("ce n'est pas un dossier", 400)
    entries: list[dict] = []
    truncated = False
    try:
        with os.scandir(target) as it:
            for de in it:
                child = target / de.name
                if _is_denied(child, home):
                    continue
                e = _entry(child, home)
                if e:
                    entries.append(e)
                if len(entries) >= FS_LIST_CAP:
                    truncated = True
                    break
    except PermissionError:
        raise FsError("accès refusé par le système", 403)
    entries.sort(key=lambda e: (e["type"] != "dir", e["name"].lower()))
    return {"path": str(target), "home": str(home), "parent": None if target == home else str(target.parent), "entries": entries, "truncated": truncated}


def fs_list(path: Any) -> dict:
    return _listing(_resolve(path))


def fs_open(candidates: Any) -> dict:
    if isinstance(candidates, list):
        for c in candidates[:20]:
            try:
                t = _resolve(c)
            except FsError:
                continue
            if t.is_dir():
                return _listing(t)
    return _listing(_fs_home())


def fs_read(path: Any) -> dict:
    t = _resolve(path)
    if not t.is_file():
        raise FsError("fichier introuvable", 404)
    size = t.stat().st_size
    mime = mimetypes.guess_type(t.name)[0] or ""
    if mime.startswith("image/") or mime == "application/pdf":
        if size > FS_MEDIA_CAP:
            raise FsError("fichier trop volumineux pour l'aperçu", 413, size=size)
        return {"kind": "pdf" if mime == "application/pdf" else "image", "mime": mime, "size": size, "base64": base64.b64encode(t.read_bytes()).decode("ascii")}
    with open(t, "rb") as fh:
        data = fh.read(FS_TEXT_CAP + 1)
    if b"\0" in data[:8192]:
        return {"kind": "binary", "size": size}
    truncated = len(data) > FS_TEXT_CAP
    return {"kind": "text", "size": size, "truncated": truncated, "text": data[:FS_TEXT_CAP].decode("utf-8", errors="replace")}


def fs_download(path: Any) -> dict:
    t = _resolve(path)
    if not t.is_file():
        raise FsError("fichier introuvable", 404)
    size = t.stat().st_size
    if size > FS_DOWNLOAD_CAP:
        raise FsError("fichier trop volumineux (50 Mo max)", 413, size=size)
    return {"name": t.name, "size": size, "mime": mimetypes.guess_type(t.name)[0] or "application/octet-stream", "base64": base64.b64encode(t.read_bytes()).decode("ascii")}


def fs_write(path: Any, text: Any = None, b64: Any = None, overwrite: bool = False) -> dict:
    if (text is None) == (b64 is None):
        raise FsError("fournir text ou base64")
    if b64 is not None:
        if not isinstance(b64, str) or len(b64) > FS_WRITE_CAP * 4 // 3 + 8:
            raise FsError("fichier trop volumineux (25 Mo max)", 413)
        try:
            data = base64.b64decode(b64, validate=True)
        except Exception:
            raise FsError("base64 invalide")
    else:
        if not isinstance(text, str):
            raise FsError("text invalide")
        data = text.encode("utf-8")
    if len(data) > FS_WRITE_CAP:
        raise FsError("fichier trop volumineux (25 Mo max)", 413)
    t = _resolve(path)
    if t.is_dir():
        raise FsError("c'est un dossier", 400)
    if not t.parent.is_dir():
        raise FsError("dossier parent introuvable", 404)
    exists = t.exists()
    if exists and not overwrite:
        raise FsError("existe déjà", 409, exists=True)
    fd, tmp = tempfile.mkstemp(dir=t.parent, prefix=".pf-upload.")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        if exists:
            shutil.copymode(t, tmp)
        else:
            os.chmod(tmp, 0o644)
        os.replace(tmp, t)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return {"ok": True, "path": str(t), "size": len(data)}


def fs_mkdir(path: Any) -> dict:
    t = _resolve(path, follow=False)
    if os.path.lexists(t):
        raise FsError("existe déjà", 409, exists=True)
    if not t.parent.is_dir():
        raise FsError("dossier parent introuvable", 404)
    os.mkdir(t)
    return {"ok": True, "path": str(t)}


def _clean_name(name: Any) -> str:
    n = name.strip() if isinstance(name, str) else ""
    if not n or n in (".", "..") or "/" in n or "\0" in n or len(n) > 255:
        raise FsError("nom invalide")
    return n


def fs_rename(path: Any, new_name: Any) -> dict:
    home = _fs_home()
    src = _resolve(path, follow=False)
    if src == home:
        raise FsError("dossier personnel protégé", 403)
    if _shields_denied(src, home):
        raise FsError("ce dossier contient des éléments protégés", 403)
    if not os.path.lexists(src):
        raise FsError("introuvable", 404)
    dst = _resolve(str(src.parent / _clean_name(new_name)), follow=False)
    if os.path.lexists(dst):
        raise FsError("existe déjà", 409, exists=True)
    os.rename(src, dst)
    return {"ok": True, "path": str(dst)}


def fs_delete(path: Any) -> dict:
    home = _fs_home()
    t = _resolve(path, follow=False)
    if t == home:
        raise FsError("dossier personnel protégé", 403)
    if _shields_denied(t, home):
        raise FsError("ce dossier contient des éléments protégés", 403)
    if not os.path.lexists(t):
        raise FsError("introuvable", 404)
    if t.is_symlink() or not t.is_dir():
        os.unlink(t)
    else:
        shutil.rmtree(t)
    return {"ok": True}


def _fs_call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except FsError as e:
        return JSONResponse({"error": e.msg, **e.extra}, status_code=e.status)
    except OSError as e:
        return JSONResponse({"error": e.strerror or str(e)}, status_code=500)


def _body(payload: Any) -> dict:
    return payload if isinstance(payload, dict) else {}


@router.post("/fs/open")
def fs_open_route(payload: dict = Body(...)):
    return _fs_call(fs_open, _body(payload).get("candidates"))


@router.post("/fs/list")
def fs_list_route(payload: dict = Body(...)):
    return _fs_call(fs_list, _body(payload).get("path"))


@router.post("/fs/read")
def fs_read_route(payload: dict = Body(...)):
    return _fs_call(fs_read, _body(payload).get("path"))


@router.post("/fs/download")
def fs_download_route(payload: dict = Body(...)):
    return _fs_call(fs_download, _body(payload).get("path"))


@router.post("/fs/write")
def fs_write_route(payload: dict = Body(...)):
    b = _body(payload)
    return _fs_call(fs_write, b.get("path"), b.get("text"), b.get("base64"), bool(b.get("overwrite")))


@router.post("/fs/mkdir")
def fs_mkdir_route(payload: dict = Body(...)):
    return _fs_call(fs_mkdir, _body(payload).get("path"))


@router.post("/fs/rename")
def fs_rename_route(payload: dict = Body(...)):
    b = _body(payload)
    return _fs_call(fs_rename, b.get("path"), b.get("new_name"))


@router.post("/fs/delete")
def fs_delete_route(payload: dict = Body(...)):
    return _fs_call(fs_delete, _body(payload).get("path"))

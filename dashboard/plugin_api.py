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

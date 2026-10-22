"""Mounted at /api/plugins/user-steps.

GET  /ping                         -> {ok, api}
GET  /steps?session=<id>           -> {root, steps: [open steps, oldest first], card: {id, board}|null,
                                       own_cards: [this chat's rolling card ids, current + archived]}
GET  /step?id=<step id>&s=<sid>    -> {step|null, root}
POST /resolve {id, session?, resolution?, by?} -> {ok, resolved: [ids], card: {id, board}|null}
     Marks a step resolved (a Desktop button click) and archives the chat's rolling blocked card when it was the
     last open step.
"""
import importlib.util
import sys
from pathlib import Path
from typing import Optional

from fastapi import APIRouter
from pydantic import BaseModel

API_VERSION = 1


def _load(name: str):
    """Sibling module, reloaded when the file changes (edits apply without a backend restart)."""
    key = f"_user_steps_{name}"
    path = Path(__file__).resolve().parents[1] / f"{name}.py"
    mtime = path.stat().st_mtime
    mod = sys.modules.get(key)
    if mod is None or getattr(mod, "_mtime", None) != mtime:
        spec = importlib.util.spec_from_file_location(key, path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        setattr(mod, "_mtime", mtime)
        sys.modules[key] = mod
    return mod


class ResolveBody(BaseModel):
    id: str
    session: str = ""
    resolution: str = ""
    by: str = "desktop"


def create_router(ledger_dir: Optional[Path] = None, state_db: Optional[Path] = None, cards=None):
    router = APIRouter()

    def ledger():
        return _load("core").Ledger(ledger_dir, state_db)

    def card_ops():
        return cards if cards is not None else _load("cards").HermesCards(in_process=False)

    @router.get("/ping")
    def ping():
        return {"ok": True, "api": API_VERSION}

    @router.get("/steps")
    def steps(session: str = ""):
        return _load("core").steps_payload(ledger(), session)

    @router.get("/step")
    def step(id: str, s: str = ""):
        root, st = ledger().find(id, s)
        return {"root": root, "step": _load("core").public_step(st) if st else None}

    @router.post("/resolve")
    def resolve(b: ResolveBody):  # sync: FastAPI runs it in the threadpool (CLI archive call)
        core = _load("core")
        lg = ledger()
        root, st = lg.find(b.id, b.session)
        if not st:
            return {"ok": False, "error": "unknown step", "resolved": [], "card": None}
        done = core.resolve(lg, root, [b.id], b.resolution, b.by)
        sid = b.session or st.get("session") or root
        card = core.sync_card(lg, root, sid, f"@session:default/{sid}", card_ops())
        return {"ok": True, "resolved": done, "card": card}

    return router


router = create_router()

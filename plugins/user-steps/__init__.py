"""user-steps: what each chat is waiting for the user to do, framed in the reply and listed above the composer.

transform_llm_output  aux model (task `user_steps`) reads the final reply + open steps + the user's last message
                      → new steps go into ~/.hermes/user-steps/<lineage root>.json, their paragraph in the reply
                      becomes `::user-step{...}` (Desktop chats only), resolved steps close, and one rolling
                      blocked Kanban card per chat lists the open steps (archived when the last one resolves).
pre_llm_call          when steps are open, one note listing them (and the user message is kept for the extractor).
Skipped: Kanban workers, subagents, cron/webhook. Off switch: HERMES_USER_STEPS_OFF=1. See README.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

try:
    from . import cards as cards_mod, core
except ImportError:  # pytest/spec loading imports the hyphenated plugin dir without a parent package
    import importlib.util as _ilu
    import sys as _sys

    def _sib(name: str):
        key = "user_steps_" + name
        if key in _sys.modules:
            return _sys.modules[key]
        spec = _ilu.spec_from_file_location(key, Path(__file__).with_name(name + ".py"))
        mod = _ilu.module_from_spec(spec)  # type: ignore[arg-type]
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
        return _sys.modules.setdefault(key, mod)

    core, cards_mod = _sib("core"), _sib("cards")

log = logging.getLogger("user-steps")
SKIP_PLATFORMS = {"cron", "kanban", "webhook", "subagent"}
_USER_MSG: dict[str, str] = {}


def _eligible(session_id: str, platform: str = "", parent_session_id: str = "") -> bool:
    if not session_id or parent_session_id:
        return False
    if os.environ.get("HERMES_KANBAN_TASK") or os.environ.get("HERMES_USER_STEPS_OFF"):
        return False
    return (platform or "").lower() not in SKIP_PLATFORMS


def _profile() -> str:
    return os.environ.get("HERMES_SESSION_PROFILE") or os.environ.get("HERMES_PROFILE") or "default"


def _model_cfg() -> tuple[str | None, str | None]:
    prov, model = os.environ.get("USER_STEPS_PROVIDER"), os.environ.get("USER_STEPS_MODEL")
    if prov or model:
        return prov, model
    try:  # an explicit auxiliary.user_steps block wins; otherwise a fast Claude model (this runs on the reply path)
        from hermes_cli.config import load_config_readonly  # type: ignore

        cfg = (load_config_readonly().get("auxiliary") or {}).get("user_steps") or {}
        if cfg.get("model") or (cfg.get("provider") and cfg.get("provider") != "auto"):
            return None, None
    except Exception:
        pass
    return "anthropic", "claude-haiku-5-5"


def aux_llm(system: str, user: str) -> str:
    from agent.auxiliary_client import call_llm  # type: ignore

    prov, model = _model_cfg()
    kw: dict[str, Any] = {}
    if prov:
        kw["provider"] = prov
    if model:
        kw["model"] = model
    resp = call_llm(task="user_steps", messages=[{"role": "system", "content": system},
                                                  {"role": "user", "content": user}],
                    max_tokens=900, temperature=None, timeout=25, reasoning_config={"enabled": False}, **kw)
    return (getattr(resp.choices[0].message, "content", None) or "").strip()


def _ledger():
    return core.Ledger()


def _trace(**kw: Any) -> None:
    """One JSON line per decision under <home>/cache/user-steps/trace.jsonl (kept to ~2000 lines)."""
    try:
        import json
        import time

        p = core.home() / "cache" / "user-steps" / "trace.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        if p.exists() and p.stat().st_size > 1_000_000:
            p.write_text("\n".join(p.read_text().splitlines()[-2000:]) + "\n")
        with p.open("a") as f:
            f.write(json.dumps({"t": round(time.time(), 3), "pid": os.getpid(), **kw}, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _framed(session_id: str) -> bool:
    """Only the Desktop renders `::user-step`; CLI/TUI/messaging chats keep plain text."""
    forced = os.environ.get("HERMES_USER_STEPS_FRAME")
    if forced is not None:
        return forced == "1"
    return core.session_source(session_id) == "desktop"


# ---- hooks ---------------------------------------------------------------------------------------------

def pre_llm_call(session_id: str = "", user_message: str = "", platform: str = "", parent_session_id: str = "",
                 **_: Any) -> dict | None:
    if not _eligible(session_id, platform, parent_session_id):
        return None
    try:
        _USER_MSG[session_id] = user_message if isinstance(user_message, str) else ""
        opened = _ledger().open_steps(session_id)
        return {"context": core.reminder(opened)} if opened else None
    except Exception:
        log.exception("user-steps pre_llm_call failed")
        return None


def transform_llm_output(response_text: str = "", session_id: str = "", platform: str = "", **_: Any):
    if not isinstance(response_text, str) or not response_text.strip():
        return None
    if not _eligible(session_id, platform):
        return None
    try:
        out = core.process_reply(
            response_text, session_id, user_msg=_USER_MSG.pop(session_id, ""), llm=aux_llm, ledger=_ledger(),
            cards=cards_mod.HermesCards(), framed=_framed(session_id),
            session_ref=f"@session:{_profile()}/{session_id}")
        if out.get("error"):
            log.warning("user-steps extractor fell back to regex: %s", out["error"])
        if out.get("dropped"):
            log.info("user-steps dropped %d terminal-doable step(s): %s", len(out["dropped"]), out["dropped"])
        _trace(hook="transform", session=session_id, platform=platform, source=out.get("source"),
               new=[s["id"] for s in out.get("steps", [])], resolved=out.get("resolved"),
               framed=out.get("text") is not None, error=out.get("error"),
               dropped_terminal=out.get("dropped") or None)
        return out.get("text")
    except Exception as exc:
        _trace(hook="transform", session=session_id, platform=platform, exception=repr(exc)[:300])
        log.exception("user-steps transform failed")
        return None  # fail open: the reply is shown unchanged


def register(ctx) -> None:
    ctx.register_hook("pre_llm_call", pre_llm_call)
    ctx.register_hook("transform_llm_output", transform_llm_output)

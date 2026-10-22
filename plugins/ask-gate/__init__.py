"""ask-gate: make it mechanically hard to ask David for something without checking first.

Layers (see README):
  L1 pre_llm_call        fresh block: status of every card linked to this chat + asks David already answered
  L2 pre_final_gate      judge every ask in a final reply BEFORE it is shown; hold + private nudge, or deliver
  L3 pre_tool_call       kanban_create(blocked) / kanban_block / "needs David" bodies need a verified `Checked:` line;
                         workers' kanban_complete / kanban_block handoffs are judged and leftovers become FYI lines
  L4 answered-asks ledger (sqlite, ~/.hermes/state) filled when David's message answers or dismisses an ask
Every hook fails open: a broken gate must never break a chat.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any

try:
    from . import core, handoff, judge as judge_mod
except ImportError:  # pytest/spec loading imports the hyphenated plugin dir without a parent package
    import importlib.util as _ilu
    import sys as _sys

    def _sib(name: str):
        key = "ask_gate_" + name
        if key in _sys.modules:
            return _sys.modules[key]
        spec = _ilu.spec_from_file_location(key, Path(__file__).with_name(name + ".py"))
        mod = _ilu.module_from_spec(spec)  # type: ignore[arg-type]
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
        return _sys.modules.setdefault(key, mod)
    core, judge_mod, handoff = _sib("core"), _sib("judge"), _sib("handoff")

SKIP_PLATFORMS = {"cron", "kanban", "webhook", "subagent"}
DEFAULT_BOT_PROFILES = ("bb-tuner", "coach")  # cron profiles whose reports go to David and are judged like a chat reply
_SUBAGENTS: set[str] = set()                  # sessions seen with a parent_session_id (delegate_task children)
MAX_NUDGES = 2
LOG = core.TurnLog()
_LEDGER: dict[str, Any] = {}
_JUDGED: dict[tuple, list] = {}  # (session, text hash) -> verdicts, so a re-asked judge call is not repeated


def _off() -> bool:
    return bool(os.environ.get("ASK_GATE_OFF"))


def _is_worker() -> bool:
    return bool(os.environ.get("HERMES_KANBAN_TASK"))


def _chat_eligible(session_id: str, platform: str = "") -> bool:
    return bool(session_id) and not _off() and not _is_worker() and (platform or "").lower() not in SKIP_PLATFORMS


def _bot_profiles() -> tuple[str, ...]:
    """Profiles whose cron runs are gated: ASK_GATE_BOT_PROFILES (comma list), else config ``ask_gate.bot_profiles``."""
    env = os.environ.get("ASK_GATE_BOT_PROFILES")
    if env is not None:
        return tuple(x.strip() for x in env.split(",") if x.strip())
    try:
        from hermes_cli.config import load_config_readonly  # type: ignore
        got = (load_config_readonly().get("ask_gate") or {}).get("bot_profiles")
        if isinstance(got, (list, tuple)):
            return tuple(str(x) for x in got)
    except Exception:
        pass
    return DEFAULT_BOT_PROFILES


def _profile() -> str:
    env = os.environ.get("ASK_GATE_PROFILE")
    if env:
        return env
    try:
        from hermes_cli.profiles import current_profile_name  # type: ignore
        return current_profile_name("default") or "default"
    except Exception:
        pass
    for k in ("HERMES_PROFILE_NAME", "HERMES_PROFILE"):
        if os.environ.get(k):
            return os.environ[k]
    parts = core.home().parts
    return parts[parts.index("profiles") + 1] if "profiles" in parts and parts.index("profiles") + 1 < len(parts) else "default"


def _gate_eligible(session_id: str, platform: str = "") -> bool:
    """L2 runs for chats, and for platform-cron runs of a listed bot profile. Kanban workers, subagents and every
    other cron job stay skipped (core also runs this hook for them because it is registered include_background)."""
    if not session_id or _off() or _is_worker() or session_id in _SUBAGENTS:
        return False
    if (platform or "").lower() == "cron":
        return _profile() in _bot_profiles()
    return _chat_eligible(session_id, platform)


def ledger() -> "core.Ledger":
    path = os.environ.get("ASK_GATE_LEDGER") or str(core.home() / "state" / "ask-ledger.sqlite")
    if _LEDGER.get("path") != path:
        _LEDGER.update(path=path, obj=core.Ledger(Path(path)))
    return _LEDGER["obj"]


# ---- the model call ------------------------------------------------------------------------------------
def _model_cfg() -> tuple[str | None, str | None]:
    prov, model = os.environ.get("ASK_GATE_PROVIDER"), os.environ.get("ASK_GATE_MODEL")
    if prov or model:
        return prov, model
    try:  # an explicit auxiliary.ask_gate block in config.yaml wins; otherwise a cheap Claude model
        from hermes_cli.config import load_config_readonly  # type: ignore
        cfg = (load_config_readonly().get("auxiliary") or {}).get("ask_gate") or {}
        if cfg.get("model") or (cfg.get("provider") and cfg.get("provider") != "auto"):
            return None, None
    except Exception:
        pass
    return "anthropic", "claude-haiku-5-5"


def default_llm(system: str, user: str) -> str:
    prov, model = _model_cfg()
    return judge_mod.aux_llm(system, user, provider=prov, model=model)


LLM = default_llm  # tests replace this


def _llm():
    return LLM


# ---- verdict handoff to open-asks (it must not file a card for an unverified ask) ----------------------
def _verdict_path() -> Path:
    return core.home() / "cache" / "ask-gate" / "verdicts.json"


def record_verdict(session_id: str, verdict: str, text: str) -> None:
    p = _verdict_path()
    try:
        d = json.loads(p.read_text())
    except (OSError, ValueError):
        d = {}
    d[session_id] = {"t": time.time(), "verdict": verdict, "h": core.text_hash(text)}
    d = {k: v for k, v in d.items() if time.time() - v.get("t", 0) < 86400}
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(d))
        os.replace(tmp, p)
    except OSError:
        pass


def last_verdict(session_id: str, max_age_s: float = 300) -> str | None:
    try:
        v = json.loads(_verdict_path().read_text()).get(session_id)
    except (OSError, ValueError):
        return None
    return v["verdict"] if v and time.time() - v.get("t", 0) <= max_age_s else None


# ---- shared: judge a set of ask sentences -------------------------------------------------------------
def _user_msgs(session_id: str) -> list[str]:
    msgs = LOG.users(session_id)
    if msgs:
        return msgs
    try:
        import sqlite3
        c = core._ro_connect(core.home() / 'state.db', timeout=2)
        rows = c.execute("select content from messages where session_id=? and role='user' order by id desc limit 30",
                         (session_id,)).fetchall()
        c.close()
        return [r[0] for r in reversed(rows) if isinstance(r[0], str)]
    except Exception:
        return []


def run_judge(session_id: str, asks: list[str], tools: list[dict], reply: str = "") -> list[dict]:
    ids = core.card_ids(" ".join(asks) + " " + reply)
    statuses = core.card_status(ids)
    led = ledger().recent(14 * 86400, limit=20)
    ledger_text = "\n".join(f"- {a['topic']} -> {a['answer']} ({core.fmt_ts(a['ts'])})" for a in led)
    return judge_mod.judge(asks, tools_text=core.render_tools(tools), user_msgs=_user_msgs(session_id),
                           card_status=statuses, ledger_text=ledger_text, caps=core.capability_sheet(),
                           reply=reply, llm=_llm(), ledger=led,
                           soft=[core.is_soft_ask(a) for a in asks])


# ---- L1 + L4 ------------------------------------------------------------------------------------------
_WRAP = re.compile(r"\[OUT-OF-BAND USER MESSAGE[^\]]*\]\s*(.*?)\s*\[/OUT-OF-BAND USER MESSAGE\]", re.S)


def _human_text(msg: str) -> str | None:
    """The part of a 'user' message David actually typed (None for tool notices / kanban completion pings)."""
    m = _WRAP.search(msg or "")
    if m:
        return m.group(1)
    s = (msg or "").strip()
    if not s or s.startswith("[IMPORTANT:") or re.match(r"^[✔✖⚠]\s*\[", s) or s.startswith("[SYSTEM"):
        return None
    return s


def pre_llm_call(session_id: str = "", user_message: str = "", conversation_history: list | None = None,
                 platform: str = "", parent_session_id: str = "", **_: Any) -> dict | None:
    if parent_session_id and session_id:
        _SUBAGENTS.add(session_id)
    if not _chat_eligible(session_id, platform) or parent_session_id:
        return None
    _prewarm()
    try:
        LOG.reset(session_id)
        hist = [m.get("content") for m in (conversation_history or []) if isinstance(m, dict) and m.get("role") == "user"
                and isinstance(m.get("content"), str)]
        users = [t for t in (_human_text(h) for h in hist + [user_message or ""]) if t]
        LOG.set_users(session_id, users)
        _record_answers(session_id, _human_text(user_message or ""), conversation_history or [])
        block = core.l1_block(session_id, ledger())
        return {"context": block} if block else None
    except Exception as e:  # never break a chat
        core.log_event("l1_error", error=repr(e))
        return None


def _last_assistant_text(history: list) -> str:
    for m in reversed(history):
        if isinstance(m, dict) and m.get("role") == "assistant" and isinstance(m.get("content"), str) and m["content"].strip():
            return m["content"]
    return ""


def _record_answers(session_id: str, msg: str | None, history: list) -> None:
    if not msg:
        return
    led = ledger()
    open_asks = led.open_asks(session_id)
    strong = core.is_strong_dismissal(msg)
    if not open_asks:
        open_asks = core.find_asks(_last_assistant_text(history))
    if not open_asks and not strong:
        return
    if not (strong or core.looks_like_answer(msg)):
        return
    answered: list[dict] = []
    try:
        answered = judge_mod.classify_answer(msg, open_asks, _llm())
    except Exception as e:
        core.log_event("l4_classify_error", error=repr(e))
    if not answered and strong:  # model down or silent: a strong dismissal is still recorded
        topic = (open_asks[0] if open_asks else _last_assistant_text(history)[:160]) or "asks in general"
        answered = [{"i": -1, "topic": topic, "answer": msg[:160]}]
    for a in answered:
        led.add(a["topic"], a["answer"], session=session_id)
    if answered:
        led.set_open_asks(session_id, [])
        core.log_event("l4_answered", session=session_id, n=len(answered))


# ---- tool log -----------------------------------------------------------------------------------------
def post_tool_call(tool_name: str = "", args: Any = None, result: Any = None, session_id: str = "", **_: Any) -> None:
    if session_id:
        LOG.add(session_id, tool_name, args, result)


# ---- L2 -----------------------------------------------------------------------------------------------
def pre_final_gate(session_id: str = "", platform: str = "", model: str = "", final_response: str = "",
                   turn_messages: list | None = None, attempt: int = 0, **_: Any) -> dict | None:
    if not _gate_eligible(session_id, platform) or not isinstance(final_response, str):
        return None
    try:
        users = _user_msgs(session_id)
        howto = bool(users) and core.asked_how_to(users[-1])
        held = None if howto else _handoff_gate(session_id, final_response, turn_messages, attempt)
        if held is not None:
            return held
        asks = core.find_asks(final_response, howto=howto)
        if not asks:
            return None
        tools = core.merge_tool_logs(core.tools_from_messages(turn_messages or []), LOG.get(session_id))
        key = (session_id, core.text_hash(final_response))
        verdicts = _JUDGED.get(key)
        if verdicts is None:
            verdicts = run_judge(session_id, asks, tools, final_response)
            _JUDGED[key] = verdicts
            if len(_JUDGED) > 200:
                _JUDGED.pop(next(iter(_JUDGED)))
        bad = judge_mod.bad(verdicts)
        core.log_event("verdict", session=session_id, attempt=attempt, asks=asks,
                       verdicts=[(v["verdict"], v.get("reason", "")) for v in verdicts])
        if not bad:
            ledger().set_open_asks(session_id, asks)
            record_verdict(session_id, "ok", final_response)
            return None
        if attempt < MAX_NUDGES:
            record_verdict(session_id, "held", final_response)
            return {"hold": True, "nudge": judge_mod.nudge(asks, verdicts)}
        record_verdict(session_id, "unverified", final_response)
        ledger().set_open_asks(session_id, [asks[v["i"]] for v in verdicts if v["verdict"] == "ok"])
        return {"append": judge_mod.delivery_note(asks, verdicts)}
    except Exception as e:  # judge down: deliver, log, never break a chat
        core.log_event("gate_error", session=session_id, error=repr(e))
        return None


def _handoff_gate(session_id: str, final_response: str, turn_messages: list | None, attempt: int) -> dict | None:
    """A reply that hands a terminal/password step to David (\"run `sudo ...` on the mini\", \"add the key to
    authorized_keys\") is refused unless this turn already opened a terminal for him: the agent must call
    run_in_user_terminal itself. Deterministic (no model call), so it holds even when the judge is down."""
    hits = handoff.find_handoffs(final_response)
    if not hits:
        return None
    tools = core.merge_tool_logs(core.tools_from_messages(turn_messages or []), LOG.get(session_id))
    if handoff.opened_terminal(tools):
        core.log_event("handoff_ok", session=session_id, hits=hits[:3])
        return None
    core.log_event("handoff_held", session=session_id, attempt=attempt, hits=hits[:3])
    if attempt < MAX_NUDGES:
        record_verdict(session_id, "held", final_response)
        return {"hold": True, "nudge": handoff.gate_nudge(hits)}
    return None  # nudged twice already: deliver (the normal ask judge still runs) rather than loop


# core may buffer streamed final text while this hook is registered, so a held draft never reaches the screen
pre_final_gate.needs_buffer = True  # type: ignore[attr-defined]
# core skips gate hooks in cron/kanban/subagent runs unless they opt in; _gate_eligible then lets only bot crons through
pre_final_gate.include_background = True  # type: ignore[attr-defined]


# ---- L3 -----------------------------------------------------------------------------------------------
CHECK_MSG = (
    "ask-gate: refused. This {what} says David has to act or decide, but it has no verified `Checked:` line. "
    "Check the premise first with a tool call in THIS turn (kanban_show for a card, the CLI/API that proves the "
    "state, token-broker scopes for a permission), then add a line `Checked: <tool or command you ran> — <one line "
    "of its output>` and retry. If a token-broker minter scope, ssh host or signed-in browser can do it, do it "
    "instead of asking. {extra}")
_WORKER_NO_ASK_KINDS = {"dependency", "time", "transient"}


def _tools_now(session_id: str) -> list[dict]:
    return LOG.get(session_id)


def pre_tool_call(tool_name: str = "", args: Any = None, session_id: str = "", **_: Any) -> dict | None:
    if _off() or not isinstance(args, dict):
        return None
    try:
        if tool_name == "kanban_complete" and _is_worker():
            return _worker_complete(session_id, args)
        if tool_name not in ("kanban_create", "kanban_block"):
            return None
        if tool_name == "kanban_block" and str(args.get("kind") or "") in _WORKER_NO_ASK_KINDS:
            return None
        gated, text = core.needs_checked(tool_name, args, _is_worker())
        if not gated or "Filed automatically by the `open-asks` plugin" in text:
            return None
        what = "blocked card" if tool_name == "kanban_create" else "block"
        tools = _tools_now(session_id)
        claim = core.checked_line(text)
        if not claim:
            return {"action": "block", "message": CHECK_MSG.format(what=what, extra="(No `Checked:` line found.)")}
        if not core.checked_matches(claim, tools):
            return {"action": "block", "message": CHECK_MSG.format(
                what=what, extra=f"(Your Checked line \"{claim[:100]}\" matches no tool call made this turn.)")}
        sents = core.find_leftovers(text) + [a for a in core.find_asks(text) if a not in core.find_leftovers(text)]
        sents = sents or [str(args.get("title") or text)[:200]]
        verdicts = run_judge(session_id, sents[:6], tools, text)
        core.log_event("l3_verdict", tool=tool_name, session=session_id,
                       verdicts=[(v["verdict"], v.get("reason", "")) for v in verdicts])
        if judge_mod.bad(verdicts):
            return {"action": "block", "message": "ask-gate: refused after the judge read the card. "
                    + judge_mod.nudge(sents, verdicts).split("\n", 1)[1]}
        return None
    except Exception as e:
        core.log_event("l3_error", error=repr(e))
        return None


FYI_PREFIX = "FYI (unverified, not an ask for David): "


def _worker_complete(session_id: str, args: dict) -> dict | None:
    summary = str(args.get("summary") or args.get("result") or "")
    left = core.find_leftovers(summary)
    if not left:
        return None
    tools = _tools_now(session_id)
    verdicts = run_judge(session_id, left[:8], tools, summary)
    keep_ok = {left[v["i"]] for v in verdicts if v["verdict"] == "ok"}
    # an ask is only left standing if the judge approved it AND the handoff carries a matching Checked line
    claim = core.checked_line(summary)
    if claim and core.checked_matches(claim, tools):
        pass
    else:
        keep_ok = set()
    out = summary
    changed = False
    for s in left:
        if s in keep_ok or FYI_PREFIX in s:
            continue
        out = out.replace(s, FYI_PREFIX + s, 1)
        changed = True
    core.log_event("worker_handoff", session=session_id, leftovers=left, rewritten=changed)
    if not changed:
        return None
    key = "summary" if args.get("summary") else "result"
    return {"action": "modify", "args": {key: out}}


_PREWARMED = {"done": False}


def _prewarm() -> None:
    """The first aux call of a process can take 20+ s (client + credential-pool setup); core gives a gate hook 30 s.
    Pay that cost once, in the background, from the first chat turn: it must run inside the agent's context
    (profile-scoped secrets are context-bound), so the thread runs a copy of the caller's contextvars."""
    if _PREWARMED["done"] or _off() or _is_worker() or os.environ.get("ASK_GATE_NO_PREWARM"):
        return
    _PREWARMED["done"] = True
    import contextvars
    import threading

    def go():
        try:
            prov, model = _model_cfg()
            judge_mod.aux_llm("Reply with the single word ok.", "ok", timeout=40.0, max_tokens=5, provider=prov, model=model)
        except Exception as e:
            core.log_event("prewarm_error", error=repr(e)[:300])
    ctx = contextvars.copy_context()
    threading.Thread(target=ctx.run, args=(go,), name="ask-gate-prewarm", daemon=True).start()


def register(ctx) -> None:
    ctx.register_hook("pre_llm_call", pre_llm_call)
    ctx.register_hook("post_tool_call", post_tool_call)
    ctx.register_hook("pre_tool_call", pre_tool_call)
    try:
        ctx.register_hook("pre_final_gate", pre_final_gate)
    except Exception:  # older core without the hook: L1/L3/L4 still work
        core.log_event("no_pre_final_gate")

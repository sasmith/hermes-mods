"""slack-workspace — give a Hermes Slack bot workspace awareness and safe use of its scopes.

* Observes every Events API payload the bot receives (Socket Mode middleware) into a per-profile
  SQLite store, including messages it was not @-mentioned in.
* ``pre_llm_call``: on a Slack turn in a channel, injects what was said in that channel since this
  session last looked (the adapter already supplies the current thread).
* Tools (toolset ``slack_workspace``): ``slack_recent``, ``slack_join``, ``slack_api``,
  ``slack_request``. Methods are tiered read / act / admin. Reads of private conversations only from
  inside them (or by an admin); acts outside the current conversation and admin methods need an
  admin, or an approval request that DMs the admins one message with checkboxes and
  Approve selected / Approve all + always allow / Deny buttons.

Outside the Slack gateway process (Desktop/CLI/cron chats of a Slack bot profile) there is no adapter: the tools
then use the profile's own ``SLACK_BOT_TOKEN`` (env, else ``<HERMES_HOME>/.env``) and the operator is trusted.

Admins = ``platforms.slack.extra.allow_admin_from`` (plus ``admins`` in policy.json). Turns from
non-Slack surfaces (desktop, CLI, cron) are the operator and are trusted.
State: ``<HERMES_HOME>/slack-workspace/{observed.sqlite,policy.json}``.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import re
import threading
import time
from pathlib import Path
from typing import Any, Optional

if __package__:
    from . import core, engage, follow, ownerops
else:  # imported outside a package (e.g. pytest collecting this dir)
    import importlib.util as _ilu
    import sys as _sys

    def _sib(name, file):
        mod = _sys.modules.get(name)
        if mod is None:
            spec = _ilu.spec_from_file_location(name, Path(__file__).with_name(file))
            mod = _ilu.module_from_spec(spec)  # type: ignore[arg-type]
            _sys.modules[name] = mod
            spec.loader.exec_module(mod)  # type: ignore[union-attr]
        return mod
    core = _sib("slack_workspace_core", "core.py")
    engage = _sib("slack_workspace_engage", "engage.py")
    follow = _sib("slack_workspace_follow", "follow.py")
    ownerops = _sib("slack_workspace_ownerops", "ownerops.py")

logger = logging.getLogger(__name__)

TOOLSET = "slack_workspace"
MAX_RESULT_CHARS = 14000
CONTEXT_MAX_CHARS = 6000
CONTEXT_FIRST_LOOKBACK_S = 3 * 86400


def _session(name: str) -> str:
    try:
        from gateway.session_context import get_session_env
        return get_session_env("HERMES_SESSION_" + name, "") or ""
    except Exception:
        return ""


class State:
    def __init__(self, root: Path):
        self.store = core.Store(root)
        self.engs = engage.Engagements(self.store)
        self.runner: Any = None
        self.ownerops: Any = None
        self.adapter: Any = None
        self.native: Any = None
        self._chan_list: tuple[float, list] = (0.0, [])
        self._lock = threading.Lock()

    # ---- identity / auth ---------------------------------------------------------------------
    def token(self, team: str = "") -> str:
        a = self.adapter
        if a is None:
            tok = self.standalone_token()
            if tok:
                return tok
            raise RuntimeError("Slack is not connected in this profile yet")
        clients = getattr(a, "_team_clients", None) or {}
        client = clients.get(team) if team else None
        if client is None and clients:
            client = next(iter(clients.values()))
        if client is None and self.native is not None:
            client = getattr(self.native, "client", None)
        tok = getattr(client, "token", None)
        if not tok:
            raise RuntimeError("no Slack bot token available on the adapter")
        return tok

    def standalone_token(self) -> str:
        """Bot token for processes without the Slack adapter (Desktop/CLI/cron chats of a Slack bot profile):
        this profile's own ``SLACK_BOT_TOKEN`` (env, else ``<HERMES_HOME>/.env``). Empty if none."""
        import os
        tok = (os.environ.get("SLACK_BOT_TOKEN") or "").strip()
        if not tok:
            tok = follow.read_env_value(self.store.root.parent / ".env", "SLACK_BOT_TOKEN")
        return tok if tok.startswith("xoxb-") else ""

    def available(self) -> bool:
        return self.adapter is not None or bool(self.standalone_token())

    def api(self, method: str, params: Optional[dict] = None) -> dict:
        return core.call(self.token(_session("SCOPE_ID")), method, params)

    def bot_user_id(self) -> str:
        if self.adapter is None:
            if not getattr(self, "_standalone_uid", ""):
                try:
                    self._standalone_uid = str(self.api("auth.test").get("user_id") or "")
                except Exception:
                    return ""
            return self._standalone_uid
        return str(getattr(self.adapter, "_bot_user_id", "") or "")

    def admins(self) -> set[str]:
        out: set[str] = set()
        extra = getattr(getattr(self.adapter, "config", None), "extra", None) or {}
        for key in ("allow_admin_from", "group_allow_admin_from"):
            v = extra.get(key) or []
            out |= set(v.split(",") if isinstance(v, str) else v)
        out |= set(self.store.policy().get("admins") or [])
        return {x.strip() for x in out if x and x.strip()}

    # ---- requester context ---------------------------------------------------------------------
    def requester(self) -> dict:
        platform = _session("PLATFORM")
        user = _session("USER_ID")
        on_slack = platform == "slack"
        return {"platform": platform, "user": user, "channel": _session("CHAT_ID") if on_slack else "",
                "thread": _session("THREAD_ID") if on_slack else "",
                "trusted": (not on_slack) or user in self.admins()}

    # ---- names -----------------------------------------------------------------------------------
    def channel_info(self, cid: str) -> dict:
        hit = self.store.cached(cid)
        if hit:
            return {"id": cid, "name": hit["name"], "private": bool(hit["private"]),
                    **json.loads(hit["extra"] or "{}")}
        try:
            c = self.api("conversations.info", {"channel": cid})["channel"]
        except core.SlackError:
            return {"id": cid, "name": cid, "private": cid[:1] in ("D", "G"), "unknown": True}
        private = bool(c.get("is_private") or c.get("is_im") or c.get("is_mpim"))
        name = c.get("name") or (("dm:" + c.get("user", "")) if c.get("is_im") else cid)
        extra = {"is_im": bool(c.get("is_im")), "is_mpim": bool(c.get("is_mpim")),
                 "ext_shared": bool(c.get("is_ext_shared"))}
        self.store.cache_name(cid, name, "channel", private, extra)
        return {"id": cid, "name": name, "private": private, **extra}

    def user_name(self, uid: str) -> str:
        if not uid:
            return "?"
        hit = self.store.cached(uid)
        if hit:
            return hit["name"]
        try:
            u = self.api("users.info", {"user": uid})["user"]
            p = u.get("profile") or {}
            name = p.get("display_name") or u.get("real_name") or u.get("name") or uid
            self.store.cache_name(uid, name, "user", False,
                                  {"bot": bool(u.get("is_bot")), "team": u.get("team_id"),
                                   "guest": bool(u.get("is_restricted") or u.get("is_ultra_restricted"))})
            return name
        except core.SlackError:
            return uid

    def render_text(self, text: str) -> str:
        text = re.sub(r"<@([UW][A-Z0-9]+)(\|[^>]*)?>", lambda m: "@" + self.user_name(m.group(1)), text or "")
        text = re.sub(r"<#([CG][A-Z0-9]+)\|([^>]+)>", r"#\2", text)
        text = re.sub(r"<#([CG][A-Z0-9]+)\|?>", lambda m: "#" + self.channel_info(m.group(1)).get("name", m.group(1)),
                      text)
        text = re.sub(r"<!(here|channel|everyone)>", r"@\1", text)
        text = re.sub(r"<(https?://[^|>]+)\|([^>]+)>", r"\2 (\1)", text)
        return html.unescape(text)

    def public_channels(self, max_age: float = 600) -> list[dict]:
        with self._lock:
            at, chans = self._chan_list
            if chans and time.time() - at < max_age:
                return chans
        out, cursor = [], None
        while True:
            d = self.api("conversations.list", {"types": "public_channel", "exclude_archived": "true",
                                                "limit": 1000, "cursor": cursor})
            out += d.get("channels", [])
            cursor = (d.get("response_metadata") or {}).get("next_cursor")
            if not cursor:
                break
        for c in out:
            self.store.cache_name(c["id"], c["name"], "channel", False, {})
        with self._lock:
            self._chan_list = (time.time(), out)
        return out

    def can_read(self, cid: str, who: dict) -> tuple[bool, str]:
        if not cid or who["trusted"] or cid == who["channel"]:
            return True, ""
        info = self.channel_info(cid)
        if info.get("private"):
            return False, ("that is a private conversation; its content can only be read from inside it "
                           "or by an admin")
        return True, ""


STATE: dict[str, State] = {}


# =================================================================================================
# Observation
# =================================================================================================

def _owner_ops(state: State):
    if state.ownerops is None:
        home = state.store.root.parent
        state.ownerops = ownerops.OwnerOps(
            state.store.root, owners=lambda: ownerops.owner_ids(ownerops.home_cfg(home)), bot_home=home,
            profile=lambda: ownerops.ops_profile(ownerops.home_cfg(home)),
            api=lambda m, p=None: core.call(state.token(), m, p))
    return state.ownerops


def _route_event(state: State, ev: dict) -> bool:
    """Record + route one event. True = it belongs to an engagement or to the owner's ops profile
    (ambient bot must not see it)."""
    try:
        if _owner_ops(state).route(ev):
            logger.info("mac-ops: owner DM %s ts=%s -> ops profile", ev.get("channel"), ev.get("ts"))
            return True
    except Exception as exc:  # routing must never break dispatch
        logger.warning("mac-ops routing failed: %s", exc)
    r = engage.route(ev, state.engs, bot_user_id=state.bot_user_id())
    if r.decision != "engaged":
        return False
    if r.trigger and r.engagement:
        lag = time.time() - float(ev.get("ts") or time.time())
        logger.info("slack-follow: %s ts=%s -> follower (event lag %.1fs)", r.engagement["id"], ev.get("ts"), lag)
        state.engs.update(r.engagement["id"], last_activity=time.time())
        threading.Thread(target=follow.ack_if_addressed, daemon=True, name="slack-follow-ack",
                         args=(state.store, lambda m, p=None: core.call(state.token(), m, p), ev, state.bot_user_id())).start()
        if state.runner is not None:
            state.runner.kick(r.engagement["id"])
    return True


def _ack_response():
    """A 200 for bolt to ack the envelope with. bolt does not inject ``resp`` into global middleware,
    and returning None (no next()) becomes a 404: no Socket Mode ack, and Slack redelivers the event
    after 3 s, 1 min and 5 min (and too many failures disable Event Subscriptions)."""
    try:
        from slack_bolt.response import BoltResponse
        return BoltResponse(status=200, body="")
    except Exception:  # bolt missing (unit tests)
        import types
        return types.SimpleNamespace(status=200, body="", headers={})


def _make_factory(state: State):
    def slack_workspace_wire(native, adapter):
        state.native, state.adapter = native, adapter

        async def _observe(body, next):  # noqa: A002 - slack_bolt injects by name
            engaged = False
            try:
                if isinstance(body, dict) and body.get("type") == "event_callback":
                    ev = body.get("event") or {}
                    # Inline (a few ms of sqlite), not asyncio.to_thread: a busy default executor
                    # delayed the ack past Slack's 3 s deadline and Slack redelivered the event.
                    t0 = time.time()
                    state.store.record_event(ev, body.get("team_id") or "")
                    engaged = _route_event(state, ev)
                    if engaged:
                        logger.info("slack-follow: event %s (%s) swallowed in %.3fs", body.get("event_id"),
                                    ev.get("type"), time.time() - t0)
            except Exception as exc:  # never break dispatch
                logger.debug("slack-workspace observe failed: %s", exc)
            if engaged:  # the follower owns this channel/thread: ack (200) and stop here
                return _ack_response()
            await next()

        native.use(_observe)
        try:
            state.store.prune()
        except Exception:
            pass
        if state.runner is None:
            state.runner = _make_runner(state)
            state.runner.start()
        logger.info("slack-workspace: observing Slack events for this profile")
    return slack_workspace_wire


def _make_runner(state: State):
    try:
        from hermes_constants import get_default_hermes_root
        root_home = Path(get_default_hermes_root())
    except Exception:
        root_home = state.store.root.parent
    fcfg = state.store.policy().get("follower") or {}

    def _home(profile):
        return root_home / "profiles" / profile if profile else root_home

    def inbox_post(title, body, accept_prompt, key=None, profile=None, type=None, fields=None, **_):
        return _post_typed(_inbox_for(_home(profile)), title, body, accept_prompt, key=key, poster="slack follower",
                           type=type, fields=fields)

    def inbox_outcome(key, profile=None):
        return _inbox_for(_home(profile)).outcome(key)

    return follow.Runner(state.store, state.engs, token=state.token, bot_user_id=state.bot_user_id,
                         name=state.user_name, root_home=root_home, inbox_post=inbox_post,
                         inbox_outcome=inbox_outcome, self_gateway=fcfg.get("self_gateway") or "",
                         policy=state.store.policy)


def _post_typed(inbox, title, body, accept_prompt, *, key=None, poster=None, type=None, fields=None):
    """Post an Inbox card of card type *type* (the slack-workspace-bot skill's inbox-types/). A host whose inbox
    plugin predates card types, or whose skills dir lacks the type, gets the plain Do it / Archive card."""
    if type:
        try:
            return inbox.post(title, body, accept_prompt, key=key, poster=poster, type=type, fields=fields)
        except TypeError:  # an inbox store without card types
            pass
        except ValueError as exc:  # unknown/invalid type on this host; any other ValueError is a real error
            if "type" not in str(exc).lower():
                raise
            logger.info("slack-workspace: card type %s unavailable (%s); posting a plain card", type, exc)
    return inbox.post(title, body, accept_prompt, key=key, poster=poster)


def _inbox_for(home: Path):
    """The inbox plugin's store bound to *home* (a profile's HERMES_HOME), next to this plugin."""
    import importlib.util as ilu
    import sys as _s
    path = Path(__file__).resolve().parent.parent / "inbox" / "store.py"
    name = "_slack_workspace_inbox_store"
    home = Path(home)
    if not path.exists():
        try:
            from hermes_constants import get_default_hermes_root
            alt = Path(get_default_hermes_root()) / "plugins" / "inbox" / "store.py"
        except Exception:
            alt = home / "plugins" / "inbox" / "store.py"
        path = alt
    mod = _s.modules.get(name)
    if mod is None:
        if not path.exists():
            raise RuntimeError("the inbox plugin is not installed (needed for escalations)")
        spec = ilu.spec_from_file_location(name, path)
        mod = ilu.module_from_spec(spec)  # type: ignore[arg-type]
        _s.modules[name] = mod
        spec.loader.exec_module(mod)  # type: ignore[union-attr]

    def db_factory():
        from hermes_state import SessionDB
        return SessionDB(db_path=home / "state.db")
    return mod.Inbox(home=home, db_factory=db_factory)


# =================================================================================================
# Context injection
# =================================================================================================

def _fmt_lines(state: State, rows: list[dict], cur_thread: str = "") -> list[str]:
    lines = []
    bot = state.bot_user_id()
    for r in rows:
        who = "you" if bot and r["user"] == bot else (state.user_name(r["user"]) if r["user"]
                                                      else (r["bot"] or "bot"))
        t = time.strftime("%a %H:%M", time.localtime(float(r["ts"])))
        where = ""
        if r["thread_ts"] and r["thread_ts"] != r["ts"]:
            where = " (thread reply)" if r["thread_ts"] != cur_thread else " (this thread)"
        elif r.get("reply_count"):
            where = f" ({r['reply_count']} replies)"
        text = state.render_text(r["text"]).replace("\n", " ⏎ ")
        if len(text) > 500:
            text = text[:500] + "…"
        lines.append(f"[{t}] {who}{where}: {text}")
    return lines


def _make_context_hook(state: State):
    def pre_llm_call(**kw):
        if (kw.get("platform") or "") != "slack":
            return None
        cid, thread, msg = _session("CHAT_ID"), _session("THREAD_ID"), _session("MESSAGE_ID")
        if not cid or cid.startswith("D"):
            return None
        try:
            info = state.channel_info(cid)
            if core.matches(state.store.policy().get("quiet_channels") or [], cid, info.get("name", "")):
                return None
            key = f"{kw.get('session_id')}:{cid}"
            wm = state.store.watermark(key)
            since = float(wm) if wm else time.time() - CONTEXT_FIRST_LOOKBACK_S
            rows = state.store.recent(cid, since, limit=60, exclude_ts={msg} if msg else ())
            if thread:  # the adapter already injects the current thread
                rows = [r for r in rows if (r["thread_ts"] or r["ts"]) != thread]
            newest = max([r["ts"] for r in rows] + ([msg] if msg else []) + ([wm] if wm else []),
                         key=float, default=None)
            if newest:
                state.store.set_watermark(key, newest)
            if not rows:
                return None
            lines = _fmt_lines(state, rows, thread)
            while lines and sum(len(x) + 1 for x in lines) > CONTEXT_MAX_CHARS:
                lines.pop(0)
            head = ("Recent activity in #%s you observed (not addressed to you; for context only, "
                    "do not reply to it unless asked)%s:" % (
                        info.get("name", cid), "" if wm else " — last 3 days"))
            return {"context": head + "\n" + "\n".join(lines)}
        except Exception as exc:
            logger.debug("slack-workspace context failed: %s", exc)
            return None
    return pre_llm_call


# =================================================================================================
# Approvals
# =================================================================================================

def _approval_blocks(state: State, req: dict) -> list[dict]:
    who = req["requester"]
    origin = req.get("origin") or {}
    where = f" in <#{origin['channel']}>" if origin.get("channel") else ""
    joins = all(op["method"] == "conversations.join" for op in req["ops"])
    blocks: list[dict] = [{"type": "section", "text": {"type": "mrkdwn", "text": (
        f"*{'<@' + who + '>' if who else 'The agent'}* asked me{where} to do {len(req['ops'])} thing(s)"
        f" that need your OK.\n>{(req.get('reason') or '(no reason given)')[:600]}")}}]
    ops = req["ops"]
    for start in range(0, len(ops), 10):
        chunk = ops[start:start + 10]
        options = [{"text": {"type": "mrkdwn", "text": core.op_label(op)[:150]}, "value": str(start + i)}
                   for i, op in enumerate(chunk)]
        blocks.append({"type": "actions", "block_id": f"sw_pick_{start}", "elements": [{
            "type": "checkboxes", "action_id": f"sw_pick_{start}", "options": options,
            "initial_options": options}]})
    always = ("Approve all + auto-approve future joins" if joins
              else "Approve all + always allow these methods")
    blocks.append({"type": "actions", "block_id": "sw_buttons", "elements": [
        {"type": "button", "action_id": "sw_approve", "style": "primary", "value": req["id"],
         "text": {"type": "plain_text", "text": "Approve selected"}},
        {"type": "button", "action_id": "sw_always", "value": req["id"],
         "text": {"type": "plain_text", "text": always}},
        {"type": "button", "action_id": "sw_deny", "style": "danger", "value": req["id"],
         "text": {"type": "plain_text", "text": "Deny"}}]})
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": f"request `{req['id']}`"}]})
    return blocks


def _send_request(state: State, req: dict) -> list[str]:
    sent = []
    blocks = _approval_blocks(state, req)
    for admin in sorted(state.admins()):
        try:
            dm = state.api("conversations.open", {"users": admin})["channel"]["id"]
            m = state.api("chat.postMessage", {"channel": dm, "blocks": blocks,
                                               "text": f"Approval needed: request {req['id']}"})
            req["dm"].append([dm, m["ts"]])
            sent.append(admin)
        except core.SlackError as exc:
            logger.warning("slack-workspace: could not DM admin %s: %s", admin, exc)
    state.store.save_request(req)
    return sent


def _execute(state: State, op: dict) -> dict:
    try:
        out = state.api(op["method"], op["params"])
        return {"op": core.op_label(op), "ok": True,
                "result": {k: out[k] for k in ("channel", "ts") if k in out and not isinstance(out[k], dict)}}
    except core.SlackError as exc:
        return {"op": core.op_label(op), "ok": False, "error": exc.error}


def _resolve(state: State, rid: str, admin: str, decision: str, picked: Optional[set[int]]) -> Optional[dict]:
    req = state.store.request(rid)
    if not req or req["status"] != "pending":
        return req
    ops = req["ops"]
    if decision == "deny":
        req["status"] = "denied"
    else:
        idx = set(range(len(ops))) if (decision == "always" or picked is None) else picked
        req["results"] = [_execute(state, ops[i]) for i in sorted(idx) if 0 <= i < len(ops)]
        req["status"] = "approved"
        if decision == "always":
            joins = [op for op in ops if op["method"] == "conversations.join"]
            others = sorted({op["method"] for op in ops if op["method"] != "conversations.join"})
            state.store.update_policy(**({"auto_join": ["*"]} if joins else {}),
                                      **({"auto_methods": others} if others else {}))
    req["decided_by"], req["decided_at"] = admin, time.time()
    state.store.save_request(req)
    # Update every admin's copy of the request.
    if req["status"] == "denied":
        summary = f"Denied by <@{admin}>."
    else:
        ok = [r["op"] for r in req["results"] if r["ok"]]
        bad = [f"{r['op']} ({r['error']})" for r in req["results"] if not r["ok"]]
        summary = f"Approved by <@{admin}>" + (" (and set to always allow)" if decision == "always" else "") + "."
        if ok:
            summary += "\n✅ " + "\n✅ ".join(ok)
        if bad:
            summary += "\n⚠️ " + "\n⚠️ ".join(bad)
    for dm, ts in req.get("dm") or []:
        try:
            state.api("chat.update", {"channel": dm, "ts": ts, "text": summary, "blocks": [
                {"type": "section", "text": {"type": "mrkdwn", "text": f"Request `{rid}`: {summary}"[:2900]}}]})
        except core.SlackError:
            pass
    origin = req.get("origin") or {}
    if origin.get("channel") and req.get("requester") != admin:
        try:
            state.api("chat.postMessage", {"channel": origin["channel"],
                                           "thread_ts": origin.get("thread") or None,
                                           "text": f"<@{req['requester']}> request `{rid}`: {summary}"[:3000]})
        except core.SlackError:
            pass
    return req


def _picked(body: dict) -> Optional[set[int]]:
    values = ((body.get("state") or {}).get("values") or {})
    found, picked = False, set()
    for block in values.values():
        for aid, v in block.items():
            if aid.startswith("sw_pick_") and v.get("type") == "checkboxes":
                found = True
                picked |= {int(o["value"]) for o in v.get("selected_options") or []}
    return picked if found else None


def _make_action_handler(state: State, decision: str):
    async def handler(ack, body, action):
        await ack()
        admin = ((body or {}).get("user") or {}).get("id", "")
        if admin not in state.admins():
            ch = ((body or {}).get("container") or {}).get("channel_id")
            if ch:
                await asyncio.to_thread(state.api, "chat.postEphemeral", {
                    "channel": ch, "user": admin, "text": "Only this bot's admins can decide requests."})
            return
        await asyncio.to_thread(_resolve, state, (action or {}).get("value", ""), admin, decision,
                                _picked(body or {}))
    return handler


async def _ack_only(ack, body, action):
    await ack()


# =================================================================================================
# Tools
# =================================================================================================

def _out(obj: Any) -> str:
    s = json.dumps(obj, ensure_ascii=False, default=str)
    if len(s) > MAX_RESULT_CHARS:
        s = s[:MAX_RESULT_CHARS] + f'… [truncated {len(s) - MAX_RESULT_CHARS} chars; narrow the query]'
    return s


def _resolve_channel(state: State, ref: str) -> Optional[dict]:
    ref = (ref or "").strip()
    if not ref:
        return None
    m = re.match(r"<#([CG][A-Z0-9]+)(\|[^>]*)?>", ref)
    if m:
        ref = m.group(1)
    name = ref.lstrip("#").lower()
    for c in state.public_channels():
        if c["id"] == ref or c["name"] == name:
            return c
    if re.fullmatch(r"[CGD][A-Z0-9]{6,}", ref):
        info = state.channel_info(ref)
        if info.get("unknown"):
            return None
        return {"id": ref, "name": info.get("name"), "is_private": info.get("private"),
                "is_member": None}
    return None


def tool_recent(state: State, args: dict, **_) -> str:
    who = state.requester()
    hours = float(args.get("hours") or 24)
    since = time.time() - hours * 3600
    ref = args.get("channel")
    if ref:
        c = _resolve_channel(state, ref)
        if not c:
            return _out({"error": f"unknown channel {ref!r}"})
        ok, why = state.can_read(c["id"], who)
        if not ok:
            return _out({"error": why})
        rows = state.store.recent(c["id"], since, limit=int(args.get("limit") or 150))
        if args.get("thread_ts"):
            rows = [r for r in rows if (r["thread_ts"] or r["ts"]) == args["thread_ts"]]
        out = {"channel": f"#{c.get('name')}", "id": c["id"], "messages": _fmt_lines(state, rows),
               "note": "only messages since the bot joined / since observation started are stored; "
                       "use slack_api conversations.history for older"}
        if args.get("include_events"):
            out["events"] = state.store.events_since(since, c["id"])
        return _out(out)
    summary, quiet = [], []
    quiet_pats = state.store.policy().get("quiet_channels") or []
    for s in state.store.activity_summary(since):
        info = state.channel_info(s["channel"])
        if info.get("private") and not (who["trusted"] or s["channel"] == who["channel"]):
            continue
        if core.matches(quiet_pats, s["channel"], info.get("name", "")):
            quiet.append("#" + info.get("name", s["channel"]))
            continue
        summary.append({"channel": ("#" + info["name"]) if not info.get("is_im") else info["name"],
                        "id": s["channel"], "messages": s["n"], "people": s["people"],
                        "last": time.strftime("%a %H:%M", time.localtime(float(s["last_ts"])))})
    return _out({"since_hours": hours, "active_channels": summary,
                 "quiet_channels_skipped": quiet,
                 "hint": "call again with channel=… to read one (quiet channels too)"})


def _request_or_run(state: State, who: dict, ops: list[dict], reason: str) -> dict:
    req = state.store.new_request(requester=who["user"], origin={"channel": who["channel"],
                                                                 "thread": who["thread"]},
                                  ops=ops, reason=reason)
    sent = _send_request(state, req)
    if not sent:
        return {"status": "blocked", "error": "needs admin approval but no admin could be DMed",
                "request_id": req["id"]}
    return {"status": "pending_approval", "request_id": req["id"],
            "waiting_on": [f"<@{a}>" for a in sent],
            "note": "The admins got one DM with approve/deny buttons; the requester is told in this "
                    "thread when it's decided. Check with slack_request."}


def tool_join(state: State, args: dict, **_) -> str:
    who = state.requester()
    pol = state.store.policy()
    refs = args.get("channels") or []
    if isinstance(refs, str):
        refs = [x for x in re.split(r"[\s,]+", refs) if x]
    if refs == ["*"] or args.get("all_public"):
        refs = [c["id"] for c in state.public_channels() if not c.get("is_member")]
    joined, already, pending_ops, problems = [], [], [], []
    for ref in refs:
        c = _resolve_channel(state, ref)
        if not c:
            problems.append({"channel": ref, "error": "not found among public channels — if it is private, "
                                                      "a member has to /invite the bot"})
            continue
        name = c.get("name") or c["id"]
        if c.get("is_member"):
            already.append("#" + name)
            continue
        if c.get("is_private"):
            problems.append({"channel": "#" + name, "error": "private: a member must /invite the bot"})
            continue
        if core.matches(pol.get("never_join") or [], c["id"], name):
            problems.append({"channel": "#" + name, "error": "on the never_join list"})
            continue
        op = {"method": "conversations.join", "params": {"channel": c["id"]}, "name": name}
        if who["trusted"] or core.matches(pol.get("auto_join") or [], c["id"], name):
            r = _execute(state, op)
            (joined.append("#" + name) if r["ok"] else problems.append({"channel": "#" + name,
                                                                        "error": r.get("error")}))
        else:
            pending_ops.append(op)
    if joined:
        with state._lock:
            state._chan_list = (0.0, [])
    out: dict = {"joined": joined, "already_member": already, "problems": problems}
    if pending_ops:
        out["approval"] = _request_or_run(state, who, pending_ops, args.get("reason") or "")
    return _out(out)


def tool_api(state: State, args: dict, **_) -> str:
    who = state.requester()
    method = (args.get("method") or "").strip()
    params = args.get("params") or {}
    if isinstance(params, str):
        try:
            params = json.loads(params)
        except ValueError:
            return _out({"error": "params must be a JSON object"})
    if not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_.]+", method):
        return _out({"error": "method must look like conversations.history"})
    if method == "conversations.join":
        return tool_join(state, {"channels": [core.target_channel(params)], "reason": args.get("reason")})
    tier = core.method_tier(method)
    ch = core.target_channel(params)
    pol = state.store.policy()
    if tier == core.READ:
        ok, why = state.can_read(ch, who)
        if not ok:
            return _out({"error": why})
        allowed = True
    elif tier == core.ACT:
        allowed = (who["trusted"] or (ch and ch == who["channel"])
                   or (not ch and method in core.ACT_ANYWHERE) or method in (pol.get("auto_methods") or []))
    else:
        allowed = who["trusted"] or method in (pol.get("auto_methods") or [])
    if not allowed:
        op = {"method": method, "params": params}
        return _out({"tier": tier, **_request_or_run(state, who, [op], args.get("reason") or "")})
    try:
        out = state.api(method, params)
    except core.SlackError as exc:
        hint = ""
        if exc.error in ("missing_scope",):
            hint = f" (needs scope {exc.data.get('needed')}; ask an admin to add it and reinstall the app)"
        elif exc.error == "not_in_channel":
            hint = " (join it first with slack_join, or ask a member to /invite the bot if it's private)"
        return _out({"error": exc.error + hint})
    if not (out.get("response_metadata") or {}).get("next_cursor"):
        out.pop("response_metadata", None)
    return _out({"tier": tier, **out})


def tool_request(state: State, args: dict, **_) -> str:
    rid = (args.get("request_id") or "").strip()
    if rid:
        req = state.store.request(rid)
        if not req:
            return _out({"error": "no such request"})
        return _out({k: req.get(k) for k in ("id", "status", "reason", "results", "decided_by")}
                    | {"ops": [core.op_label(o) for o in req["ops"]]})
    pol = state.store.policy()
    return _out({"policy": {k: pol.get(k) for k in ("auto_join", "never_join", "auto_methods",
                                                    "quiet_channels")},
                 "admins": sorted(state.admins())})


SCHEMAS = {
    "slack_recent": ("Read what has been happening in Slack channels the bot is in, from the bot's own "
                     "observation log (includes messages that did not mention it). No channel → list of "
                     "active channels with counts. With channel → the messages.", {
        "channel": {"type": "string", "description": "#name, name, <#C…>, or channel id"},
        "hours": {"type": "number", "description": "lookback window, default 24"},
        "thread_ts": {"type": "string", "description": "only this thread"},
        "limit": {"type": "integer"},
        "include_events": {"type": "boolean", "description": "joins/leaves/reactions/pins too"}}, []),
    "slack_join": ("Join public Slack channels (bulk). Channels matching the standing policy (or asked for by "
                   "an admin) are joined now; the rest go to the admins as ONE approval DM. Private channels "
                   "need a member to /invite the bot.", {
        "channels": {"type": "array", "items": {"type": "string"},
                     "description": "names or ids; [\"*\"] = every public channel not joined yet"},
        "reason": {"type": "string", "description": "why — shown to the admins"}}, ["channels"]),
    "slack_api": ("Call any Slack Web API method with the bot token (e.g. conversations.history, "
                  "conversations.replies, users.info, search.messages, assistant.search.context, "
                  "reactions.add, pins.add, bookmarks.add, canvases.create, conversations.setTopic). Reads "
                  "are free except other private conversations; posting/reacting is free in the current "
                  "conversation; anything else or admin-type methods (topic/rename/archive/invite/kick/"
                  "usergroups/profile) needs an admin's approval, requested automatically.", {
        "method": {"type": "string"},
        "params": {"type": "object", "description": "method arguments as JSON"},
        "reason": {"type": "string", "description": "why — shown to the admins if approval is needed"}},
        ["method"]),
    "slack_request": ("Check an approval request by id, or (no id) show the standing policy and admins.", {
        "request_id": {"type": "string"}}, []),
}
HANDLERS = {"slack_recent": tool_recent, "slack_join": tool_join, "slack_api": tool_api,
            "slack_request": tool_request}


# =================================================================================================
# Follower / engagement tools (toolset slack_workspace; visible only where peer.json configures them)
# =================================================================================================

def _engaged_scope(peer, channel: str, thread: str) -> Optional[dict]:
    return peer.engs().active_for(channel, thread or "")


def _is_inbox_card(peer, session_id: str) -> bool:
    """True when this turn runs inside one of this profile's Inbox cards (the owner pressed Do it or
    replied there)."""
    try:
        return bool(session_id and _inbox_for(peer.home).get(session_id))
    except Exception:
        return False


def ftool_post(peer, args: dict, session_id: str = "", **_) -> str:
    p = follow.Poster(peer.engs(), peer.api, peer.bot_user_id)
    return _out(p.post(args.get("channel", ""), args.get("text", ""), args.get("thread_ts") or "",
                       args.get("reply_to") or "", owner_approved=_is_inbox_card(peer, session_id)))


def ftool_react(peer, args: dict, **_) -> str:
    ch, ts = args.get("channel", ""), args.get("ts", "")
    m = [r for r in peer.store().recent(ch, float(ts or 0) - 0.0001, limit=5) if r["ts"] == ts]
    if not _engaged_scope(peer, ch, (m[0]["thread_ts"] or ts) if m else ts):
        return _out({"ok": False, "error": "not_engaged"})
    try:
        peer.api("reactions.add", {"channel": ch, "timestamp": ts, "name": (args.get("name") or "eyes").strip(":")})
    except core.SlackError as exc:
        if exc.error != "already_reacted":
            return _out({"ok": False, "error": exc.error})
    return _out({"ok": True})


def ftool_read(peer, args: dict, **_) -> str:
    ch, thread = args.get("channel", ""), args.get("thread_ts") or ""
    e = _engaged_scope(peer, ch, thread)
    if not e and not peer.engs().list():
        return _out({"error": "not_engaged"})
    if thread:
        try:
            d = peer.api("conversations.replies", {"channel": ch, "ts": thread, "limit": 200})
            rows = [{"ts": m["ts"], "thread_ts": m.get("thread_ts") or "", "user": m.get("user") or "",
                     "bot": m.get("bot_id") or "", "text": m.get("text") or ""} for m in d.get("messages") or []]
        except core.SlackError as exc:
            return _out({"error": exc.error})
    else:
        hours = float(args.get("hours") or 72)
        rows = peer.store().recent(ch, time.time() - hours * 3600, limit=int(args.get("limit") or 80))
    bot = peer.bot_user_id()
    names: dict[str, str] = {}

    def name(uid: str) -> str:
        if not uid:
            return "?"
        if uid not in names:
            hit = peer.store().cached(uid)
            if hit:
                names[uid] = hit["name"]
            else:
                try:
                    u = peer.api("users.info", {"user": uid})["user"]
                    names[uid] = (u.get("profile") or {}).get("display_name") or u.get("real_name") or u.get("name") or uid
                except core.SlackError:
                    names[uid] = uid
        return names[uid]
    return _out({"channel": ch, "thread_ts": thread or None, "messages": follow._fmt(rows, name, bot),
                 "engagement": e and e["id"]})


def ftool_escalate(peer, args: dict, **_) -> str:
    inbox = _inbox_for(peer.home)

    def post(title, body, accept_prompt, key=None, poster=None, type=None, fields=None):
        return _post_typed(inbox, title, body, accept_prompt, key=key, poster=poster, type=type, fields=fields)
    return _out(follow.escalate(peer.engs(), post, inbox_outcome=inbox.outcome, channel=args.get("channel", ""),
                                thread_ts=args.get("thread_ts") or "", topic=args.get("topic") or "",
                                title=args.get("title") or "", body=args.get("body") or "",
                                reply_text=args.get("reply_text") or "", reply_to=args.get("reply_to") or "",
                                action=args.get("action") or "", permalink=args.get("permalink") or ""))


def ftool_defer(peer, args: dict, session_id: str = "", **_) -> str:
    ch, thread = args.get("channel", ""), args.get("thread_ts") or ""
    e = _engaged_scope(peer, ch, thread)
    if not e:
        return _out({"ok": False, "error": "not_engaged"})
    target = (args.get("target") or e.get("owner") or "").strip()
    if not target:
        return _out({"ok": False, "error": "no target session (engagement has no owner); pass target"})
    gw = peer.cfg().get("self_gateway") or ""
    me = f"@session:{peer.cfg().get('self_profile') or follow.DEFAULT_FOLLOWER}@{gw}/{session_id}" if gw and session_id else ""
    msg = (f"[From the Slack follower for {follow.scope_label(e)}, engagement {e['id']}]\n\n{args.get('message', '')}"
           + f"\n\nWhen done, send the result back with slack_engagements(action='message', id='{e['id']}', "
             f"text=<result, incl. what to post in thread {thread or '(top level)'}>)"
           + (f" (or cross_session action=send target={me})." if me else "."))
    q = peer.engs().queue_mbp(e["id"], target, msg, ch, thread)
    return _out({"ok": True, "queued": q["id"], "target": target,
                 "note": "Delivered by the Mac's Desktop within ~1 min when it's running; otherwise the Mac's "
                         "pull cron runs it (every 2 min while the Mac is awake). Tell the thread you'll follow up."})


def ftool_engage(peer, args: dict, session_id: str = "", **_) -> str:
    args = dict(args)
    if not args.get("owner") and session_id:
        gw = peer.cfg().get("self_gateway") or ""
        prof = peer.cfg().get("self_profile") or "default"
        args["owner"] = f"@session:{prof}@{gw}/{session_id}" if gw else f"@session:{prof}/{session_id}"
    if isinstance(args.get("kanban_ids"), str):
        args["kanban_ids"] = [x for x in re.split(r"[\s,]+", args["kanban_ids"]) if x]
    if peer.remote:
        return _out(peer.remote_op("create", args))
    return _out(follow.engagement_op(peer.engs(), "create", args))


def ftool_engagements(peer, args: dict, session_id: str = "", **_) -> str:
    op = args.get("action") or "list"
    if op == "message" and not args.get("sender") and session_id:
        gw = peer.cfg().get("self_gateway") or ""
        prof = peer.cfg().get("self_profile") or "default"
        args = dict(args, sender=f"@session:{prof}@{gw}/{session_id}" if gw else f"@session:{prof}/{session_id}")
    if peer.remote:
        return _out(peer.remote_op(op, args))
    return _out(follow.engagement_op(peer.engs(), op, args))


_CH = {"type": "string", "description": "channel id (C…/G…)"}
FSCHEMAS = {
    "slack_post": ("Post as the bot in an engaged channel (follower only). One reply per message: pass "
                   "reply_to=<ts of the message you answer>; a second reply to it is refused. Replies to bots "
                   "stop after 3 bot-to-bot turns per thread. Returns the verified ts.", {
        "channel": _CH, "text": {"type": "string", "description": "Slack mrkdwn"},
        "thread_ts": {"type": "string", "description": "thread to reply in ('' = top level)"},
        "reply_to": {"type": "string", "description": "ts of the message this answers"}}, ["channel", "text"], "local"),
    "slack_react": ("Add a reaction (eyes = on it, white_check_mark = done) in an engaged channel.", {
        "channel": _CH, "ts": {"type": "string"}, "name": {"type": "string", "description": "emoji name"}},
        ["channel", "ts"], "local"),
    "slack_read": ("Read an engaged channel (observed log) or one thread (live conversations.replies).", {
        "channel": _CH, "thread_ts": {"type": "string"}, "hours": {"type": "number"},
        "limit": {"type": "integer"}}, ["channel"], "local"),
    "slack_escalate": ("Post ONE Inbox card for the owner (nothing is posted in Slack): commitments, personal/"
                       "business topics, out-of-brief or over-budget work, bot-loop cap, questions you can't "
                       "ground. Same channel+thread+topic updates the same card. 'Do it' runs `action` and/or "
                       "posts `reply_text` in the thread.", {
        "channel": _CH, "thread_ts": {"type": "string"}, "reply_to": {"type": "string"},
        "topic": {"type": "string", "description": "short stable topic, e.g. 'board seat'"},
        "title": {"type": "string"}, "body": {"type": "string", "description": "who asked what, evidence"},
        "reply_text": {"type": "string", "description": "drafted reply to post on Do it"},
        "action": {"type": "string", "description": "exact instruction to run on Do it"},
        "permalink": {"type": "string"}}, ["channel", "topic", "title", "body"], "local"),
    "slack_defer_to_mbp": ("Hand work that needs the Mac (local checkouts, its Kanban cards, browser, rented "
                           "machines) to the engagement's originating Mac session. Queued and retried until the "
                           "Desktop is online; the session is asked to send the result back to you.", {
        "channel": _CH, "thread_ts": {"type": "string"}, "message": {"type": "string"},
        "target": {"type": "string", "description": "@session link (default: the engagement owner)"}},
        ["channel", "message"], "local"),
    "slack_engage": ("Bring the Slack bot into a channel with a follower agent that monitors and participates "
                     "there with the owner's context (replies, Inbox escalations). Public channels are joined "
                     "automatically; private ones need the bot invited first.", {
        "channel": {"type": "string", "description": "channel id"},
        "brief": {"type": "string", "description": "what to follow, members, goals, rules, what is in/out of scope"},
        "context": {"type": "string", "description": "handoff: summary of this session, links, open work"},
        "thread": {"type": "string", "description": "limit to one thread ts"},
        "until": {"type": "string", "description": "end time: ISO date, epoch, or '7d'"},
        "kanban_ids": {"type": "array", "items": {"type": "string"}},
        "budget": {"type": "string"}, "name": {"type": "string", "description": "channel name"},
        "last_handled_ts": {"type": "string", "description": "skip messages up to this ts"},
        "test": {"type": "boolean", "description": "test engagement: escalations are recorded but kept out of "
                                                   "the owner's Inbox unless the test_inbox toggle is on"}},
        ["channel", "brief"], "any"),
    "slack_engagements": ("List / get / pause / resume / end / update Slack engagements. action=message (id, text) "
                          "holds a result for the engagement's follower, which posts ONE summary per finished batch "
                          "(final=true on the last result releases it; else after 2 h quiet). Never post results to the channel yourself. "
                          "update test=true|false "
                          "marks a test engagement; action=test_inbox (enabled=true|false, omit to read) is the "
                          "global toggle for whether test engagements' escalations reach the Inbox (default off).", {
        "action": {"type": "string", "enum": ["list", "get", "pause", "resume", "end", "update", "message",
                                              "test_inbox"]},
        "text": {"type": "string", "description": "message: result/answer for the follower"},
        "final": {"type": "boolean", "description": "message: this completes the batch; post the summary now"},
        "id": {"type": "string"}, "brief": {"type": "string"}, "context": {"type": "string"},
        "kanban_ids": {"type": "array", "items": {"type": "string"}}, "budget": {"type": "string"},
        "until": {"type": "string"}, "test": {"type": "boolean"}, "enabled": {"type": "boolean"}}, [], "any"),
}
FHANDLERS = {"slack_post": ftool_post, "slack_react": ftool_react, "slack_read": ftool_read,
             "slack_escalate": ftool_escalate, "slack_defer_to_mbp": ftool_defer, "slack_engage": ftool_engage,
             "slack_engagements": ftool_engagements}


HANDOFF_DESC = ("Hand the owner's request about one of their own machines (their MacBook / Mac mini: display, mouse, "
                "frozen app, reboot, 'fix it from here') to the owner-only machine-repair agent, which diagnoses, "
                "repairs and posts a screenshot in this DM thread. Use it when the owner DMs you such a request, "
                "or after run_in_user_terminal is refused for the owner's DM. Works only in the owner's own Slack DM; "
                "never give the owner commands or keyboard steps to paste instead.")


def _register_handoff(ctx, state: State) -> None:
    def handler(args, **kw):
        try:
            def env(name):
                return _session(name[len("HERMES_SESSION_"):])

            def last_owner_msg(ch, owner):
                # The owner's own latest human message in this DM (Slack-verified store), not the model's text.
                rows = [r for r in state.store.recent(ch, time.time() - 3600, limit=50)
                        if r.get("user") == owner and not r.get("bot") and (r.get("text") or "").strip()]
                return rows[-1] if rows else None
            return ownerops.handoff_tool(_owner_ops(state), dict(args or {}), env=env, last_owner_msg=last_owner_msg)
        except Exception as exc:
            logger.warning("mac_ops_handoff failed: %s", exc, exc_info=True)
            return _out({"ok": False, "error": str(exc)})

    schema = {"name": "mac_ops_handoff", "description": HANDOFF_DESC, "parameters": {
        "type": "object", "properties": {"request": {"type": "string", "description": "the owner's request, in their words"}},
        "required": ["request"]}}
    ctx.register_tool(name="mac_ops_handoff", toolset=TOOLSET, schema=schema, handler=handler, description=HANDOFF_DESC,
                      check_fn=lambda: bool(ownerops.owner_ids(ownerops.home_cfg(state.store.root.parent))))


def register(ctx):
    try:
        from hermes_constants import get_hermes_home
        root = Path(get_hermes_home()) / "slack-workspace"
    except Exception:
        root = Path.home() / ".hermes" / "slack-workspace"
    state = STATE.setdefault(str(root), State(root))

    ctx.register_platform_handler("slack", _make_factory(state))
    ctx.register_hook("pre_llm_call", _make_context_hook(state))
    ctx.register_slack_action_handler("sw_approve", _make_action_handler(state, "approve"))
    ctx.register_slack_action_handler("sw_always", _make_action_handler(state, "always"))
    ctx.register_slack_action_handler("sw_deny", _make_action_handler(state, "deny"))
    ctx.register_slack_action_handler(re.compile(r"^sw_pick_\d+$"), _ack_only)

    for name, (desc, props, required) in SCHEMAS.items():
        fn = HANDLERS[name]

        def handler(args, _fn=fn, **kw):
            try:
                return _fn(state, dict(args or {}), **kw)
            except Exception as exc:
                logger.warning("slack-workspace %s failed: %s", _fn.__name__, exc, exc_info=True)
                return _out({"error": str(exc)})

        schema = {"name": name, "description": desc, "parameters": {
            "type": "object", "properties": props, "required": required}}
        ctx.register_tool(name=name, toolset=TOOLSET, schema=schema, handler=handler, description=desc,
                          check_fn=state.available)

    _register_handoff(ctx, state)

    peer = follow.Peer(root.parent)
    for name, (desc, props, required, where) in FSCHEMAS.items():
        fn = FHANDLERS[name]

        def fhandler(args, _fn=fn, **kw):
            try:
                return _fn(peer, dict(args or {}), **kw)
            except Exception as exc:
                logger.warning("slack-workspace %s failed: %s", _fn.__name__, exc, exc_info=True)
                return _out({"ok": False, "error": f"{type(exc).__name__}: {exc}"})

        schema = {"name": name, "description": desc, "parameters": {
            "type": "object", "properties": props, "required": required}}
        check = (lambda: peer.local) if where == "local" else (lambda: peer.local or peer.remote)
        ctx.register_tool(name=name, toolset=TOOLSET, schema=schema, handler=fhandler, description=desc,
                          check_fn=check)
